"""Ringkasan dokumen sebagai knowledge turunan.

Kenapa ini ada: pertanyaan "ringkas dokumen X" sulit dijawab dari potongan-potongan hasil
pencarian kemiripan - yang terambil hanya bagian yang mirip dengan pertanyaannya, bukan
keseluruhan isi. Dua akibatnya nyata:

* jawabannya jadi ringkasan sebagian (atau model bilang datanya tidak ada di konteks);
* menyeret seluruh dokumen ke konteks itu mahal dan cepat menghabiskan anggaran token.

Jalan keluarnya: saat dokumen diindeks, buat **ringkasan** dari seluruh isinya dan simpan
sebagai potongan tersendiri di indeks yang sama (``is_summary=True``, ``document_id`` sama
dengan dokumen asalnya). Pertanyaan yang meminta ringkasan lalu menemukan potongan itu, dan
jawabannya datang dari satu potongan padat - bukan dari tebak-tebakan atas sebagian isi.

Aturan yang dipegang:

* **Ringkasan selalu berasal dari isi dokumen itu sendiri.** Potongannya diambil lewat
  ``list_document_chunks`` yang disaring tenant, jadi tidak ada isi tenant lain yang bocor.
* **Dokumen besar diringkas bertahap (map-reduce).** Isi dibagi menjadi beberapa kelompok yang
  muat di jendela model, tiap kelompok diringkas, lalu ringkasan-ringkasan itu diringkas lagi.
  Tanpa ini, dokumen besar hanya akan diringkas sampai batas konteks pertama.
* **Ringkasan gagal tidak menggagalkan pengindeksan.** Isi dokumen tetap terindeks dan bisa
  dicari; yang hilang hanya potongan ringkasannya, dan alasannya dilaporkan apa adanya
  (``summary_error``) supaya tidak terbaca sebagai "sudah diringkas".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from app.core.logging import get_logger
from app.rag.chunker import estimate_tokens
from app.rag.constants import SUMMARY_CHUNK_ID
from app.rag.context import DocumentCoverage, build_context

logger = get_logger(__name__)

@dataclass
class SummaryResult:
    text: str = ""
    tokens: int = 0
    passes: int = 0
    parts: int = 0
    truncated: bool = False
    error: str = ""
    # Diisi bila ringkasannya hanya mencakup sebagian dokumen (batas tahap/waktu/potongan).
    # Dipakai supaya "ringkasan sebagian" tidak terbaca sebagai "ringkasan lengkap".
    partial: str = ""

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def _group_parts(parts: Sequence[Dict[str, Any]], budget_tokens: int) -> List[List[Dict[str, Any]]]:
    """Bagi potongan dokumen menjadi kelompok yang muat di satu jendela model."""
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    spent = 0
    for part in parts:
        tokens = estimate_tokens(str(part.get("content") or ""))
        if current and spent + tokens > budget_tokens:
            groups.append(current)
            current = []
            spent = 0
        current.append(part)
        spent += tokens
    if current:
        groups.append(current)
    return groups


def _context_from_parts(parts: Sequence[Dict[str, Any]], budget_tokens: int):
    """Ubah potongan dokumen menjadi ``BuiltContext`` dengan blok bernomor ``[n]``."""
    from app.rag.retriever import Candidate

    candidates: List[Candidate] = []
    for position, payload in enumerate(parts, start=1):
        content = str(payload.get("content") or "").strip()
        if not content:
            continue
        candidates.append(
            Candidate(
                chunk_id=str(payload.get("chunk_id") or f"part_{position:04d}"),
                document_id=str(payload.get("document_id") or ""),
                content=content,
                document_name=str(payload.get("document_name") or ""),
                page=payload.get("page"),
                section=str(payload.get("section") or ""),
                source_url=str(payload.get("source_url") or ""),
                expanded=True,
                document_order=position,
            )
        )
    coverage = [
        DocumentCoverage(
            document_id=candidates[0].document_id if candidates else "",
            document_name=candidates[0].document_name if candidates else "",
            included=len(candidates),
            total=len(candidates),
            complete=True,
            ordered=True,
        )
    ]
    return build_context(candidates, max_tokens=budget_tokens, max_chunks=max(1, len(candidates)), coverage=coverage)


def summarize_document(
    *,
    generator,
    settings,
    document_id: str,
    document_name: str,
    organization_id: str,
    knowledge_base_id: str,
    language: str = "",
    list_chunks=None,
    parts: Optional[Sequence[Dict[str, Any]]] = None,
) -> SummaryResult:
    """Buat ringkasan satu dokumen.

    ``parts`` boleh diberikan langsung oleh pemanggil yang sudah memegang potongannya (jalur
    pengindeksan: isinya baru saja diparse dan belum tentu sudah tersimpan). Bila tidak
    diberikan, potongan diambil lewat ``list_chunks`` (``repository.list_document_chunks``) yang
    disaring tenant - tidak ada jalan pintas lintas tenant.
    """
    if not getattr(settings, "document_summary_enabled", True):
        return SummaryResult(error="ringkasan dimatikan (DOCUMENT_SUMMARY_ENABLED=false)")

    if parts is None:
        if list_chunks is None:
            from app.qdrant import repository

            list_chunks = repository.list_document_chunks
        try:
            parts = list_chunks(
                settings,
                organization_id=organization_id,
                document_id=document_id,
                knowledge_base_id=knowledge_base_id,
            )
        except Exception as exc:  # noqa: BLE001 - ringkasan opsional
            return SummaryResult(error=f"gagal membaca isi dokumen: {exc}")

    # Potongan ringkasan lama jangan ikut diringkas lagi (bisa berputar-putar).
    parts = [part for part in parts if not part.get("is_summary")]
    if not parts:
        return SummaryResult(error="dokumen tidak punya isi untuk diringkas")

    budget = max(2000, int(getattr(settings, "summary_window_tokens", 12000)))
    max_parts = max(1, int(getattr(settings, "summary_max_parts", 400)))
    max_stages = max(1, int(getattr(settings, "summary_max_stages", 12)))
    deadline = time.monotonic() + max(10.0, float(getattr(settings, "summary_budget_seconds", 120.0)))
    partial_reason = ""
    if len(parts) > max_parts:
        # Jangan diam-diam memotong: katakan bahwa ringkasannya mencakup sebagian.
        logger.warning(
            "dokumen %s punya %d potongan, ringkasan dibatasi %d potongan pertama",
            document_id,
            len(parts),
            max_parts,
        )
        parts = parts[:max_parts]
        partial_reason = f"dokumen punya lebih dari {max_parts} potongan; ringkasan mencakup bagian awal"

    groups = _group_parts(parts, budget)
    if len(groups) > max_stages:
        # Batas tahap: dokumen raksasa tidak boleh menjelajah tanpa ujung. Yang dilewati
        # dilaporkan, bukan disembunyikan.
        logger.warning(
            "dokumen %s butuh %d tahap ringkasan, dibatasi %d tahap",
            document_id,
            len(groups),
            max_stages,
        )
        groups = groups[:max_stages]
        partial_reason = partial_reason or f"ringkasan dibatasi {max_stages} tahap pertama"

    pieces: List[str] = []
    passes = 0
    truncated = False
    stopped_early = False

    for group in groups:
        if time.monotonic() > deadline:
            # Waktu habis: berhenti meringkas. Pengindeksan berjalan berurutan, jadi ringkasan
            # yang terlalu lama menahan unggahan lain di antrian - dan isi dokumen sudah
            # tersimpan lebih dulu, jadi tidak ada yang hilang.
            stopped_early = True
            logger.warning(
                "ringkasan dokumen %s dihentikan setelah %.0f detik (%d dari %d tahap selesai)",
                document_id,
                float(getattr(settings, "summary_budget_seconds", 120.0)),
                passes,
                len(groups),
            )
            break
        built = _context_from_parts(group, budget)
        if not built.used:
            continue
        answer = generator.summarize(
            context=built,
            document_name=document_name,
            language=language,
            max_tokens=int(getattr(settings, "summary_max_tokens", 2048)),
        )
        passes += 1
        truncated = truncated or bool(getattr(answer, "truncated", False))
        text = (answer.answer or "").strip()
        if text:
            pieces.append(text)

    if not pieces:
        reason = "waktu ringkasan habis sebelum satu tahap pun selesai" if stopped_early else "model tidak menghasilkan ringkasan"
        return SummaryResult(error=reason, passes=passes, truncated=truncated)

    # Satu kelompok = ringkasannya sudah final. Lebih dari satu = ringkas ulang gabungannya.
    if len(pieces) == 1:
        final = pieces[0]
    elif time.monotonic() > deadline:
        # Waktu habis di tahap reduce: gabungkan apa adanya daripada tidak ada ringkasan.
        stopped_early = True
        final = "\n\n".join(pieces)
    else:
        final = _reduce(generator, settings, pieces, document_name, language, budget)
        passes += 1

    final = final.strip()
    if not final:
        return SummaryResult(error="ringkasan akhir kosong", passes=passes, truncated=truncated)
    if stopped_early:
        partial_reason = partial_reason or "waktu ringkasan habis; ringkasan mencakup bagian yang sempat diproses"
    return SummaryResult(
        text=final,
        tokens=estimate_tokens(final),
        passes=passes,
        parts=len(parts),
        truncated=truncated,
        partial=partial_reason,
    )


def _reduce(generator, settings, pieces: Sequence[str], document_name: str, language: str, budget: int) -> str:
    """Ringkas gabungan ringkasan bagian menjadi satu ringkasan akhir."""
    from app.rag.retriever import Candidate

    joined: List[str] = []
    spent = 0
    for index, piece in enumerate(pieces, start=1):
        tokens = estimate_tokens(piece)
        if spent + tokens > budget:
            break
        joined.append(piece)
        spent += tokens

    candidates = [
        Candidate(
            chunk_id=f"piece_{index:03d}",
            document_id="summary",
            content=piece,
            document_name=document_name,
            expanded=True,
            document_order=index,
        )
        for index, piece in enumerate(joined, start=1)
    ]
    built = build_context(candidates, max_tokens=budget, max_chunks=max(1, len(candidates)))
    if not built.used:
        return pieces[0]
    answer = generator.summarize(
        context=built,
        document_name=document_name,
        language=language,
        max_tokens=int(getattr(settings, "summary_max_tokens", 2048)),
    )
    text = (answer.answer or "").strip()
    return text or "\n\n".join(joined)


def summary_chunk_payload(
    *,
    summary: str,
    document_id: str,
    document_name: str,
    organization_id: str,
    knowledge_base_id: str,
    language: str = "",
    source_url: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Payload potongan ringkasan. ``is_summary`` yang membuatnya bisa dibedakan di daftar."""
    return {
        "document_id": document_id,
        "organization_id": organization_id,
        "knowledge_base_id": knowledge_base_id,
        "chunk_id": SUMMARY_CHUNK_ID,
        "content": summary,
        "document_name": f"Ringkasan: {document_name}" if document_name else "Ringkasan",
        "page": None,
        "section": "Ringkasan dokumen",
        "source_url": source_url,
        "page_title": "",
        "language": language or "id",
        "chunk_index": -1,  # selalu di depan saat dokumen dilengkapi
        "token_count": estimate_tokens(summary),
        "is_table": False,
        "is_summary": True,
        "summary_of": document_id,
        "created_at": "",
        "extra": dict(metadata or {}),
    }
