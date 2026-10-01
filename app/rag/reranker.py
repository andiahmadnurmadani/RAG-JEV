"""Reranking (PRD 8.3, 15).

Providers
---------
* ``sentence_transformers`` — PRD default ``BAAI/bge-reranker-v2-m3`` (CrossEncoder).
* ``fastembed``            — ONNX cross-encoder, light-RAM profile.
* ``none``                 — keep fusion order (rerank is then a no-op, reported as such).
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)


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


def build_reranker(settings: Settings) -> BaseReranker:
    if settings.reranker_provider == "sentence_transformers":
        return CrossEncoderReranker(settings)
    if settings.reranker_provider == "fastembed":
        return FastEmbedReranker(settings)
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
