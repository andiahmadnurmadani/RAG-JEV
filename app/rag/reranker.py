"""Reranking (PRD 8.3, 15).

Providers
---------
* ``sentence_transformers`` — PRD default ``BAAI/bge-reranker-v2-m3`` (CrossEncoder).
* ``fastembed``            — ONNX cross-encoder, light-RAM profile.
* ``lexical``              — **bawaan**, tanpa dependensi: skor silang IDF + frasa + kedekatan.
* ``none``                 — keep fusion order (rerank is then a no-op, reported as such).

Kenapa ``lexical`` ada
----------------------
Reranker neural butuh torch/fastembed (ratusan MB–GB). Image produksi sengaja ramping
(``WITH_LOCAL_MODELS=0``), dan gateway yang dipakai tidak menyediakan ``/rerank``. Akibatnya
``reranker_provider=none`` — yang **bukan** reranker: ia hanya mengembalikan skor menurun yang
mempertahankan urutan fusi, sehingga urutan hasil tidak pernah diperbaiki.

``LexicalReranker`` mengisi celah itu tanpa dependensi apa pun. Ia menilai tiap kandidat
terhadap kueri dengan sinyal yang **tidak** dipakai pencarian vektor: IDF kueri (kata langka
lebih menentukan), bonus frasa utuh, kedekatan kata, dan cakupan kueri. Ini bukan pengganti
reranker neural - ia tidak memahami makna - tetapi ia memperbaiki urutan jauh lebih baik
daripada tidak sama sekali, dan ia jujur melaporkan dirinya sebagai ``lexical``.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.rag.textnorm import keywords, positions, term_forms, unique

logger = get_logger(__name__)

IdfFunction = Callable[[str], float]


class BaseReranker:
    name = "none"

    def score(self, query: str, documents: Sequence[str], idf: Optional[IdfFunction] = None) -> List[float]:
        return [0.0 for _ in documents]

    def unload(self) -> None:
        return None


class NoopReranker(BaseReranker):
    name = "none"

    def score(self, query: str, documents: Sequence[str], idf: Optional[IdfFunction] = None) -> List[float]:
        # descending pseudo-scores preserving the incoming (fusion) order
        total = len(documents)
        return [1.0 - (index / max(1, total)) for index in range(total)]


def _min_window(position_lists: List[List[int]]) -> int:
    """Rentang terpendek (dalam kata bermakna) yang memuat minimal satu posisi tiap daftar."""
    events = sorted((position, owner) for owner, items in enumerate(position_lists) for position in items)
    need = len(position_lists)
    counts: Dict[int, int] = {}
    covered = 0
    best = 10**9
    left = 0
    for right in range(len(events)):
        owner = events[right][1]
        counts[owner] = counts.get(owner, 0) + 1
        if counts[owner] == 1:
            covered += 1
        while covered == need:
            best = min(best, events[right][0] - events[left][0] + 1)
            left_owner = events[left][1]
            counts[left_owner] -= 1
            if counts[left_owner] == 0:
                covered -= 1
            left += 1
    return best


class LexicalReranker(BaseReranker):
    """Reranker leksikal: cakupan istilah kueri (berbobot IDF) + kedekatan + frasa + kepadatan.

    Skornya **absolut** di rentang 0..1 - bukan dinormalkan ke kandidat terbaik. Itu yang
    membuat ambang relevansi bermakna: dulu kandidat terbaik selalu bernilai 1.0, sehingga
    pertanyaan di luar knowledge tetap "lolos" dan dijawab dengan konteks yang tidak relevan.

    Bobot IDF diambil dari SELURUH knowledge tenant (bila diberikan): kata yang langka di
    seluruh knowledge menentukan; kata yang ada di mana-mana hampir tidak berbobot. Kata kueri
    yang tidak ada di knowledge sama sekali tetap berbobot penuh, sehingga pertanyaan tentang
    hal yang tidak pernah diunggah mendapat cakupan rendah.
    """

    name = "lexical"

    def score(self, query: str, documents: Sequence[str], idf: Optional[IdfFunction] = None) -> List[float]:
        if not documents:
            return []
        query_tokens = unique(keywords(query))
        if not query_tokens:
            # Kueri tanpa kata bermakna ("Apa itu?", "Contohnya?"): tidak ada bukti kecocokan kata.
            # Dulu diberi skor menurun palsu (1,0; 0,99; ...) sehingga selalu lolos gerbang
            # relevansi dan potongan acak dikirim ke model. Penentunya kini kemiripan makna (bila
            # embedder semantik), atau riwayat percakapan untuk pertanyaan lanjutan.
            return [0.0] * len(documents)

        forms = {token: term_forms(token) for token in query_tokens}
        doc_maps = [positions(document) for document in documents]
        doc_lengths = [max(1, len(keywords(document))) for document in documents]

        if idf is None:
            # Tanpa statistik korpus: IDF dari himpunan kandidat.
            count = len(doc_maps)

            def idf(term: str) -> float:  # noqa: F811 - pengganti lokal
                frequency = sum(1 for table in doc_maps if term in table)
                return math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))

        weights = {token: max(0.05, min(idf(form) for form in forms[token])) for token in query_tokens}
        total_weight = sum(weights.values()) or 1.0

        scores: List[float] = []
        for table, length in zip(doc_maps, doc_lengths):
            matched: Dict[str, List[int]] = {}
            strength: Dict[str, float] = {}
            for token in query_tokens:
                if token in table:
                    matched[token] = table[token]
                    strength[token] = 1.0
                    continue
                hits = sorted({position for form in forms[token][1:] for position in table.get(form, [])})
                if hits:
                    # Cocok lewat bentuk dasar ("pengajuan" vs "diajukan"): sah, sedikit di bawah persis.
                    matched[token] = hits
                    strength[token] = 0.85
            if not matched:
                scores.append(0.0)
                continue

            coverage = sum(weights[token] * strength[token] for token in matched) / total_weight

            if len(query_tokens) == 1:
                proximity = 1.0
                phrase = 1.0
            else:
                proximity = 0.0
                if len(matched) >= 2:
                    window = _min_window(list(matched.values()))
                    proximity = min(1.0, len(matched) / max(1, window))
                pairs = list(zip(query_tokens, query_tokens[1:]))
                adjacent = 0
                for first, second in pairs:
                    if first in matched and second in matched:
                        later = set(matched[second])
                        if any(position + 1 in later or position + 2 in later for position in matched[first]):
                            adjacent += 1
                phrase = adjacent / len(pairs)

            occurrences = sum(len(items) for items in matched.values())
            density = min(1.0, (occurrences / length) * 8)

            raw = 0.6 * coverage + 0.15 * proximity + 0.15 * phrase + 0.10 * density
            scores.append(round(min(1.0, raw), 6))
        return scores


class CrossEncoderReranker(BaseReranker):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.name = f"sentence_transformers:{settings.reranker_model}"
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            started = time.perf_counter()
            self._model = CrossEncoder(self._settings.reranker_model, device=self._settings.reranker_device)
            logger.info("loaded reranker %s in %.1fs", self._settings.reranker_model, time.perf_counter() - started)
        return self._model

    def score(self, query: str, documents: Sequence[str], idf: Optional[IdfFunction] = None) -> List[float]:
        if not documents:
            return []
        model = self._load()
        pairs = [(query, document) for document in documents]
        raw = model.predict(pairs, batch_size=8, show_progress_bar=False)
        return [_sigmoid(float(value)) if not 0.0 <= float(value) <= 1.0 else float(value) for value in raw]

    def unload(self) -> None:
        self._model = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass


class FastEmbedReranker(BaseReranker):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # fastembed ships ONNX cross-encoders; bge-reranker-v2-m3 has no ONNX port,
        # so this profile uses the closest multilingual model and says so in /ready.
        self._model_name = settings.reranker_fastembed_model
        self.name = f"fastembed:{self._model_name}"
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(model_name=self._model_name)
            logger.info("loaded fastembed reranker %s", self._model_name)
        return self._model

    def score(self, query: str, documents: Sequence[str], idf: Optional[IdfFunction] = None) -> List[float]:
        if not documents:
            return []
        model = self._load()
        # Batch kecil + teks dipotong: cross-encoder memakai memori sebanding (batch x panjang^2);
        # 30 pasangan panjang sekaligus pernah memakan >7 GB RAM.
        limit = max(200, int(getattr(self._settings, "reranker_max_chars", 2000)))
        batch = max(1, int(getattr(self._settings, "reranker_batch_size", 8)))
        texts = [str(document)[:limit] for document in documents]
        return [_sigmoid(float(value)) for value in model.rerank(query, texts, batch_size=batch)]

    def unload(self) -> None:
        self._model = None


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + pow(2.718281828459045, -value))
    exp = pow(2.718281828459045, value)
    return exp / (1.0 + exp)


def _neural_available(provider: str) -> bool:
    """Apakah pustaka yang dibutuhkan provider neural benar-benar terpasang di image ini?"""
    try:
        if provider == "sentence_transformers":
            import sentence_transformers  # noqa: F401

            return True
        if provider == "fastembed":
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401

            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def build_reranker(settings: Settings) -> BaseReranker:
    """Bangun reranker sesuai setelan, dengan penurunan yang jujur ke ``lexical``.

    Provider neural yang pustakanya tidak terpasang dulu membuat pemuatan model gagal (atau
    diam-diam berakhir ``none``, yang bukan reranker sama sekali). Sekarang: turun ke reranker
    leksikal dan **laporkan** penggantinya lewat log, supaya operator tahu kualitas yang
    benar-benar dipakai - bukan mengira sudah memakai model neural.
    """
    provider = settings.reranker_provider
    if provider in ("sentence_transformers", "fastembed"):
        if _neural_available(provider):
            if provider == "sentence_transformers":
                return CrossEncoderReranker(settings)
            return FastEmbedReranker(settings)
        if settings.reranker_fallback_to_lexical:
            logger.warning(
                "reranker '%s' diminta tetapi pustakanya tidak terpasang; memakai reranker leksikal",
                provider,
            )
            return LexicalReranker()
        return NoopReranker()
    if provider == "lexical":
        return LexicalReranker()
    return NoopReranker()


class RerankerService:
    """Lazy, thread-safe, unloadable wrapper."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._reranker: Optional[BaseReranker] = None

    @property
    def reranker(self) -> BaseReranker:
        with self._lock:
            if self._reranker is None:
                self._reranker = build_reranker(self._settings)
            return self._reranker

    @property
    def name(self) -> str:
        return self.reranker.name

    def rerank(
        self,
        query: str,
        candidates: Sequence[Tuple[str, str]],
        *,
        top_k: int,
        threshold: Optional[float] = None,
        idf: Optional[IdfFunction] = None,
    ) -> List[Tuple[str, str, float]]:
        """``candidates`` = [(chunk_id, content)] -> [(chunk_id, content, score)]."""
        if not candidates:
            return []
        try:
            scores = self.reranker.score(query, [content for _, content in candidates], idf=idf)
        except Exception as exc:  # noqa: BLE001
            raise AppError("RERANK_FAILED", f"Reranker failed: {exc}") from exc
        scored = [
            (chunk_id, content, float(score))
            for (chunk_id, content), score in zip(candidates, scores)
        ]
        scored.sort(key=lambda item: item[2], reverse=True)
        if threshold is not None:
            scored = [item for item in scored if item[2] >= threshold]
        return scored[:top_k]

    def unload(self) -> None:
        with self._lock:
            if self._reranker is not None:
                self._reranker.unload()

    def health(self) -> str:
        try:
            self.reranker.score("health probe", ["health probe document"])
            return "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("reranker health check failed: %s", exc)
            return "error"
