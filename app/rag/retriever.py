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

    @property
    def count(self) -> int:
        return len(self.candidates)


def fused_key(document_id: str, chunk_id: str) -> str:
    """Identity of a chunk: ``chunk_id`` alone repeats across documents (``chunk_0001``)."""
    return f"{document_id}::{chunk_id}"


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
        final_k: int = 5,
        dense_top_k: Optional[int] = None,
        sparse_top_k: Optional[int] = None,
        use_hybrid: Optional[bool] = None,
        use_reranker: Optional[bool] = None,
        threshold: Optional[float] = None,
        document_ids: Optional[Sequence[str]] = None,
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
        if settings.retrieval_dense_enabled:
            vector = self._embedder.encode([query], is_query=True)[0]
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

        fused = fuse(dense_hits, sparse_hits, k=settings.rrf_k, dense_weight=settings.dense_weight)
        result.fused_count = len(fused)
        if not fused:
            return result

        # Re-read payloads through the tenant filter: nothing reaches the LLM
        # that was not verified to belong to this organization.
        payloads = repository.get_chunks_by_ids(
            settings,
            chunk_ids=[key for key, _ in fused],
            organization_id=organization_id,
            knowledge_base_id=knowledge_base_id,
            document_ids=scope,
        )

        candidates: List[Candidate] = []
        dense_scores = dict(dense_hits)
        sparse_scores = dict(sparse_hits)
        for key, fused_score in fused:
            payload = payloads.get(key)
            if payload is None:
                continue
            if knowledge_base_id and payload.get("knowledge_base_id") != knowledge_base_id:
                continue
            if scope is not None and payload.get("document_id") not in scope:
                continue
            chunk_id = payload.get("chunk_id", "")
            dense_score = dense_scores.get(key)
            sparse_score = sparse_scores.get(key)
            candidates.append(
                Candidate(
                    chunk_id=chunk_id,
                    document_id=payload.get("document_id", ""),
                    content=payload.get("content", ""),
                    document_name=payload.get("document_name", ""),
                    page=payload.get("page"),
                    section=payload.get("section", ""),
                    source_url=payload.get("source_url", ""),
                    language=payload.get("language", ""),
                    score=fused_score,
                    dense_score=dense_score,
                    sparse_score=sparse_score,
                    fused_score=fused_score,
                    is_summary=bool(payload.get("is_summary")),
                )
            )

        if not candidates:
            return result

        candidates = _dedupe_by_document(candidates, settings.max_chunks_per_document)

        if rerank:
            rerank_started = time.perf_counter()
            candidates = self._apply_rerank(query, candidates, final_k=final_k, threshold=threshold)
            result.rerank_ms = round((time.perf_counter() - rerank_started) * 1000, 2)
            # a no-op reranker must not be reported (or counted) as a real rerank
            result.reranked = settings.reranker_provider != "none"
            result.reranker_used = self._reranker.name
        else:
            candidates = candidates[:final_k]

        result.candidates = candidates
        result.best_score = max((candidate.score for candidate in candidates), default=0.0)
        return result

    # ------------------------------------------------------------------ #
    def _apply_rerank(
        self,
        query: str,
        candidates: Sequence[Candidate],
        *,
        final_k: int,
        threshold: float,
    ) -> List[Candidate]:
        pool = list(candidates)[: self._settings.reranker_candidates]
        scored = self._reranker.rerank(
            query,
            [(fused_key(candidate.document_id, candidate.chunk_id), candidate.content) for candidate in pool],
            top_k=len(pool),
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
            return ordered[:final_k]
        return [candidate for candidate in ordered if candidate.score >= threshold][:final_k]

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
