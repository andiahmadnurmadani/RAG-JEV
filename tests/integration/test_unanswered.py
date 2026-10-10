"""Pertanyaan yang tidak terjawab dikumpulkan, bisa ditinjau, ditandai selesai, dan dihapus."""

from __future__ import annotations

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job

KB = "kb_unans"
OUT = "Berapa harga saham perusahaan di bursa tahun 2099?"


def _setup(client, settings) -> None:
    settings.reranker_provider = "lexical"
    settings.reranker_enabled = True
    response = client.post(
        "/api/v1/knowledge/index",
        json={"document_id": "doc_cuti", "knowledge_base_id": KB, "document_name": "SOP Cuti.md",
              "text": "# SOP Cuti\nJatah cuti tahunan adalah 12 hari kerja per tahun."},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    wait_for_job(client, "doc_cuti")


def _ask(client, query: str, key: str = TENANT_A_KEY, kb: str = KB) -> dict:
    return client.post("/api/v1/query", json={"query": query, "knowledge_base_id": kb}, headers=auth(key)).json()["data"]


def _list(client, key: str = TENANT_A_KEY, **params) -> dict:
    return client.get("/api/v1/unanswered", params=params, headers=auth(key)).json()["data"]


def test_pertanyaan_tak_terjawab_dicatat_dan_digabung(client, settings):
    _setup(client, settings)
    assert _ask(client, OUT)["grounded"] is False
    _ask(client, "  berapa HARGA saham perusahaan di bursa tahun 2099 ")
    assert _ask(client, "berapa jatah cuti tahunan")["grounded"] is True

    data = _list(client)
    assert data["total"] == 1 and data["open"] == 1
    item = data["items"][0]
    assert item["count"] == 2 and item["knowledge_base_id"] == KB
    assert item["reason"] in ("below_threshold", "no_candidates")
    assert data["knowledge_bases"] == [KB]
    # tenant lain tidak melihatnya
    assert _list(client, TENANT_B_KEY)["total"] == 0


def test_tandai_selesai_dibuka_lagi_bila_ditanya_lagi_lalu_dihapus(client, settings):
    _setup(client, settings)
    _ask(client, OUT)
    item_id = _list(client)["items"][0]["id"]

    assert client.patch("/api/v1/unanswered", json={"ids": [item_id], "status": "resolved", "note": "dokumen saham diunggah"},
                        headers=auth(READ_ONLY_KEY)).status_code == 403
    done = client.patch("/api/v1/unanswered", json={"ids": [item_id], "status": "resolved", "note": "dokumen saham diunggah"},
                        headers=auth(TENANT_A_KEY))
    assert done.json()["data"]["updated"] == 1
    assert _list(client, status="resolved")["items"][0]["note"] == "dokumen saham diunggah"

    _ask(client, OUT)
    assert _list(client)["items"][0]["status"] == "open", "ditanya lagi dan masih tak terjawab -> terbuka lagi"

    assert client.post("/api/v1/unanswered/delete", json={"ids": [item_id]}, headers=auth(TENANT_B_KEY)).json()["data"]["deleted"] == 0
    assert client.post("/api/v1/unanswered/delete", json={"ids": [item_id]}, headers=auth(TENANT_A_KEY)).json()["data"]["deleted"] == 1
    assert _list(client)["total"] == 0


def test_hapus_semua_wajib_konfirmasi(client, settings):
    _setup(client, settings)
    _ask(client, OUT)
    _ask(client, "Siapa direktur utama perusahaan tahun 2099?")
    assert client.delete("/api/v1/unanswered", headers=auth(TENANT_A_KEY)).status_code == 422
    cleared = client.delete("/api/v1/unanswered?confirm=hapus&knowledge_base_id=" + KB, headers=auth(TENANT_A_KEY))
    assert cleared.json()["data"]["deleted"] == 2
    assert _list(client)["total"] == 0


def test_pencatatan_bisa_diatur(client, settings):
    _setup(client, settings)
    bad = client.put("/api/v1/settings", json={"unanswered": {"reasons": ["ngawur"]}}, headers=auth(TENANT_A_KEY))
    assert bad.status_code == 422
    off = client.put("/api/v1/settings", json={"unanswered": {"enabled": False}}, headers=auth(TENANT_A_KEY))
    assert off.status_code == 200
    _ask(client, OUT)
    assert _list(client)["total"] == 0
    client.put("/api/v1/settings", json={"unanswered": {"enabled": True, "reasons": ["no_candidates"]}}, headers=auth(TENANT_A_KEY))
    _ask(client, OUT)  # alasannya below_threshold/no_candidates - hanya no_candidates yang dicatat
    data = _list(client)
    assert all(item["reason"] == "no_candidates" for item in data["items"])
    assert data["recording"]["reasons"] == ["no_candidates"]


def test_kunci_proyek_hanya_melihat_kb_miliknya(client, settings):
    _setup(client, settings)
    _ask(client, OUT)
    key = client.post(
        "/api/v1/settings/api-keys",
        json={"label": "lain", "permissions": ["read", "write"], "knowledge_base_ids": ["kb_lain"]},
        headers=auth(TENANT_A_KEY),
    ).json()["data"]["key"]
    assert _list(client, key)["total"] == 0
    assert client.get("/api/v1/unanswered?knowledge_base_id=" + KB, headers=auth(key)).status_code == 403


def test_hapus_kb_ikut_menghapus_pertanyaannya(client, settings):
    _setup(client, settings)
    _ask(client, OUT)
    client.delete(f"/api/v1/knowledge-bases/{KB}?confirm={KB}", headers=auth(TENANT_A_KEY))
    assert _list(client)["total"] == 0
