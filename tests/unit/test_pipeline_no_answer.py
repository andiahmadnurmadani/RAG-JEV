"""Threshold / no-answer behaviour with a stubbed retriever (PRD 15, 16, 40)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.core.errors import AppError
from app.rag.pipeline import RagPipeline
from app.rag.retriever import Candidate, RetrievalResult


class StubRetriever:
    """Kontrak retriever: gerbang relevansi (``relevant``) diputuskan oleh retriever sendiri."""

    def __init__(self, candidates: List[Candidate], *, reranked: bool = True, minimum: float = 0.35) -> None:
        self._candidates = candidates
        self._reranked = reranked
        self._minimum = minimum
        self.calls = 0

    def retrieve(self, **kwargs) -> RetrievalResult:
        self.calls += 1
        best = max((candidate.score for candidate in self._candidates), default=0.0)
        relevant = bool(self._candidates) and (not self._reranked or best >= self._minimum)
        return RetrievalResult(
            candidates=list(self._candidates),
            dense_hits=len(self._candidates),
            sparse_hits=0,
            fused_count=len(self._candidates),
            reranked=self._reranked,
            best_score=best,
            relevant=relevant,
            gate="lexical" if relevant else "below_min_relevance",
        )


class CountingGenerator:
    """Stands in for the LLM: records whether it was ever called."""

    model = "stub-llm"

    def __init__(self) -> None:
        self.calls = 0

    def answer(self, *, query, context, strict_grounding=True, no_candidate_reason=None):  # noqa: ANN001
        self.calls += 1
        from app.rag.generator import GeneratedAnswer, LLMUsage

        return GeneratedAnswer(answer="jawaban dari konteks [1]", grounded=True, usage=LLMUsage(model=self.model))

    def extract(self, *, query, context, output_schema=None):  # noqa: ANN001
        self.calls += 1
        return {"items": [], "not_found": True}

    def health(self) -> str:
        return "ok"


class StubJev:
    status = "disabled"

    def route(self, query: str, *, tenant_context: Dict[str, Any]):  # noqa: ANN001
        from app.jev.policies import RouteDecision

        return RouteDecision(capability="knowledge_query", source="heuristic", reason="stub")

    def health(self) -> str:
        return "disabled"


class Tenant:
    organization_id = "org_a"
    application_id = "app_a"
    user_id = "user_a"
    permissions = ["read", "write"]

    def as_dict(self) -> Dict[str, Any]:
        return {"organization_id": self.organization_id, "application_id": self.application_id, "user_id": self.user_id}

    def require(self, permission: str) -> None:
        return None


def _candidate(score: float) -> Candidate:
    return Candidate(
        chunk_id="chunk_0001",
        document_id="doc_1",
        content="Prosedur cuti tahunan: pengajuan melalui sistem HR minimal tujuh hari sebelumnya.",
        document_name="SOP Cuti.pdf",
        page=12,
        score=score,
    )


def test_below_threshold_never_reaches_the_llm(settings):
    generator = CountingGenerator()
    pipeline = RagPipeline(settings, StubRetriever([_candidate(0.12)], minimum=0.5), generator, StubJev())

    result = pipeline.answer(query="prosedur cuti", context=Tenant(), knowledge_base_id="kb_hr")

    assert result.grounded is False
    assert result.sources == []
    assert result.no_answer_reason == "below_threshold"
    assert generator.calls == 0, "an ungrounded answer must not be produced by the model"


def test_above_threshold_produces_a_cited_answer(settings):
    generator = CountingGenerator()
    pipeline = RagPipeline(settings, StubRetriever([_candidate(0.82)], minimum=0.3), generator, StubJev())

    result = pipeline.answer(query="prosedur cuti", context=Tenant(), knowledge_base_id="kb_hr")

    assert result.grounded is True
    assert generator.calls == 1
    assert result.sources and result.sources[0]["document_id"] == "doc_1"
    assert result.sources[0]["page"] == 12
    assert result.usage["context_tokens"] > 0


def test_no_candidates_short_circuits(settings):
    generator = CountingGenerator()
    pipeline = RagPipeline(settings, StubRetriever([], reranked=False), generator, StubJev())

    result = pipeline.answer(query="apa saja", context=Tenant(), knowledge_base_id="kb_hr")

    assert result.no_answer_reason == "no_candidates"
    assert generator.calls == 0
    assert result.sources == []


def test_strict_grounding_drops_a_non_committal_answer(settings):
    class RefusingGenerator(CountingGenerator):
        def answer(self, *, query, context, strict_grounding=True, no_candidate_reason=None):  # noqa: ANN001
            self.calls += 1
            from app.rag.generator import GeneratedAnswer, LLMUsage

            return GeneratedAnswer(
                answer="Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia.",
                grounded=False,
                usage=LLMUsage(model=self.model),
            )

    generator = RefusingGenerator()
    pipeline = RagPipeline(settings, StubRetriever([_candidate(0.9)], minimum=0.1), generator, StubJev())

    result = pipeline.answer(
        query="prosedur cuti", context=Tenant(), knowledge_base_id="kb_hr", strict_grounding=True
    )
    assert result.grounded is False
    assert result.sources == []
    assert result.no_answer_reason == "strict_grounding"


def test_route_hint_bypasses_jev_without_touching_tenant_data(settings):
    pipeline = RagPipeline(settings, StubRetriever([_candidate(0.9)]), CountingGenerator(), StubJev())
    decision = pipeline.route("cari apa saja", Tenant(), hint="extract")
    assert decision.capability == "knowledge_extract"
    assert decision.source == "request_hint"
