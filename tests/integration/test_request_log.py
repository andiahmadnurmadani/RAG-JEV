"""Log permintaan di konsol: setiap permintaan API tercatat lengkap dengan asalnya."""

from __future__ import annotations

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth

ORIGIN_HEADERS = {
    "CF-Connecting-IP": "203.0.113.7",
    "CF-IPCountry": "ID",
    "Origin": "https://kms.contoh.id",
    "Referer": "https://kms.contoh.id/dokumen",
    "User-Agent": "node",
}


def _logs(client, key=TENANT_A_KEY, **params):
    response = client.get("/api/v1/logs/requests", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_request_is_stored_with_where_it_came_from(client):
    client.get(
        "/api/v1/knowledge?knowledge_base_id=kb_log&limit=5&access_token=rahasia",
        headers={**auth(TENANT_A_KEY), **ORIGIN_HEADERS},
    )

    data = _logs(client)
    item = next(entry for entry in data["items"] if entry["path"] == "/api/v1/knowledge")
    assert item["method"] == "GET"
    assert item["status"] == 200
    assert item["client_ip"] == "203.0.113.7"
    assert item["country"] == "ID"
    assert item["origin"] == "https://kms.contoh.id"
    assert item["referer"] == "https://kms.contoh.id/dokumen"
    assert item["user_agent"] == "node"
    assert item["application_id"] == "app_a"
    assert item["knowledge_base_id"] == "kb_log"
    assert item["latency_ms"] is not None
    assert "rahasia" not in str(item)
    assert TENANT_A_KEY not in str(item)


def test_the_question_and_error_code_are_recorded(client):
    client.post(
        "/api/v1/query",
        json={"query": "berapa hari cuti tahunan?", "knowledge_base_id": "kb_kosong"},
        headers=auth(TENANT_A_KEY),
    )
    client.get("/api/v1/knowledge?limit=99999", headers=auth(TENANT_A_KEY))

    items = _logs(client)["items"]
    asked = next(entry for entry in items if entry["path"] == "/api/v1/query")
    assert asked["detail"] == "berapa hari cuti tahunan?"
    assert asked["knowledge_base_id"] == "kb_kosong"
    rejected = next(entry for entry in items if entry["status"] == 422)
    assert rejected["error_code"] == "VALIDATION_ERROR"


def test_the_summary_groups_requests_by_source(client):
    for _ in range(3):
        client.get("/api/v1/knowledge-bases", headers={**auth(TENANT_A_KEY), **ORIGIN_HEADERS})
    client.get("/api/v1/knowledge-bases", headers={**auth(TENANT_A_KEY), "CF-Connecting-IP": "198.51.100.2"})

    summary = _logs(client)["summary"]
    assert summary["totals"]["total"] >= 4
    top = summary["sources"][0]
    assert top["client_ip"] == "203.0.113.7"
    assert top["count"] == 3
    assert any(row["path"] == "/api/v1/knowledge-bases" for row in summary["paths"])
    assert sum(point["count"] for point in summary["series"]) == summary["totals"]["total"]


def test_filters_narrow_the_list(client):
    client.get("/api/v1/knowledge-bases", headers={**auth(TENANT_A_KEY), **ORIGIN_HEADERS})
    client.get("/api/v1/knowledge?limit=99999", headers=auth(TENANT_A_KEY))

    errors = _logs(client, status="errors")["items"]
    assert errors and all(entry["status"] >= 400 for entry in errors)
    by_ip = _logs(client, client_ip="203.0.113.7")["items"]
    assert by_ip and all(entry["client_ip"] == "203.0.113.7" for entry in by_ip)
    searched = _logs(client, q="kms.contoh")["items"]
    assert searched and all("kms.contoh" in (entry["origin"] or "") for entry in searched)


def test_live_mode_fetches_only_newer_requests(client):
    client.get("/api/v1/knowledge-bases", headers=auth(TENANT_A_KEY))
    newest = _logs(client, status="429")["last_id"]
    assert newest > 0
    assert _logs(client, after_id=newest, summary="false")["items"] == []

    client.get("/api/v1/knowledge?limit=5", headers=auth(TENANT_A_KEY))
    client.get("/api/v1/knowledge?limit=99999", headers=auth(TENANT_A_KEY))
    fresh = _logs(client, after_id=newest, summary="false")
    assert [entry["path"] for entry in fresh["items"]] == ["/api/v1/knowledge", "/api/v1/knowledge"]
    assert fresh["total"] == 2
    assert "summary" not in fresh
    only_errors = _logs(client, after_id=newest, summary="false", status="errors")["items"]
    assert [entry["status"] for entry in only_errors] == [422]


def test_health_checks_and_the_log_page_itself_are_not_recorded(client):
    client.get("/api/v1/health")
    _logs(client)
    _logs(client)

    paths = {entry["path"] for entry in _logs(client)["items"]}
    assert "/api/v1/health" not in paths
    assert not any(path.startswith("/api/v1/logs") for path in paths)


def test_only_the_operator_can_read_or_clear_the_log(client):
    for key in (TENANT_B_KEY, READ_ONLY_KEY):
        response = client.get("/api/v1/logs/requests", headers=auth(key))
        assert response.status_code == 403, response.text
    assert client.delete("/api/v1/logs/requests", headers=auth(TENANT_A_KEY)).status_code == 422
    cleared = client.delete("/api/v1/logs/requests?confirm=hapus", headers=auth(TENANT_A_KEY))
    assert cleared.status_code == 200, cleared.text
    assert _logs(client)["items"] == []


def test_recording_can_be_switched_off(client, settings):
    settings.request_log_enabled = False
    client.get("/api/v1/knowledge-bases", headers=auth(TENANT_A_KEY))
    settings.request_log_enabled = True

    assert all(entry["path"] != "/api/v1/knowledge-bases" for entry in _logs(client)["items"])
