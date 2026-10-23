"""Log akses mencatat asal permintaan tanpa membocorkan rahasia."""

import json
import logging

from tests.conftest import TENANT_A_KEY, auth


def _access_lines(caplog):
    return [r.extra_fields for r in caplog.records if r.name == "app.access"]


def test_access_log_records_where_the_request_came_from(client, caplog):
    caplog.set_level(logging.INFO, logger="app.access")
    client.get(
        "/api/v1/health?knowledge_base_id=kb_utama&access_token=rahasia123",
        headers={
            "CF-Connecting-IP": "203.0.113.9",
            "CF-IPCountry": "ID",
            "Origin": "https://contoh.aiones.app",
            "Referer": "https://contoh.aiones.app/dokumen",
            "User-Agent": "UjiKlien/1.0",
        },
    )
    line = _access_lines(caplog)[-1]
    assert line["client_ip"] == "203.0.113.9"
    assert line["country"] == "ID"
    assert line["origin"] == "https://contoh.aiones.app"
    assert line["referer"] == "https://contoh.aiones.app/dokumen"
    assert line["user_agent"] == "UjiKlien/1.0"
    assert "knowledge_base_id=kb_utama" in line["query"]
    assert "rahasia123" not in json.dumps(line)
    assert "access_token=%2A%2A%2A" in line["query"]


def test_access_log_names_the_credential_but_not_its_value(client, caplog):
    caplog.set_level(logging.INFO, logger="app.access")
    client.get("/api/v1/knowledge?limit=5", headers=auth())
    line = _access_lines(caplog)[-1]
    assert line["credential"]
    assert TENANT_A_KEY not in json.dumps(line)
