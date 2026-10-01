"""Setelan "dokumen besar" bisa disetel dari panel: anggaran konteks, top_k, pelengkap dokumen.

Yang dijaga di sini bukan sekadar "tersimpan", tetapi bahwa nilainya benar-benar mengikat
pipeline: menurunkan anggaran konteks harus mempersempit konteks yang dikirim ke model, dan
mematikan pelengkap dokumen harus benar-benar menghentikan pengambilan bagian tambahan.
"""

from __future__ import annotations

import base64

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job

KB = "kb_retrieval_settings"


def _index(client, text: str, document_id: str = "doc_ctx") -> dict:
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


def _document(sections: int = 60, repeats: int = 40) -> str:
    parts = ["# Dokumen besar", ""]
    for index in range(1, sections + 1):
        parts.append(f"## Bagian_{index:02d}")
        parts.append(f"Bagian {index} menjelaskan berkas Bagian_{index:02d} " + "beserta seluruh isinya " * repeats)
        parts.append("")
    return "\n".join(parts)


def _retrieval_section(client, key: str = TENANT_A_KEY) -> dict:
    data = client.get("/api/v1/settings", headers=auth(key)).json()["data"]
    return data["sections"]["retrieval"]


def test_the_retrieval_section_is_readable_with_the_defaults(client):
    section = _retrieval_section(client)
    assert section["context_max_tokens"] == 24000
    assert section["final_top_k"] == 12
    assert section["max_chunks_per_document"] == 8
    assert section["context_expand_documents"] is True


def test_only_an_admin_may_change_the_context_budget(client):
    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_max_tokens": 40000}},
        headers=auth(TENANT_B_KEY),
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["permission"] == "admin"
    assert client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_max_tokens": 40000}},
        headers=auth(READ_ONLY_KEY),
    ).status_code == 403


def test_a_role_without_admin_sees_no_retrieval_section_at_all(client):
    """Tanpa izin admin, layar setelan tidak boleh membocorkan konfigurasi layanan."""
    response = client.get("/api/v1/settings", headers=auth(READ_ONLY_KEY))
    assert response.status_code == 403


def test_the_new_budget_applies_to_the_next_query(client, settings):
    """Anggaran konteks yang disimpan benar-benar dipakai pipeline, bukan hanya tersimpan."""

    _index(client, _document())

    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_max_tokens": 2500, "max_chunks_per_document": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert "retrieval.context_max_tokens" in body["applied"]
    assert body["sections"]["retrieval"]["context_max_tokens"] == 2500
    assert settings.context_max_tokens == 2500
    assert settings.context_token_budget == 2500

    answer = client.post(
        "/api/v1/query",
        json={"query": "Ringkas isi dokumen ini.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert answer.status_code == 200, answer.text
    usage = answer.json()["data"]["usage"]
    coverage = usage["document_coverage"][0]
    assert coverage["complete"] is False, coverage
    assert usage["context_tokens"] <= 2500 + 300          # batasnya mengikat


def test_turning_off_document_expansion_stops_the_extra_chunks(client, settings):
    _index(client, _document(), document_id="doc_off_again")

    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_expand_documents": False}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    assert settings.context_expand_documents is False

    answer = client.post(
        "/api/v1/query",
        json={"query": "Ringkas isi dokumen ini.", "knowledge_base_id": KB, "options": {"top_k": 4}},
        headers=auth(TENANT_A_KEY),
    )
    usage = answer.json()["data"]["usage"]
    assert usage["context_expanded_chunks"] == 0


def test_an_absurd_budget_is_rejected_before_it_is_stored(client, settings):
    before = settings.context_max_tokens
    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_max_tokens": 10_000_000}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert settings.context_max_tokens == before


def test_an_out_of_range_top_k_is_refused_before_it_is_stored(client, settings):
    before = settings.final_top_k
    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"final_top_k": 5000}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert settings.final_top_k == before


def test_an_unknown_retrieval_field_is_rejected_not_ignored(client):
    response = client.put(
        "/api/v1/settings",
        json={"retrieval": {"context_max_token": 1000}},          # salah ketik
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_answer_token_limit_is_editable_and_bounded(client, settings):
    """Batas token jawaban menentukan jawaban panjang selesai atau terpotong."""

    assert settings.llm_max_tokens == 8192
    response = client.put(
        "/api/v1/settings",
        json={"llm": {"max_tokens": 8000}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["sections"]["llm"]["max_tokens"] == 8000
    assert settings.llm_max_tokens == 8000

    before = settings.llm_max_tokens
    too_small = client.put(
        "/api/v1/settings",
        json={"llm": {"max_tokens": 4}},
        headers=auth(TENANT_A_KEY),
    )
    assert too_small.status_code == 422
    assert too_small.json()["error"]["code"] == "VALIDATION_ERROR"
    assert settings.llm_max_tokens == before


def test_the_browser_console_exposes_the_answer_token_limit(client):
    script = client.get("/ui/app.js").text
    page = client.get("/ui/").text
    assert "llm-max-tokens" in script and "llm-max-tokens" in page


def test_the_browser_console_exposes_the_context_controls(client):
    script = client.get("/ui/app.js").text
    page = client.get("/ui/").text

    assert "s-context-tokens" in script and "saveRetrievalService" in script
    assert "retrieval" in script
    assert 'id="s-context-tokens"' in page and 'id="btn-save-retr-svc"' in page
    # Hasil jawaban harus memperlihatkan berapa bagian dokumen yang ikut ke konteks.
    assert "context_expanded_chunks" in script and "document_coverage" in script
