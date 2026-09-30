"""Document listing and document-scoped prompting (the two controls the console needs).

The console shows a document list and lets the operator ask questions about the selected
documents only. Both are tenant-scoped: scoping may *narrow* which of your own documents are
searched, never *widen* whose.
"""

from __future__ import annotations

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job

CUTI_TEXT = (
    "# SOP Cuti\n\nProsedur cuti tahunan: karyawan mengajukan cuti melalui sistem HR minimal tujuh "
    "hari sebelum tanggal mulai.\n"
)
LEMBUR_TEXT = (
    "# SOP Lembur\n\nPengajuan lembur disetujui manajer paling lambat satu hari sebelum pelaksanaan, "
    "dan dibayar satu setengah kali upah per jam.\n"
)


def _index(client, *, document_id: str, text: str, key: str = TENANT_A_KEY, kb: str = "kb_hr"):
    return client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": document_id,
            "knowledge_base_id": kb,
            "document_name": f"{document_id}.md",
            "text": text,
        },
        headers=auth(key),
    )


def test_list_returns_this_tenants_documents_only(client):
    _index(client, document_id="doc_list_a", text=CUTI_TEXT)
    wait_for_job(client, "doc_list_a")
    _index(client, document_id="doc_list_b", text=LEMBUR_TEXT, key=TENANT_B_KEY)
    wait_for_job(client, "doc_list_b", key=TENANT_B_KEY)

    data = client.get("/api/v1/knowledge", headers=auth(TENANT_A_KEY)).json()["data"]
    ids = [item["document_id"] for item in data["documents"]]
    assert "doc_list_a" in ids
    assert "doc_list_b" not in ids
    assert data["count"] == len(data["documents"])
    entry = next(item for item in data["documents"] if item["document_id"] == "doc_list_a")
    assert entry["status"] == "completed"
    assert entry["document_name"] == "doc_list_a.md"
    assert entry["chunks"] >= 1
    assert entry["vectors_in_store"] >= 1


def test_list_ignores_a_client_supplied_organization_id(client):
    """An unknown query param must be inert, not honoured."""
    _index(client, document_id="doc_list_c", text=CUTI_TEXT)
    wait_for_job(client, "doc_list_c")
    response = client.get(
        "/api/v1/knowledge?organization_id=org_b", headers=auth(TENANT_A_KEY)
    )
    assert response.status_code == 200
    ids = [item["document_id"] for item in response.json()["data"]["documents"]]
    assert "doc_list_c" in ids
    assert all(not item.startswith("doc_b") for item in ids)


def test_list_is_read_permission_only_and_supports_kb_filter(client):
    assert client.get("/api/v1/knowledge", headers=auth(READ_ONLY_KEY)).status_code == 200
    assert client.get("/api/v1/knowledge", headers={}).status_code == 401

    _index(client, document_id="doc_list_kb1", text=CUTI_TEXT, kb="kb_one")
    wait_for_job(client, "doc_list_kb1")
    _index(client, document_id="doc_list_kb2", text=CUTI_TEXT, kb="kb_two")
    wait_for_job(client, "doc_list_kb2")

    only_one = client.get("/api/v1/knowledge?knowledge_base_id=kb_one", headers=auth(TENANT_A_KEY))
    ids = [item["document_id"] for item in only_one.json()["data"]["documents"]]
    assert ids == ["doc_list_kb1"]


def test_deleted_documents_leave_the_list(client):
    _index(client, document_id="doc_list_gone", text=CUTI_TEXT)
    wait_for_job(client, "doc_list_gone")
    assert client.delete("/api/v1/knowledge/doc_list_gone", headers=auth(TENANT_A_KEY)).status_code == 200

    ids = [
        item["document_id"]
        for item in client.get("/api/v1/knowledge", headers=auth(TENANT_A_KEY)).json()["data"]["documents"]
    ]
    assert "doc_list_gone" not in ids

    show_deleted = client.get(
        "/api/v1/knowledge?include_deleted=true", headers=auth(TENANT_A_KEY)
    ).json()["data"]["documents"]
    assert "doc_list_gone" in [item["document_id"] for item in show_deleted]


def test_query_can_be_scoped_to_one_document(client):
    _index(client, document_id="doc_scope_cuti", text=CUTI_TEXT)
    wait_for_job(client, "doc_scope_cuti")
    _index(client, document_id="doc_scope_lembur", text=LEMBUR_TEXT)
    wait_for_job(client, "doc_scope_lembur")

    scoped = client.post(
        "/api/v1/search",
        json={
            "query": "lembur upah",
            "knowledge_base_id": "kb_hr",
            "options": {"document_ids": ["doc_scope_cuti"]},
        },
        headers=auth(TENANT_A_KEY),
    )
    hits = scoped.json()["data"]["results"]
    assert hits, "scoping must not empty a single-document scope that has matches"
    assert {hit["document_id"] for hit in hits} == {"doc_scope_cuti"}

    unscoped = client.post(
        "/api/v1/search",
        json={"query": "lembur upah", "knowledge_base_id": "kb_hr"},
        headers=auth(TENANT_A_KEY),
    )
    assert "doc_scope_lembur" in {hit["document_id"] for hit in unscoped.json()["data"]["results"]}


def test_document_scope_cannot_reach_another_tenant(client):
    """Naming a foreign document id must return nothing — not an error that leaks existence."""
    _index(client, document_id="doc_scope_b", text=LEMBUR_TEXT, key=TENANT_B_KEY)
    wait_for_job(client, "doc_scope_b", key=TENANT_B_KEY)

    response = client.post(
        "/api/v1/query",
        json={
            "query": "pengajuan lembur",
            "knowledge_base_id": "kb_hr",
            "options": {"document_ids": ["doc_scope_b"]},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sources"] == []
    assert data["grounded"] is False
    assert data["no_answer_reason"] == "no_candidates"


def test_document_scope_is_validated_early(client):
    """Bad scope input is a 422 before any retrieval work happens."""
    too_many = client.post(
        "/api/v1/query",
        json={
            "query": "cuti",
            "knowledge_base_id": "kb_hr",
            "options": {"document_ids": [f"d{i}" for i in range(51)]},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert too_many.status_code == 422

    blank_only = client.post(
        "/api/v1/query",
        json={
            "query": "cuti",
            "knowledge_base_id": "kb_hr",
            "options": {"document_ids": ["   ", ""]},
        },
        headers=auth(TENANT_A_KEY),
    )
    # blanks are dropped, and an empty scope means "no narrowing" rather than "no documents"
    assert blank_only.status_code == 200


def test_indexing_inline_text_with_a_free_form_label_works(client):
    """The main console flow: type text, name it like a human, get a document."""
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_free_label",
            "knowledge_base_id": "kb_hr",
            "document_name": "SOP Cuti 2026",
            "text": CUTI_TEXT,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    status = wait_for_job(client, "doc_free_label")
    assert status["status"] == "completed", status.get("error")
    assert status["chunks"] >= 1

    listed = client.get("/api/v1/knowledge", headers=auth(TENANT_A_KEY)).json()["data"]["documents"]
    entry = next(item for item in listed if item["document_id"] == "doc_free_label")
    assert entry["status"] == "completed"
