"""Jawaban yang terpotong batas token keluaran tidak boleh dilaporkan "tidak ditemukan".

Kasus nyatanya: dokumen besar sudah seluruhnya masuk konteks, tetapi model berhenti di tengah
jawaban (finish_reason="length") sehingga teksnya kosong. Tanpa pemeriksaan ini jawabannya
menjadi "Informasi tersebut tidak ditemukan dalam knowledge base" - dan pemakainya menyimpulkan
datanya tidak ada, lalu menambah dokumen yang sebenarnya sudah terbaca.
"""

from __future__ import annotations

import base64

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_truncated"


class _TruncatingClient:
    """Klien LLM uji: selalu berhenti karena batas token, kadang tanpa teks sama sekali."""

    def __init__(self, *, text: str = "") -> None:
        self.model = "uji-pemotong"
        self._text = text
        self.calls = 0

    def chat(self, messages, **kwargs):
        from app.rag.generator import LLMUsage

        self.calls += 1
        limit = kwargs.get("max_tokens") or 4096
        return self._text, LLMUsage(
            input_tokens=100,
            output_tokens=limit,
            latency_ms=1.0,
            model=self.model,
            finish_reason="length",
        )

    def health(self) -> str:
        return "ok"


def _index(client, text: str, document_id: str = "doc_trunc") -> dict:
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": document_id,
            "document_name": "besar.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202, response.text
    return wait_for_job(client, document_id)


def _document(sections: int = 20) -> str:
    return "\n\n".join(
        f"## Bagian_{index:02d}\nBagian {index} memuat daftar kolom " + ", ".join(f"kolom_{index}_{n}" for n in range(1, 8))
        for index in range(1, sections + 1)
    )


def test_a_truncated_answer_is_reported_as_truncated_not_as_not_found(settings, client):
    from app.api.deps import build_services
    from app.core.tenant import TrustedContext

    _index(client, _document())
    services = build_services(settings)
    services.rag._generator.rebind_llm_client(_TruncatingClient(text=""))

    result = services.rag.answer(
        query="Sebutkan semua kolom yang ada.",
        context=TrustedContext(
            user_id="user_a", organization_id="org_a", application_id="app_a", permissions=["read", "write"]
        ),
        knowledge_base_id=KB,
        top_k=4,
    )

    assert result.no_answer_reason == "answer_truncated", result.no_answer_reason
    assert "LLM_MAX_TOKENS" in result.answer
    assert "terpotong" in result.answer
    assert "tidak ditemukan" not in result.answer.lower()
    # Kelengkapan konteks tetap dilaporkan: yang salah bukan knowledge-nya.
    assert result.usage["context_chunks"] > 0
    assert result.usage["finish_reason"] == "length"
    assert result.document_coverage and result.document_coverage[0]["complete"] is True


def test_the_partial_answer_is_kept_and_shown(settings, client):
    from app.api.deps import build_services
    from app.core.tenant import TrustedContext

    _index(client, _document(sections=10), document_id="doc_trunc_partial")
    services = build_services(settings)
    services.rag._generator.rebind_llm_client(_TruncatingClient(text="Tabel: knowledge, badge, approval"))

    result = services.rag.answer(
        query="Sebutkan tabel yang ada.",
        context=TrustedContext(
            user_id="user_a", organization_id="org_a", application_id="app_a", permissions=["read", "write"]
        ),
        knowledge_base_id=KB,
        top_k=4,
    )

    assert result.no_answer_reason == "answer_truncated"
    assert "Tabel: knowledge, badge, approval" in result.answer
    assert "terpotong" in result.answer


def test_the_http_response_carries_the_truncated_reason(client, settings):
    """Jalur HTTP memakai services milik aplikasi, jadi klien ujinya dipasang di sana."""

    from app.api.deps import get_services

    _index(client, _document(sections=12), document_id="doc_trunc_http")
    get_services().rag._generator.rebind_llm_client(_TruncatingClient(text=""))

    response = client.post(
        "/api/v1/query",
        json={"query": "Sebutkan semua kolom.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["no_answer_reason"] == "answer_truncated"
    assert data["usage"]["finish_reason"] == "length"
    assert data["usage"]["context_chunks"] > 0
    assert "LLM_MAX_TOKENS" in data["answer"]


def test_a_normal_answer_is_not_marked_truncated(settings, client):
    """Model yang berhenti wajar (finish_reason=stop) tidak boleh ditandai terpotong."""

    from app.api.deps import build_services
    from app.core.tenant import TrustedContext
    from app.rag.generator import LLMUsage

    class _StopClient:
        model = "uji-wajar"

        def chat(self, messages, **kwargs):
            return "Ada tabel knowledge dengan kolom Knowledge_ID [1].", LLMUsage(
                input_tokens=50, output_tokens=20, latency_ms=1.0, model="uji-wajar", finish_reason="stop"
            )

        def health(self) -> str:
            return "ok"

    _index(client, _document(sections=6), document_id="doc_notrunc")
    services = build_services(settings)
    services.rag._generator.rebind_llm_client(_StopClient())

    result = services.rag.answer(
        query="Tabel apa saja?",
        context=TrustedContext(
            user_id="user_a", organization_id="org_a", application_id="app_a", permissions=["read", "write"]
        ),
        knowledge_base_id=KB,
        top_k=4,
    )
    assert result.no_answer_reason is None
    assert result.usage["finish_reason"] == "stop"
    assert "LLM_MAX_TOKENS" not in result.answer
