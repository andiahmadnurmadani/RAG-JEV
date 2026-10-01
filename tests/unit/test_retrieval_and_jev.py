"""Hybrid retrieval: fusion, lexical isolation, capability policy (PRD 14, 15, 18, 19)."""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.jev.policies import RouteDecision, coerce_capability, heuristic_route, sanitize_route_payload
from app.jev.router import JevRouter
from app.rag.retriever import fuse
from app.rag.sparse import SparseIndex, tokenize


# --------------------------------------------------------------------------- #
# Fusion
# --------------------------------------------------------------------------- #
def test_rrf_prefers_a_chunk_that_both_retrievers_agree_on():
    dense = [("c_dense_only", 0.91), ("c_both", 0.88)]
    sparse = [("c_both", 12.0), ("c_lexical_only", 9.0)]
    fused = fuse(dense, sparse, k=60)
    assert fused[0][0] == "c_both"
    scores = {chunk_id: score for chunk_id, score in fused}
    assert scores["c_both"] > scores["c_dense_only"] > 0


def test_fusion_with_a_single_retriever_still_ranks():
    fused = fuse([("a", 0.9), ("b", 0.5)], [], k=60)
    assert [chunk_id for chunk_id, _ in fused] == ["a", "b"]


def test_fusion_of_empty_lists_is_empty():
    assert fuse([], [], k=60) == []


# --------------------------------------------------------------------------- #
# Lexical index isolation
# --------------------------------------------------------------------------- #
def test_bm25_never_returns_another_tenants_chunks(settings):
    index = SparseIndex(settings)
    index.upsert("org_a", "kb_hr", [("chunk_a1", "doc_a", "prosedur cuti tahunan karyawan tetap")])
    index.upsert("org_b", "kb_hr", [("chunk_b1", "doc_b", "prosedur cuti tahunan karyawan tetap")])

    hits_a = index.search("prosedur cuti tahunan", "org_a", "kb_hr", top_k=10)
    hits_b = index.search("prosedur cuti tahunan", "org_b", "kb_hr", top_k=10)

    assert [key for key, _ in hits_a] == ["doc_a::chunk_a1"]
    assert [key for key, _ in hits_b] == ["doc_b::chunk_b1"]


def test_bm25_keys_are_document_qualified(settings):
    """Two documents both start at ``chunk_0001``; the score map must keep both."""
    index = SparseIndex(settings)
    index.upsert("org_a", "kb_hr", [("chunk_0001", "doc_1", "prosedur cuti tahunan")])
    index.upsert("org_a", "kb_hr", [("chunk_0001", "doc_2", "prosedur cuti tahunan")])
    hits = index.search("prosedur cuti tahunan", "org_a", "kb_hr", top_k=10)
    assert sorted(key for key, _ in hits) == ["doc_1::chunk_0001", "doc_2::chunk_0001"]


def test_bm25_is_scoped_per_knowledge_base(settings):
    index = SparseIndex(settings)
    index.upsert("org_a", "kb_hr", [("chunk_hr", "doc_hr", "kebijakan cuti")])
    index.upsert("org_a", "kb_it", [("chunk_it", "doc_it", "kebijakan cuti")])
    hits = index.search("kebijakan cuti", "org_a", "kb_it", top_k=10)
    assert [key for key, _ in hits] == ["doc_it::chunk_it"]


def test_removing_a_document_purges_its_lexical_entries(settings):
    index = SparseIndex(settings)
    index.upsert("org_a", "kb_hr", [("chunk_1", "doc_1", "cuti"), ("chunk_2", "doc_2", "lembur")])
    removed = index.remove_document("org_a", "kb_hr", "doc_1")
    assert removed == 1
    assert all(entry.document_id != "doc_1" for entry in index._scope("org_a", "kb_hr").entries)
    assert index.document_ids("org_a", "kb_hr") == ["doc_2"]


def test_tokenizer_keeps_identifiers_that_lexical_search_exists_for():
    assert "iso-27001" in tokenize("Audit ISO-27001 dan SOP-12/2026")
    assert "sop-12" in tokenize("Audit ISO-27001 dan SOP-12/2026")


# --------------------------------------------------------------------------- #
# Jev policy
# --------------------------------------------------------------------------- #
def test_jev_payload_tenant_fields_are_stripped():
    cleaned, stripped = sanitize_route_payload(
        {"route": "knowledge_extract", "organization_id": "org_b", "user_id": "u1", "confidence": 0.9}
    )
    assert "organization_id" not in cleaned and "user_id" not in cleaned
    assert set(stripped) == {"organization_id", "user_id"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("knowledge_extract", "knowledge_extract"),
        ("extract", "knowledge_extract"),
        ("RAG", "knowledge_query"),
        ("search", "knowledge_search"),
        ("ringkasan", None),
        (None, None),
        ("delete_everything", None),
    ],
)
def test_capability_coercion(raw, expected):
    assert coerce_capability(raw) == expected


def test_heuristic_route_recognises_indonesian_intents():
    assert heuristic_route("Ringkas SOP HR").capability == "knowledge_summary"
    assert heuristic_route("Ambil data penjualan bulanan").capability == "knowledge_extract"
    assert heuristic_route("Bagaimana prosedur cuti tahunan?").capability == "knowledge_query"


def test_router_degrades_to_heuristic_when_jev_fails(settings):
    class ExplodingClient:
        enabled = True
        last_error = None

        def call_tool(self, name, arguments=None):  # noqa: ANN001
            raise AppError("JEV_FAILED", "credentials rejected")

        def structured_content(self, result):  # noqa: ANN001
            return {}

    router = JevRouter(settings.model_copy(update={"jev_enabled": True}), ExplodingClient())
    decision = router.route("Ambil data penjualan", tenant_context={"application_id": "app_a"})
    assert decision.source == "fallback"
    assert decision.capability == "knowledge_extract"
    assert "jev_error" in decision.reason
    assert router.status == "error"


def test_router_refuses_to_let_jev_pick_the_tenant(settings):
    class SpoofingClient:
        enabled = True
        last_error = None

        def call_tool(self, name, arguments=None):  # noqa: ANN001
            assert "organization_id" not in (arguments or {}).get("context", {})
            return {"structuredContent": {"route": "knowledge_search", "organization_id": "org_victim"}}

        def structured_content(self, result):  # noqa: ANN001
            return dict(result["structuredContent"])

    router = JevRouter(settings.model_copy(update={"jev_enabled": True}), SpoofingClient())
    decision = router.route("cari SOP", tenant_context={"application_id": "app_a", "organization_id": "org_a"})
    assert decision.capability == "knowledge_search"
    assert "organization_id" in decision.stripped_fields
    assert decision.to_dict()["stripped_tenant_fields"] == ["organization_id"]


def test_route_decision_reports_generation_and_rerank_policy():
    assert RouteDecision("knowledge_search", "jev").generates_answer is False
    assert RouteDecision("knowledge_query", "jev").uses_reranker is True
