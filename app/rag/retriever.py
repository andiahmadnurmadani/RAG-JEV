"""Hybrid retrieval: dense + BM25 + fusion + rerank (PRD 12, 13, 14, 15).

Order of operations is deliberate and matches the PRD:

    embed query -> tenant-filtered dense search
                -> tenant-scoped BM25 search
                -> fusion (RRF)  -> dedupe
                -> reranker      -> relevance threshold

Every hit is re-read from Qdrant through a tenant-constrained lookup before it can
reach the context builder, so a chunk from another organization cannot survive the
pipeline even if an index were poisoned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger
from app.qdrant import repository
from app.rag.embedder import EmbedderService
from app.rag.reranker import RerankerService
from app.rag.sparse import SparseIndex

logger = get_logger(__name__)


@dataclass
class Candidate:
    chunk_id: str
    document_id: str
    content: str
    document_name: str = ""
    page: Optional[int] = None
    section: str = ""
    source_url: str = ""
    language: str = ""
    score: float = 0.0
    dense_score: Optional[float] = None
    sparse_score: Optional[float] = None
    fused_score: Optional[float] = None
    rerank_score: Optional[float] = None
    # Kemiripan makna kueri-potongan (kosinus vektor) dan skor kata kunci (reranker leksikal),
    # sebelum digabung. Hanya terisi dengan embedder semantik.
    semantic_score: Optional[float] = None
    lexical_score: Optional[float] = None
    # Bagian dokumen yang DISERTAKAN untuk melengkapi dokumen (bukan hasil pencarian baris
    # teratas). Dipakai perender konteks untuk menandai urutan dokumen + menyusun catatan
    # kelengkapan, bukan untuk mengubah skor.
    expanded: bool = False
    document_order: int = 0
    # Potongan ringkasan dokumen (knowledge turunan). Bukan bagian isi: dipakai menjawab
    # pertanyaan "ringkas dokumen ini", dan tidak dihitung sebagai bagian isi dokumen.
    is_summary: bool = False

    def to_source(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_name": self.document_name,
            "chunk_id": self.chunk_id,
            "page": self.page,
            "section": self.section,
            "source_url": self.source_url,
            "score": round(float(self.score), 6),
        }


@dataclass
class RetrievalResult:
    candidates: List[Candidate] = field(default_factory=list)
    dense_hits: int = 0
    sparse_hits: int = 0
    fused_count: int = 0
    reranked: bool = False
    best_score: float = 0.0
    elapsed_ms: float = 0.0
    rerank_ms: float = 0.0
    reranker_used: str = "none"
    hybrid_used: bool = True
    dense_weight: float = 1.0
    best_dense: float = 0.0
    # Gerbang relevansi: apakah kandidat terbaik cukup relevan untuk dijawab, dan atas dasar apa.
    relevant: bool = False
    gate: str = ""

    @property
    def count(self) -> int:
        return len(self.candidates)


def fused_key(document_id: str, chunk_id: str) -> str:
    """Identity of a chunk: ``chunk_id`` alone repeats across documents (``chunk_0001``)."""
    return f"{document_id}::{chunk_id}"


def is_semantic(settings: Settings) -> bool:
    """Embedder yang benar-benar menangkap makna (bukan ``hash`` yang hanya menghitung kata)."""
    return str(settings.embedding_provider or "hash") != "hash"


def effective_dense_weight(settings: Settings) -> float:
    """Bobot sisi vektor di fusi. Embedder ``hash`` hanyalah tiruan BM25 yang lebih kasar (tanpa
    IDF, kata umum berbobot penuh); diberi bobot setara BM25 ia justru mengencerkan hasil."""
    if is_semantic(settings):
        return float(settings.dense_weight)
    return min(float(settings.dense_weight), float(settings.hash_dense_weight))


def _cosine(query_vector: Optional[Sequence[float]], vector: Optional[Sequence[float]]) -> Optional[float]:
    """Kosinus dua vektor yang sudah dinormalkan (embedder selalu menormalkan)."""
    if not query_vector or not vector or len(query_vector) != len(vector):
        return None
    return float(sum(a * b for a, b in zip(query_vector, vector)))


def blended_score(settings: Settings, lexical: float, semantic: Optional[float]) -> float:
    """Skor akhir 0..1: kemiripan makna (dikalibrasi ke 0..1) + kata kunci, berbobot.

    Kosinus mentah tidak pernah 0 untuk teks tak berhubungan, jadi dipetakan linear dari
    ``semantic_floor`` (=0, tak berhubungan) ke ``semantic_ceil`` (=1, sangat mirip).
    """
    if semantic is None:
        return float(lexical)
    floor, ceil = float(settings.semantic_floor), float(settings.semantic_ceil)
    meaning = min(1.0, max(0.0, (semantic - floor) / max(1e-6, ceil - floor)))
    weight = min(1.0, max(0.0, float(settings.semantic_weight)))
    return round(weight * meaning + (1.0 - weight) * float(lexical), 6)


def chunk_position(chunk_id: str, payload: Optional[Dict[str, Any]] = None) -> int:
    """Urutan potongan di dokumennya (``chunk_0007`` -> 7)."""
    index = (payload or {}).get("chunk_index")
    if isinstance(index, int):
        return index
    digits = str(chunk_id or "").rsplit("_", 1)[-1]
    return int(digits) if digits.isdigit() else 0


# Pertanyaan yang memang meminta beberapa dokumen sekaligus: fokus dokumen dilonggarkan.
_MULTI_DOCUMENT_RE = re.compile(
    r"\b(banding\w*|perbandingan|bedanya|perbedaan|beda|persamaan|versus|vs|masing-masing|"
    r"semua\s+(dokumen|cabang|unit|divisi|wilayah)|setiap\s+(dokumen|cabang|unit|divisi|wilayah)|"
    r"compare|comparison|difference|each)\b",
    re.IGNORECASE,
)


def wants_multiple_documents(query: str) -> bool:
    return bool(_MULTI_DOCUMENT_RE.search(query or ""))


def _focus_documents(candidates: Sequence[Candidate], ratio: float, limit: int) -> List[Candidate]:
    """Hanya dokumen yang skor terbaiknya dekat dengan dokumen teratas, paling banyak ``limit``.

    Saat knowledge berisi ribuan dokumen, potongan dari banyak dokumen yang "agak mirip" lolos
    ambang per potongan: konteks berisi ~10 dokumen dan hanya ~16% darinya dokumen yang benar,
    sehingga jawaban model rawan mencampur fakta. Penyaringan per DOKUMEN menjaga konteks fokus;
    dokumen yang skornya setara tetap ikut (pertanyaan yang memang menyangkut beberapa dokumen).
    """
    best: Dict[str, float] = {}
    for candidate in candidates:
        if candidate.document_id not in best or candidate.score > best[candidate.document_id]:
            best[candidate.document_id] = candidate.score
    if not best:
        return list(candidates)
    top = max(best.values())
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    keep = {document for document, score in ranked[: max(1, limit)] if score >= top * ratio}
    return [candidate for candidate in candidates if candidate.document_id in keep]


def _dedupe_by_document(candidates: Sequence[Candidate], max_per_document: int) -> List[Candidate]:
    """Keep the best chunks but stop one document from monopolising the context."""
    counts: Dict[str, int] = {}
    kept: List[Candidate] = []
    for candidate in candidates:
        used = counts.get(candidate.document_id, 0)
        if used >= max_per_document:
            continue
        counts[candidate.document_id] = used + 1
        kept.append(candidate)
    return kept


class Retriever:
    def __init__(
        self,
        settings: Settings,
        embedder: EmbedderService,
        reranker: RerankerService,
        sparse: SparseIndex,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._reranker = reranker
        self._sparse = sparse

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        *,
        query: str,
        organization_id: str,
        knowledge_base_id: Optional[str] = None,
        final_k: int = 12,
        dense_top_k: Optional[int] = None,
        sparse_top_k: Optional[int] = None,
        use_hybrid: Optional[bool] = None,
        use_reranker: Optional[bool] = None,
        threshold: Optional[float] = None,
        document_ids: Optional[Sequence[str]] = None,
        focus: bool = True,
    ) -> RetrievalResult:
        settings = self._settings
        scope = [str(item) for item in (document_ids or []) if item] or None
        dense_top_k = dense_top_k or settings.retrieval_dense_top_k
        sparse_top_k = sparse_top_k or settings.retrieval_sparse_top_k
        hybrid = settings.retrieval_hybrid if use_hybrid is None else use_hybrid
        rerank = settings.reranker_enabled if use_reranker is None else use_reranker
        threshold = settings.relevance_threshold if threshold is None else threshold

        result = RetrievalResult()
        result.hybrid_used = bool(hybrid and knowledge_base_id)

        dense_hits: List[Tuple[str, float]] = []
        query_vector: Optional[List[float]] = None
        if settings.retrieval_dense_enabled:
            vector = self._embedder.encode([query], is_query=True)[0]
            query_vector = list(vector)
            for hit in repository.search_dense(
                settings,
                vector=vector,
                organization_id=organization_id,
                knowledge_base_id=knowledge_base_id,
                top_k=dense_top_k,
                document_ids=scope,
            ):
                if hit.get("chunk_id"):
                    dense_hits.append((fused_key(hit.get("document_id", ""), hit["chunk_id"]), float(hit["score"])))
        result.dense_hits = len(dense_hits)

        sparse_hits: List[Tuple[str, float]] = []
        if hybrid and knowledge_base_id:
            sparse_hits = self._sparse.search(
                query, organization_id, knowledge_base_id, top_k=sparse_top_k, document_ids=scope
            )
        result.sparse_hits = len(sparse_hits)

        dense_weight = effective_dense_weight(settings)
        result.dense_weight = dense_weight
        fused = fuse(dense_hits, sparse_hits, k=settings.rrf_k, dense_weight=dense_weight)
        result.fused_count = len(fused)
        if not fused:
            return result
        # Kumpulan kandidat yang dinilai reranker. Batas per dokumen TIDAK dipasang di sini:
        # urutan fusi masih kasar, jadi memotong per dokumen sekarang bisa membuang potongan
        # yang justru dinilai terbaik oleh reranker.
        pool_size = max(int(settings.reranker_candidates), final_k * 3)
        fused = fused[:pool_size]

        # Re-read payloads through the tenant filter: nothing reaches the LLM
        # that was not verified to belong to this organization.
        payloads = repository.get_chunks_by_ids(
            settings,
            chunk_ids=[key for key, _ in fused],
            organization_id=organization_id,
            knowledge_base_id=knowledge_base_id,
            document_ids=scope,
            with_vectors=bool(query_vector) and is_semantic(settings),
        )

        candidates: List[Candidate] = []
        dense_scores = dict(dense_hits)
        sparse_scores = dict(sparse_hits)
        seen_content: set = set()
        for key, fused_score in fused:
            payload = payloads.get(key)
            if payload is None:
                continue
            if knowledge_base_id and payload.get("knowledge_base_id") != knowledge_base_id:
                continue
            if scope is not None and payload.get("document_id") not in scope:
                continue
            content = payload.get("content", "")
            # Potongan yang isinya sama persis (boilerplate halaman web yang di-crawl, berkas
            # yang diunggah dua kali) hanya memenuhi daftar: cukup satu yang dipakai.
            fingerprint = " ".join(str(content).lower().split())
            if fingerprint in seen_content:
                continue
            seen_content.add(fingerprint)
            chunk_id = payload.get("chunk_id", "")
            candidates.append(
                Candidate(
                    chunk_id=chunk_id,
                    document_id=payload.get("document_id", ""),
                    content=content,
                    document_name=payload.get("document_name", ""),
                    page=payload.get("page"),
                    section=payload.get("section", ""),
                    source_url=payload.get("source_url", ""),
                    language=payload.get("language", ""),
                    score=fused_score,
                    dense_score=dense_scores.get(key),
                    sparse_score=sparse_scores.get(key),
                    fused_score=fused_score,
                    is_summary=bool(payload.get("is_summary")),
                    document_order=chunk_position(chunk_id, payload),
                    semantic_score=_cosine(query_vector, payload.get("__vector")),
                )
            )

        if not candidates:
            return result

        result.best_dense = max((c.dense_score or 0.0 for c in candidates), default=0.0)
        if rerank:
            rerank_started = time.perf_counter()
            candidates = self._apply_rerank(
                query,
                candidates,
                threshold=threshold,
                organization_id=organization_id,
                knowledge_base_id=knowledge_base_id,
            )
            result.rerank_ms = round((time.perf_counter() - rerank_started) * 1000, 2)
            # a no-op reranker must not be reported (or counted) as a real rerank
            result.reranked = settings.reranker_provider != "none"
            result.reranker_used = self._reranker.name

        candidates = _dedupe_by_document(candidates, settings.max_chunks_per_document)
        # Fokus dokumen hanya untuk jalur JAWABAN: /search dan ekstraksi memang meminta daftar
        # luas, dan dokumen yang dipilih pemakai secara eksplisit tidak boleh dibuang.
        if focus and rerank and settings.reranker_provider != "none" and not (scope and len(scope) > 1):
            ratio = float(settings.document_focus_ratio)
            limit = int(settings.max_context_documents)
            if wants_multiple_documents(query):
                ratio, limit = ratio * 0.6, max(limit, 6)
            candidates = _focus_documents(candidates, ratio, limit)
        candidates = candidates[:final_k]
        result.candidates = candidates
        result.best_score = max(
            ((candidate.rerank_score if candidate.rerank_score is not None else candidate.score) for candidate in candidates),
            default=0.0,
        )
        result.relevant, result.gate = self._relevance_gate(result, reranked=result.reranked)
        return result

    # ------------------------------------------------------------------ #
    def _relevance_gate(self, result: "RetrievalResult", *, reranked: bool) -> Tuple[bool, str]:
        """Apakah kandidat terbaik cukup relevan untuk dijawab?

        Leksikal (skor reranker absolut) ATAU semantik (kemiripan vektor dari embedder sungguhan).
        Embedder ``hash`` tidak dihitung sebagai bukti semantik - ia hanya menghitung kata.
        """
        settings = self._settings
        if not result.candidates:
            return False, "no_candidates"
        if not reranked:
            return True, "reranker_off"
        minimum = float(settings.min_relevance or 0.0)
        if minimum <= 0:
            return True, "gate_off"
        if result.best_score >= minimum:
            return True, "semantic" if is_semantic(settings) else "lexical"
        return False, "below_min_relevance"

    def _apply_rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        *,
        threshold: float,
        organization_id: str,
        knowledge_base_id: Optional[str],
    ) -> List[Candidate]:
        pool = list(candidates)
        idf = None
        if knowledge_base_id:
            try:
                idf = self._sparse.idf_function(organization_id, knowledge_base_id)
            except Exception as exc:  # noqa: BLE001 - IDF korpus pelengkap; tanpa itu IDF kandidat
                logger.warning("IDF korpus tidak tersedia: %s", exc)
        scored = self._reranker.rerank(
            query,
            [(fused_key(candidate.document_id, candidate.chunk_id), candidate.content) for candidate in pool],
            top_k=len(pool),
            idf=idf,
        )
        by_key = {fused_key(candidate.document_id, candidate.chunk_id): candidate for candidate in pool}
        ordered: List[Candidate] = []
        for key, _content, score in scored:
            candidate = by_key.get(key)
            if candidate is None:
                continue
            candidate.rerank_score = score
            candidate.score = score
            ordered.append(candidate)
        if self._settings.reranker_provider == "none":
            # NoopReranker returns descending pseudo-scores preserving fusion order;
            # filtering on them would silently drop relevant chunks, so do not.
            return ordered
        if is_semantic(self._settings):
            # Gabungkan makna + kata kunci. Reranker leksikal saja membuang kandidat yang cocok
            # MAKNANYA tetapi tidak berbagi kata (sinonim: "jatah libur" vs "kuota cuti").
            for candidate in ordered:
                candidate.lexical_score = candidate.score
                candidate.score = blended_score(self._settings, candidate.score, candidate.semantic_score)
                candidate.rerank_score = candidate.score
            ordered.sort(key=lambda item: item.score, reverse=True)
        best = max((candidate.score for candidate in ordered), default=0.0)
        if best <= 0:
            # Tidak ada satu pun kata kueri di kandidat. Dengan embedder semantik, kecocokan
            # makna (sinonim) tetap sah: pakai urutan fusi. Tanpa itu, tidak ada bukti relevansi.
            if is_semantic(self._settings):
                for candidate in pool:
                    candidate.score = candidate.fused_score or 0.0
                return list(pool)
            return []
        # Ambang RELATIF: buang kandidat yang jauh di bawah yang terbaik (derau di ekor daftar).
        return [candidate for candidate in ordered if candidate.score >= best * threshold]

    # ------------------------------------------------------------------ #
    def search_only(self, **kwargs) -> RetrievalResult:
        return self.retrieve(**kwargs)


def fuse(
    dense: Sequence[Tuple[str, float]],
    sparse: Sequence[Tuple[str, float]],
    *,
    k: int = 60,
    dense_weight: float = 1.0,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion (PRD 14 'Fusion').

    Rank-based fusion is preferred over score mixing because BM25 and cosine
    similarities are not on comparable scales. Candidates appearing in both lists
    are lifted; ties fall back to the dense order.
    """
    scores: Dict[str, float] = {}
    dense_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(dense)}
    sparse_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(sparse)}

    for chunk_id in set(dense_rank) | set(sparse_rank):
        score = 0.0
        if chunk_id in dense_rank:
            score += dense_weight * (1.0 / (k + dense_rank[chunk_id] + 1))
        if chunk_id in sparse_rank:
            score += 1.0 / (k + sparse_rank[chunk_id] + 1)
        scores[chunk_id] = score

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered:
        return []
    best = ordered[0][1] or 1.0
    return [(chunk_id, score / best) for chunk_id, score in ordered]
