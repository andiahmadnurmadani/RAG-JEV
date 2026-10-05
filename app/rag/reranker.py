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
import re
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)

# Kata yang terlalu pendek tidak menandai relevansi.
MIN_TERM_LENGTH = 2


class BaseReranker:
    name = "none"

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        return [0.0 for _ in documents]

    def unload(self) -> None:
        return None


class NoopReranker(BaseReranker):
    name = "none"

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        # descending pseudo-scores preserving the incoming (fusion) order
        total = len(documents)
        return [1.0 - (index / max(1, total)) for index in range(total)]


def _tokens(text: str) -> List[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "") if len(token) >= MIN_TERM_LENGTH]


class LexicalReranker(BaseReranker):
    """Reranker lintas-encoder berbasis leksikal: IDF + frasa + kedekatan + cakupan.

    Skornya dihitung untuk kandidat yang sudah lolos pencarian, jadi biayanya kecil (tanpa
    model, tanpa jaringan) dan hasilnya deterministik - penting untuk layanan yang jawabannya
    harus bisa dipertanggungjawabkan.
    """

    name = "lexical"

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        if not documents:
            return []
        query_tokens = _tokens(query)
        if not query_tokens:
            total = len(documents)
            return [1.0 - (index / max(1, total)) for index in range(total)]

        # IDF dihitung dari himpunan kandidat: kata yang muncul di semua kandidat tidak
        # membedakan apa pun; kata yang muncul di sedikit kandidat sangat menentukan.
        doc_tokens = [_tokens(document) for document in documents]
        document_count = len(doc_tokens)
        unique_query = list(dict.fromkeys(query_tokens))
        document_frequency: Dict[str, int] = {
            token: sum(1 for tokens in doc_tokens if token in tokens) for token in unique_query
        }

        def idf(token: str) -> float:
            frequency = document_frequency.get(token, 0)
            # BM25-plus style: selalu positif, sehingga skor tidak pernah dibalik kata umum.
            return math.log((document_count + 1) / (frequency + 0.5))

        # Frasa utuh dari kueri (2-3 kata berturutan).
        phrases = [
            " ".join(unique_query[index:index + size])
            for size in (3, 2)
            for index in range(0, max(0, len(unique_query) - size + 1))
        ]
        total_weight = sum(idf(token) for token in unique_query) or 1.0

        scores: List[float] = []
        for tokens in doc_tokens:
            token_set = set(tokens)
            present = [token for token in unique_query if token in token_set]
            if not present:
                scores.append(0.0)
                continue

            coverage = sum(idf(token) for token in present) / total_weight

            # Kepadatan: berapa kali kata kueri muncul, dinormalkan panjang dokumen.
            occurrences = sum(tokens.count(token) for token in present)
            density = occurrences / (len(tokens) or 1)

            # Frasa utuh lebih kuat daripada kata yang terpisah-pisah.
            text_lower = " ".join(tokens)
            phrase_bonus = 0.0
            for phrase in phrases:
                if phrase and phrase in text_lower:
                    phrase_bonus = max(phrase_bonus, 0.25 if len(phrase.split()) >= 3 else 0.15)

            # Kedekatan: makin rapat kemunculan kata kueri, makin baik.
            positions = [index for index, token in enumerate(tokens) if token in set(present)]
            proximity = 0.0
            if len(positions) >= 2:
                span = positions[-1] - positions[0] + 1
                proximity = len(present) / max(1, span)

            raw = 0.55 * coverage + 0.15 * min(1.0, density * 12) + phrase_bonus + 0.15 * proximity
            scores.append(round(min(1.0, raw), 6))

        # Normalkan sehingga kandidat terbaik = 1.0, sama seperti sisi sparse (skor/best).
        # Dua alasan: (1) threshold relevansi (bawaan 0.35) jadi bermakna sebagai "berapa jauh
        # di bawah yang terbaik", bukan angka absolut yang bisa memotong SEMUA kandidat;
        # (2) kandidat terbaik selalu lolos threshold, sehingga reranker tidak pernah membuat
        # layanan menjawab "tidak ditemukan" padahal datanya ada.
        best = max(scores) if scores else 0.0
        if best > 0:
            scores = [round(score / best, 6) for score in scores]
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

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
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

    def score(self, query: str, documents: Sequence[str]) -> List[float]:
        if not documents:
            return []
        model = self._load()
        return [_sigmoid(float(value)) for value in model.rerank(query, list(documents))]

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
    ) -> List[Tuple[str, str, float]]:
        """``candidates`` = [(chunk_id, content)] -> [(chunk_id, content, score)]."""
        if not candidates:
            return []
        try:
            scores = self.reranker.score(query, [content for _, content in candidates])
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
