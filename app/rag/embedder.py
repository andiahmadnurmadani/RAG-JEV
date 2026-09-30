"""Embedding providers (PRD 8.2 — BGE-M3 with workable fallbacks).

Provider choice is a deployment decision, not a code change:

* ``sentence_transformers`` — PRD default, ``BAAI/bge-m3`` (torch, ~2.3 GB RAM).
* ``fastembed``            — ONNX int8 multilingual model; fits low-RAM boxes.
* ``http``                 — external OpenAI-compatible ``/embeddings`` service.
* ``hash``                 — dependency-free deterministic vectors, used by tests
                             and by ``/ready`` when no model is downloadable.

Every provider returns L2-normalised vectors so the distance metric is cosine.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import List, Optional, Sequence

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

E5_FAMILIES = ("multilingual-e5", "e5-small", "e5-base", "e5-large", "bge-m3")


def normalize(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in vector))
    if norm == 0:
        return [0.0 for _ in vector]
    return [float(v) / norm for v in vector]


def _prefix_for(model_name: str, is_query: bool, provider: str) -> str:
    """E5-family models expect 'query:'/'passage:' prefixes; BGE does not."""
    if provider != "fastembed":
        return ""
    if "e5" in model_name.lower():
        return "query: " if is_query else "passage: "
    return ""


class BaseEmbedder:
    name = "base"
    dim = 0

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        raise NotImplementedError

    def unload(self) -> None:
        return None

    def health(self) -> str:
        return "ok"


class HashEmbedder(BaseEmbedder):
    """Deterministic bag-of-words hashing. No downloads, no RAM cost."""

    name = "hash"
    dim = 384

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        vectors: List[List[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for token in re.findall(r"[\w\-]+", (text or "").lower()):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            vectors.append(normalize(vector))
        return vectors


class SentenceTransformerEmbedder(BaseEmbedder):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.name = f"sentence_transformers:{settings.embedding_model}"
        self._model = None
        self.dim = settings.embedding_dim

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # local import: heavy

            started = time.perf_counter()
            self._model = SentenceTransformer(self._settings.embedding_model, device=self._settings.embedding_device)
            self._model.max_seq_length = self._settings.embedding_max_length
            self.dim = int(self._model.get_sentence_embedding_dimension())
            logger.info("loaded embedding model %s in %.1fs", self._settings.embedding_model, time.perf_counter() - started)
        return self._model

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        model = self._load()
        vectors = model.encode(
            list(texts),
            batch_size=self._settings.embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [list(map(float, vector)) for vector in vectors]

    def unload(self) -> None:
        self._model = None
        try:
            import gc

            gc.collect()
        except Exception:  # noqa: BLE001
            pass


class FastEmbedEmbedder(BaseEmbedder):
    """ONNX int8 embeddings — the low-RAM profile of this deployment."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model_name = settings.embedding_fastembed_model
        self.name = f"fastembed:{self._model_name}"
        self._model = None
        self.dim = settings.embedding_dim

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            started = time.perf_counter()
            self._model = TextEmbedding(model_name=self._model_name)
            probe = next(iter(self._model.embed(["dimension probe"])))
            self.dim = len(probe)
            logger.info("loaded fastembed %s (dim=%d) in %.1fs", self._model_name, self.dim, time.perf_counter() - started)
        return self._model

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        model = self._load()
        prefix = _prefix_for(self._model_name, is_query, "fastembed")
        payload = [f"{prefix}{text}" for text in texts]
        return [normalize(list(map(float, vector))) for vector in model.embed(payload)]

    def unload(self) -> None:
        self._model = None


class HttpEmbedder(BaseEmbedder):
    """Any OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.name = f"http:{settings.embedding_model}"
        self.dim = settings.embedding_dim

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        import httpx

        url = self._settings.llm_base_url.rstrip("/") + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self._settings.llm_api_key}"
        body = {"model": self._settings.embedding_model, "input": list(texts)}
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=60.0)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AppError("EMBEDDING_FAILED", f"Embedding endpoint failed: {exc}") from exc
        vectors = [normalize(item["embedding"]) for item in payload["data"]]
        if not self.dim:
            self.dim = len(vectors[0]) if vectors else 0
        return vectors


def build_embedder(settings: Settings) -> BaseEmbedder:
    provider = settings.embedding_provider
    if provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings)
    if provider == "fastembed":
        return FastEmbedEmbedder(settings)
    if provider == "http":
        return HttpEmbedder(settings)
    return HashEmbedder()


class EmbedderService:
    """Thread-safe facade: lazily loads, can unload for sequential residency."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._embedder: Optional[BaseEmbedder] = None

    @property
    def embedder(self) -> BaseEmbedder:
        with self._lock:
            if self._embedder is None:
                self._embedder = build_embedder(self._settings)
            return self._embedder

    @property
    def dim(self) -> int:
        return self.embedder.dim

    @property
    def name(self) -> str:
        return self.embedder.name

    def encode(self, texts: Sequence[str], *, is_query: bool = False) -> List[List[float]]:
        if not texts:
            return []
        return self.embedder.encode(texts, is_query=is_query)

    def unload(self) -> None:
        with self._lock:
            if self._embedder is not None:
                self._embedder.unload()

    def health(self) -> str:
        try:
            self.encode(["health probe"], is_query=True)
            return "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding health check failed: %s", exc)
            return "error"
