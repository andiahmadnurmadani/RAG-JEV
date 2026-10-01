"""Settings screen API: global LLM/Jev configuration, admin-only, secrets write-only.

The point of these tests is the *boundary*, not the happy path: who may read, what a
read may contain, and what happens to a bad URL.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth, wait_for_job


def _settings(client, key: str = TENANT_A_KEY):
    return client.get("/api/v1/settings", headers=auth(key))


# --------------------------------------------------------------------------- #
# Who may touch global configuration
# --------------------------------------------------------------------------- #
def test_settings_read_requires_the_admin_permission(client):
    response = _settings(client, READ_ONLY_KEY)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_a_tenant_with_write_but_no_admin_cannot_read_settings(client):
    """write lets you index documents; it must not reveal global model configuration."""
    response = _settings(client, TENANT_B_KEY)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["permission"] == "admin"


def test_settings_write_requires_the_admin_permission(client):
    response = client.put(
        "/api/v1/settings",
        json={"llm": {"model": "cmc/deepseek/deepseek-v4-flash"}},
        headers=auth(TENANT_B_KEY),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Secrets never travel back to the browser
# --------------------------------------------------------------------------- #
def test_read_masks_every_key_and_never_echoes_it(client, settings):
    settings.llm_api_key = "sk-abcdefghijklmnop-ab50"
    settings.jev_api_key = "sk-zzzzzzzzzzzzzzzz-1234"

    data = _settings(client).json()["data"]
    blob = json.dumps(data)

    assert "sk-abcdefghijklmnop-ab50" not in blob
    assert "sk-zzzzzzzzzzzzzzzz-1234" not in blob
    assert data["sections"]["llm"]["api_key_set"] is True
    assert data["sections"]["llm"]["api_key_hint"] == "sk-a...ab50"
    assert data["sections"]["jev"]["api_key_hint"] == "sk-z...1234"


def test_read_reports_an_unset_key_as_unset(client):
    data = _settings(client).json()["data"]
    assert data["sections"]["llm"]["api_key_set"] is False
    assert data["sections"]["llm"]["api_key_hint"] is None


# --------------------------------------------------------------------------- #
# Applying a change: live process + persisted file
# --------------------------------------------------------------------------- #
def test_update_rebinds_the_generator_and_shows_up_in_ready(client, settings, monkeypatch):
    """Switching away from the mock provider must swap the client, not just the field."""
    response = client.put(
        "/api/v1/settings",
        json={
            "llm": {
                "provider": "openai_compatible",
                "base_url": "http://localhost:20128/v1",
                "model": "cmc/deepseek/deepseek-v4-flash",
            }
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    assert set(response.json()["data"]["applied"]) == {"llm.provider", "llm.base_url", "llm.model"}

    assert settings.llm_model == "cmc/deepseek/deepseek-v4-flash"
    assert client.get("/api/v1/ready").json()["data"]["detail"]["llm_model"] == "cmc/deepseek/deepseek-v4-flash"
    # the rebind really happened: the mock client would still call itself "mock-llm"
    from app.rag.generator import LLMClient

    assert isinstance(client.app.state.services.generator._client, LLMClient)


def test_a_mock_provider_is_reported_as_mock_not_as_the_configured_model(client, settings):
    """Honesty check: /ready reports what is actually generating, not what we wish."""
    client.put("/api/v1/settings", json={"llm": {"model": "cmc/Qwen/Qwen3.6-Plus"}}, headers=auth(TENANT_A_KEY))
    assert settings.llm_model == "cmc/Qwen/Qwen3.6-Plus"
    assert client.get("/api/v1/ready").json()["data"]["detail"]["llm_model"] == "mock-llm"


def test_update_persists_to_the_override_file(client, settings):
    client.put(
        "/api/v1/settings",
        json={"jev": {"provider": "systemone", "systemone_url": "http://127.0.0.1:20128/v1/systemone", "model": "oc/jev-1.13-free"}},
        headers=auth(TENANT_A_KEY),
    )
    stored = json.loads((settings.settings_override_path and __import__("pathlib").Path(settings.settings_override_path)).read_text(encoding="utf-8"))
    assert stored["jev"]["model"] == "oc/jev-1.13-free"
    assert stored["jev"]["provider"] == "systemone"


def test_stored_overrides_are_reapplied_on_the_next_boot(client, settings):
    client.put(
        "/api/v1/settings",
        json={"llm": {"model": "cmc/Qwen/Qwen3.6-Plus"}},
        headers=auth(TENANT_A_KEY),
    )
    from app.main import create_app
    from fastapi.testclient import TestClient

    settings.llm_model = "Qwen/Qwen3-4B"  # pretend a fresh process read only the env
    with TestClient(create_app()):
        assert settings.llm_model == "cmc/Qwen/Qwen3.6-Plus"


def test_an_absent_key_field_keeps_the_existing_key(client, settings):
    settings.llm_api_key = "sk-existing-key-0001"
    client.put("/api/v1/settings", json={"llm": {"model": "m1"}}, headers=auth(TENANT_A_KEY))
    assert settings.llm_api_key == "sk-existing-key-0001"


def test_an_empty_key_clears_it(client, settings):
    settings.llm_api_key = "sk-existing-key-0001"
    response = client.put("/api/v1/settings", json={"llm": {"api_key": ""}}, headers=auth(TENANT_A_KEY))
    assert response.status_code == 200
    assert settings.llm_api_key == ""
    assert _settings(client).json()["data"]["sections"]["llm"]["api_key_set"] is False


def test_unknown_fields_are_rejected_not_ignored(client):
    response = client.put(
        "/api/v1/settings",
        json={"llm": {"model": "m"}, "organization_id": "org_b"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422


def test_an_empty_update_is_a_validation_error(client):
    response = client.put("/api/v1/settings", json={}, headers=auth(TENANT_A_KEY))
    assert response.status_code == 422


def test_jev_update_repoints_the_router_and_reports_the_new_provider(client):
    response = client.put(
        "/api/v1/settings",
        json={"jev": {"provider": "systemone", "systemone_url": "http://127.0.0.1:20128/v1/systemone", "model": "oc/jev-1.13-free"}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    detail = client.get("/api/v1/ready").json()["data"]["detail"]
    assert detail["jev_provider"] == "systemone"
    assert detail["jev_model"] == "oc/jev-1.13-free"


# --------------------------------------------------------------------------- #
# Probes: fail before saving, and never fetch the wrong thing
# --------------------------------------------------------------------------- #
class FakeModelsResponse:
    status_code = 200

    def json(self):
        return {
            "data": [
                {"id": "cmc/Qwen/Qwen3.6-Plus", "owned_by": "cmc"},
                {"id": "cmc/deepseek/deepseek-v4-flash", "owned_by": "cmc"},
            ]
        }


def test_model_list_probe_returns_sorted_ids(client, monkeypatch):
    import httpx

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        return FakeModelsResponse()

    monkeypatch.setattr(httpx, "get", fake_get)
    response = client.post(
        "/api/v1/settings/llm/models",
        json={"base_url": "http://localhost:20128/v1", "api_key": "sk-probe-key"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert seen["url"] == "http://localhost:20128/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-probe-key"
    assert [m["id"] for m in data["models"]] == ["cmc/deepseek/deepseek-v4-flash", "cmc/Qwen/Qwen3.6-Plus"]
    assert data["count"] == 2


def test_model_list_probe_falls_back_to_the_stored_key(client, settings, monkeypatch):
    import httpx

    settings.llm_api_key = "sk-stored-key"
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: (seen.update(headers) or FakeModelsResponse()))
    client.post("/api/v1/settings/llm/models", json={"base_url": "http://localhost:20128/v1"}, headers=auth(TENANT_A_KEY))
    assert seen["Authorization"] == "Bearer sk-stored-key"


def test_model_list_probe_surfaces_an_upstream_error(client, monkeypatch):
    import httpx

    class Unauthorized:
        status_code = 401

        def json(self):
            return {"error": "nope"}

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: Unauthorized())
    response = client.post(
        "/api/v1/settings/llm/models",
        json={"base_url": "http://localhost:20128/v1"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 502
    assert "401" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://user:pass@example.com/v1",
    ],
)
def test_probe_refuses_urls_it_should_not_fetch(client, url):
    response = client.post(
        "/api/v1/settings/llm/models",
        json={"base_url": url},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422


def test_an_empty_base_url_probes_the_configured_endpoint(client, settings, monkeypatch):
    """Kosong berarti "pakai yang aktif", bukan "tolak": layar setelan memakai ini untuk uji cepat."""
    import httpx

    settings.llm_base_url = "http://configured.example/v1"
    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: (seen.update(url=url) or FakeModelsResponse()))

    response = client.post("/api/v1/settings/llm/models", json={}, headers=auth(TENANT_A_KEY))

    assert response.status_code == 200
    assert seen["url"] == "http://configured.example/v1/models"


def test_jev_probe_asks_one_noul_question_and_reports_latency(client, monkeypatch):
    import httpx

    class Ok:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"answers": {"reachable": {"type": "noul", "noul": 0.05}}}

    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["body"] = json
        return Ok()

    monkeypatch.setattr(httpx, "post", fake_post)
    response = client.post(
        "/api/v1/settings/jev/probe",
        json={"provider": "systemone", "url": "http://localhost:20128/v1/systemone", "model": "oc/jev-1.13-free", "api_key": "sk-jev"},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert seen["url"] == "http://localhost:20128/v1/systemone"
    assert set(seen["body"]) == {"state", "model", "questions"}
    assert data["model"] == "oc/jev-1.13-free"
    assert data["answer"]["type"] == "noul"


def test_jev_probe_reports_a_broken_endpoint_as_502(client, monkeypatch):
    import httpx

    class Broken:
        status_code = 422
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "Endpoint is unavailable."}}

    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None: Broken())
    response = client.post(
        "/api/v1/settings/jev/probe",
        json={
            "provider": "systemone",
            "url": "http://localhost:20128/v1/systemone",
            "model": "oc/jev-1.13-free",
            "api_key": "sk-jev-probe",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 502
    assert "unavailable" in response.json()["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# Format berkas yang boleh jadi knowledge
# --------------------------------------------------------------------------- #
def test_settings_publish_the_format_catalog_with_availability(client):
    data = _settings(client).json()["data"]
    uploads = data["sections"]["uploads"]
    assert ".pdf" in uploads["extensions"]
    assert ".docx" in uploads["extensions"]
    assert ".xlsx" in uploads["extensions"]
    assert isinstance(uploads["max_upload_mb"], int) and uploads["max_upload_mb"] > 0

    catalog = {row["key"]: row for row in data["catalog"]}
    assert catalog["xlsx"]["available"] is True
    assert catalog["pptx"]["available"] is True
    # format yang butuh OCR tidak pernah diklaim tersedia kalau tesseract tidak ada
    if not catalog["image"]["available"]:
        assert "tesseract" in catalog["image"]["note"]


def test_admin_can_choose_the_allowed_extensions_and_ready_follows(client, settings):
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": [".pdf", ".docx", ".xlsx", ".csv"]}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert "uploads.extensions" in data["applied"]
    assert data["sections"]["uploads"]["extensions"] == [".csv", ".docx", ".pdf", ".xlsx"]

    ready = client.get("/api/v1/ready").json()["data"]["detail"]
    assert ready["allowed_extensions"] == [".csv", ".docx", ".pdf", ".xlsx"]
    assert ".txt" not in ready["allowed_extensions"]


def test_an_unknown_extension_is_refused_before_anything_is_written(client, settings):
    before = sorted(settings.upload_extensions)
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": [".pdf", ".exe"]}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "ekstensi tidak dikenal" in response.json()["error"]["message"]
    assert sorted(settings.upload_extensions) == before, "setelan tidak boleh berubah"


def test_an_empty_extension_list_is_refused(client):
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": []}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert "minimal satu" in response.json()["error"]["message"].lower()


def test_formats_that_cannot_be_read_on_this_machine_cannot_be_the_only_choice(client):
    """Memilih hanya format yang belum didukung harus gagal jelas, bukan diterima lalu bingung."""
    from app.parsing import formats

    if formats.availability(formats.BY_KEY["image"]):
        pytest.skip("OCR terpasang di mesin ini")
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": [".png"]}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["unavailable"] == [".png"]


def test_disabling_an_extension_rejects_that_upload_at_pre_flight(client, settings):
    client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": [".pdf", ".docx"]}},
        headers=auth(TENANT_A_KEY),
    )

    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_txt_disabled",
            "knowledge_base_id": "kb_settings_ext",
            "document_name": "catatan.txt",
            "text": "Isi teks yang tidak boleh masuk lagi.",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    # pre-flight: tidak ada job yang dibuat
    assert client.get("/api/v1/knowledge/doc_txt_disabled", headers=auth(TENANT_A_KEY)).status_code == 404


def test_turning_an_extension_back_on_lets_it_through(client, settings):
    client.put(
        "/api/v1/settings",
        json={"uploads": {"extensions": [".pdf", ".txt"]}},
        headers=auth(TENANT_A_KEY),
    )
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_txt_enabled",
            "knowledge_base_id": "kb_settings_ext",
            "document_name": "catatan.txt",
            "text": "Isi teks yang boleh masuk. Kuota cuti dua belas hari.",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    job = wait_for_job(client, "doc_txt_enabled")
    assert job["status"] == "completed"
    assert job["chunks"] >= 1


def test_max_upload_size_is_configurable_and_enforced_early(client, settings):
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"max_upload_mb": 1}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    assert response.json()["data"]["sections"]["uploads"]["max_upload_mb"] == 1
    assert client.get("/api/v1/ready").json()["data"]["detail"]["max_upload_mb"] == 1

    import base64

    big = base64.b64encode(b"x" * (2 * 1024 * 1024)).decode()
    rejected = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_too_big",
            "knowledge_base_id": "kb_settings_ext",
            "document_name": "besar.txt",
            "content_base64": big,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert rejected.status_code == 413
    assert rejected.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_out_of_range_upload_size_is_refused(client):
    response = client.put(
        "/api/v1/settings",
        json={"uploads": {"max_upload_mb": 5000}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert "max_upload_mb" in response.json()["error"]["message"]

