"""Multi-tenant guarantees (PRD 6, 13, 21, 34, 40 acceptance criteria)."""

from __future__ import annotations

import base64

from tests.conftest import TENANT_A_KEY, TENANT_B_KEY, READ_ONLY_KEY, auth, wait_for_job

DOC_TEXT = (
    "# SOP Cuti\n\nProsedur cuti tahunan: karyawan mengajukan cuti melalui sistem HR "
    "minimal tujuh hari sebelum tanggal mulai, dan atasan langsung menyetujui dalam dua hari kerja.\n"
)


def _index(client, *, document_id: str, text: str, key: str = TENANT_A_KEY, organization_id: str | None = None):
    payload = {
        "document_id": document_id,
        "knowledge_base_id": "kb_hr",
        "document_name": "SOP Cuti.pdf",
        "text": text,
    }
    if organization_id is not None:
        payload["organization_id"] = organization_id
    return client.post("/api/v1/knowledge/index", json=payload, headers=auth(key))


def test_org_b_cannot_see_org_a_knowledge(client):
    assert _index(client, document_id="doc_a1", text=DOC_TEXT).status_code == 202
    completed = wait_for_job(client, "doc_a1")
    assert completed["status"] == "completed"
    assert completed["vectors_in_store"] > 0

    response = client.post(
        "/api/v1/query",
        json={"query": "Bagaimana prosedur cuti tahunan?", "knowledge_base_id": "kb_hr"},
        headers=auth(TENANT_B_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sources"] == []
    assert data["grounded"] is False
    assert "tidak ditemukan" in data["answer"].lower()
    assert data["no_answer_reason"] == "no_candidates"

    search = client.post(
        "/api/v1/search",
        json={"query": "prosedur cuti tahunan", "knowledge_base_id": "kb_hr"},
        headers=auth(TENANT_B_KEY),
    )
    assert search.json()["data"]["results"] == []


def test_org_a_gets_its_own_knowledge_with_citations(client):
    assert _index(client, document_id="doc_a2", text=DOC_TEXT).status_code == 202
    assert wait_for_job(client, "doc_a2")["status"] == "completed"

    response = client.post(
        "/api/v1/query",
        json={"query": "Bagaimana prosedur cuti tahunan?", "knowledge_base_id": "kb_hr"},
        headers=auth(TENANT_A_KEY),
    )
    data = response.json()["data"]
    assert data["grounded"] is True
    assert data["sources"], "a grounded answer must cite at least one chunk"
    assert data["sources"][0]["document_id"] == "doc_a2"
    for source in data["sources"]:
        assert source["chunk_id"].startswith("chunk_")


def test_client_supplied_organization_id_is_rejected_on_query(client):
    response = client.post(
        "/api/v1/query",
        json={
            "query": "prosedur cuti",
            "knowledge_base_id": "kb_hr",
            "options": {"organization_id": "org_b"},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_indexing_with_a_foreign_organization_id_is_forbidden(client):
    response = _index(client, document_id="doc_evil", text=DOC_TEXT, organization_id="org_b")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_read_only_key_cannot_index_or_delete(client):
    assert _index(client, document_id="doc_ro", text=DOC_TEXT, key=READ_ONLY_KEY).status_code == 403
    assert client.delete("/api/v1/knowledge/doc_ro", headers=auth(READ_ONLY_KEY)).status_code == 403


def test_missing_or_invalid_credentials_are_refused_early(client):
    assert client.post("/api/v1/query", json={"query": "x", "knowledge_base_id": "kb_hr"}).status_code == 401
    bad = client.post(
        "/api/v1/query",
        json={"query": "x", "knowledge_base_id": "kb_hr"},
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "AUTH_INVALID"


def test_org_b_cannot_delete_org_a_document(client):
    assert _index(client, document_id="doc_a3", text=DOC_TEXT).status_code == 202
    assert wait_for_job(client, "doc_a3")["status"] == "completed"
    response = client.delete("/api/v1/knowledge/doc_a3", headers=auth(TENANT_B_KEY))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    # the document is still alive for its owner
    assert wait_for_job(client, "doc_a3")["status"] == "completed"


def test_document_status_is_not_readable_across_tenants(client):
    assert _index(client, document_id="doc_a4", text=DOC_TEXT).status_code == 202
    assert wait_for_job(client, "doc_a4")["status"] == "completed"
    assert client.get("/api/v1/knowledge/doc_a4", headers=auth(TENANT_B_KEY)).status_code == 404


def test_base64_upload_is_validated_and_indexed(client):
    encoded = base64.b64encode(("Kebijakan absensi: keterlambatan dicatat oleh atasan." * 5).encode()).decode()
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_b64",
            "knowledge_base_id": "kb_hr",
            "document_name": "absensi.txt",
            "content_base64": encoded,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    assert wait_for_job(client, "doc_b64")["status"] == "completed"


def test_unsupported_upload_type_fails_fast(client):
    """Ditolak sebelum antre: tidak ada job, tidak ada byte yang dibaca."""
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_bad",
            "knowledge_base_id": "kb_hr",
            "document_name": "payload.exe",
            "text": "MZ binary pretending to be text",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    status = client.get("/api/v1/knowledge/doc_bad", headers=auth(TENANT_A_KEY))
    assert status.status_code == 404  # job bahkan tidak pernah dibuat


def test_kms_context_token_provides_the_tenant(settings):
    from app.core.security import issue_context_token, verify_context_token

    token = issue_context_token(
        settings,
        {"user_id": "u9", "organization_id": "org_kms", "application_id": "app_kms", "permissions": ["read"]},
    )
    payload = verify_context_token(settings, token)
    assert payload["organization_id"] == "org_kms"

    from app.api.middleware.auth import resolve_context

    context = resolve_context(settings, None, token)
    assert context.organization_id == "org_kms"
    assert context.source == "kms_token"


def test_forged_context_token_is_rejected(settings):
    import pytest

    from app.core.errors import AppError
    from app.core.security import verify_context_token

    with pytest.raises(AppError) as excinfo:
        verify_context_token(settings, "aaa.bbb.ccc")
    assert excinfo.value.code == "AUTH_INVALID"
