"""POST /vision (gambar -> teks/JSON) dan structured output di /extract (response_format json_schema)."""

from __future__ import annotations

import base64
import json

import fitz
import pytest

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, auth, wait_for_job


def _png(width: int = 64, height: int = 48) -> str:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), False)
    pixmap.set_rect(pixmap.irect, (40, 90, 200))
    return base64.b64encode(pixmap.tobytes("png")).decode()


CHART_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array"}, "values": {"type": "array"}},
    "required": ["labels", "values"],
    "additionalProperties": False,
}


def test_vision_reads_images_and_reports_model_and_usage(client):
    response = client.post(
        "/api/v1/vision",
        json={"images": [{"data": _png(), "mime": "image/png"}], "query": "Apa isi gambar ini?"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["answer"] == "Gambar diterima: 1."
    assert data["model"] == "mock-vision"
    assert data["json"] is None
    assert data["usage"]["input_tokens"] > 0
    assert data["images"][0]["width"] == 64 and data["images"][0]["height"] == 48


def test_vision_json_answer_is_valid_json(client):
    response = client.post(
        "/api/v1/vision",
        json={
            "images": [{"data": _png()}, {"data": "data:image/png;base64," + _png(32, 32)}],
            "query": "Baca grafik",
            "response_format": {"type": "json_object"},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert json.loads(data["answer"]) == data["json"]
    assert data["json"]["images_seen"] == 2


def test_vision_accepts_json_schema_and_read_only_keys(client):
    response = client.post(
        "/api/v1/vision",
        json={
            "images": [{"data": _png(), "mime": "image/png"}],
            "query": "Baca grafik",
            "response_format": {"type": "json_schema", "json_schema": {"name": "chart", "schema": CHART_SCHEMA}},
        },
        headers=auth(READ_ONLY_KEY),
    )
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["data"]["json"], dict)


@pytest.mark.parametrize(
    "body, status",
    [
        ({"images": [{"data": _png(), "mime": "image/gif"}], "query": "x"}, 415),
        ({"images": [{"data": base64.b64encode(b"bukan gambar sama sekali").decode(), "mime": "image/png"}], "query": "x"}, 415),
        ({"images": [{"data": "%%%tidak-base64%%%", "mime": "image/png"}], "query": "x"}, 422),
        ({"images": [{"data": _png(), "mime": "image/png"}] * 9, "query": "x"}, 422),
        ({"images": [{"data": _png(2100, 40), "mime": "image/png"}], "query": "x"}, 413),
        ({"images": [], "query": "x"}, 422),
        ({"images": [{"data": _png(), "mime": "image/png"}], "query": "x", "response_format": {"type": "json_schema"}}, 422),
    ],
)
def test_vision_rejects_bad_input(client, body, status):
    response = client.post("/api/v1/vision", json=body, headers=auth(TENANT_A_KEY))
    assert response.status_code == status, response.text


def test_vision_needs_an_api_key(client):
    response = client.post("/api/v1/vision", json={"images": [{"data": _png(), "mime": "image/png"}], "query": "x"})
    assert response.status_code == 401


def test_vision_can_be_switched_off_in_settings(client):
    response = client.put("/api/v1/settings", json={"vision": {"enabled": False}}, headers=auth(TENANT_A_KEY))
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/v1/vision", json={"images": [{"data": _png(), "mime": "image/png"}], "query": "x"}, headers=auth(TENANT_A_KEY)
    )
    assert response.status_code == 403


def test_vision_settings_are_validated(client):
    bad = client.put("/api/v1/settings", json={"vision": {"base_url": "file:///etc/passwd"}}, headers=auth(TENANT_A_KEY))
    assert bad.status_code == 422
    bad = client.put("/api/v1/settings", json={"vision": {"max_images": 99}}, headers=auth(TENANT_A_KEY))
    assert bad.status_code == 422
    good = client.put(
        "/api/v1/settings",
        json={"vision": {"model": "some-vl-model", "base_url": "https://vision.example.com/v1", "api_key": "k-123456789"}},
        headers=auth(TENANT_A_KEY),
    )
    assert good.status_code == 200, good.text
    section = good.json()["data"]["sections"]["vision"]
    assert section["model"] == "some-vl-model"
    assert section["api_key_set"] is True and "k-123456789" not in json.dumps(section)


TABLE = "# Laporan Penjualan\n\n| Bulan | Penjualan |\n| Januari | 100000000 |\n| Februari | 120000000 |\n"


def _index(client, document_id: str) -> None:
    response = client.post(
        "/api/v1/knowledge/index",
        json={"document_id": document_id, "knowledge_base_id": "kb_hr", "document_name": f"{document_id}.md", "text": TABLE},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    assert wait_for_job(client, document_id)["status"] == "completed"


def test_extract_reports_usage_with_the_model_name(client):
    _index(client, "doc_u1")
    response = client.post(
        "/api/v1/extract",
        json={"query": "Ambil data penjualan bulanan", "knowledge_base_id": "kb_hr"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["usage"]["model"] == "mock-llm"
    assert data["usage"]["output_tokens"] > 0


def test_extract_with_json_schema_keeps_items_clean(client):
    _index(client, "doc_u2")
    response = client.post(
        "/api/v1/extract",
        json={
            "query": "Ambil data penjualan bulanan",
            "knowledge_base_id": "kb_hr",
            "response_format": {"type": "json_schema", "json_schema": {"name": "rows", "schema": {"type": "object"}}},
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["usage"]["model"] == "mock-llm"
    assert data["items"], data
    # structured output tidak boleh ditempeli kolom tambahan yang melanggar skema pemanggil
    assert all("usage" not in item for item in data["items"] if isinstance(item, dict))
    assert all("__usage__" not in item for item in data["items"] if isinstance(item, dict))
