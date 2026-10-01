"""Kunci admin bootstrap: jalan masuk pertama pada layanan yang belum punya kunci apa pun.

Tanpa ini, gerbang kode akses mustahil dipasang: memasang kode butuh kredensial ``admin``,
dan pemasangan baru belum punya kunci sama sekali. Yang diuji di sini adalah batasnya -
kapan kunci diterbitkan, kapan tidak, dan apa yang tertulis di disk.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import BOOTSTRAP_LABEL, ensure_bootstrap_admin

ENV_KEYS = (
    "API_KEYS_JSON",
    "API_KEYS_PATH",
    "ACCESS_PATH",
    "SESSIONS_PATH",
    "BOOTSTRAP_ADMIN_KEY_PATH",
)


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Layanan tanpa kunci sama sekali: env kosong, registry kosong."""
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")
    monkeypatch.setenv("RERANKER_PROVIDER", "none")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("JEV_ENABLED", "false")
    monkeypatch.setenv("JEV_MODE", "off")
    monkeypatch.setenv("QDRANT_URL", "")
    monkeypatch.setenv("QDRANT_LOCAL_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("QDRANT_COLLECTION", "bootstrap_chunks")
    monkeypatch.setenv("SPARSE_DIR", str(tmp_path / "sparse"))
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("JOB_STORE_PATH", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("TABLE_STORE_PATH", str(tmp_path / "tables.sqlite"))
    monkeypatch.setenv("SETTINGS_OVERRIDE_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("API_KEYS_JSON", "")
    monkeypatch.setenv("API_KEYS_PATH", str(tmp_path / "api_keys.json"))
    monkeypatch.setenv("ACCESS_PATH", str(tmp_path / "access.json"))
    monkeypatch.setenv("SESSIONS_PATH", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BOOTSTRAP_ADMIN_KEY_PATH", str(tmp_path / "bootstrap_admin_key.json"))
    monkeypatch.setenv("INDEXING_WORKERS", "1")

    from app.core.api_keys import reset_registries
    from app.core.config import get_settings, reset_settings_cache
    from app.qdrant.client import reset_client as reset_qdrant

    reset_settings_cache()
    reset_registries()
    reset_qdrant()
    from app.api.deps import set_services

    set_services(None)
    settings = get_settings()
    settings.ensure_dirs()
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client, settings
    reset_registries()
    reset_qdrant()
    set_services(None)
    reset_settings_cache()


def _bootstrap_file(settings) -> Path:
    return Path(settings.bootstrap_admin_key_path)


def test_a_fresh_service_gets_one_admin_key(fresh):
    client, settings = fresh
    path = _bootstrap_file(settings)
    assert path.exists(), "pemasangan baru harus punya jalan masuk pertama"
    stored = json.loads(path.read_text(encoding="utf-8"))
    key = stored["key"]
    assert key.startswith("rag_")

    # Kunci itu benar-benar berizin penuh: cukup untuk memasang kode akses.
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/v1/settings/access", headers=headers).status_code == 200
    assert client.put("/api/v1/settings/access", json={"code": "kode-bootstrap-1"}, headers=headers).status_code == 200
    assert client.get("/api/v1/auth/gate").json()["data"]["enabled"] is True


def test_the_bootstrap_key_is_written_with_owner_only_mode(fresh):
    _, settings = fresh
    mode = stat.S_IMODE(os.stat(_bootstrap_file(settings)).st_mode)
    if os.name == "nt":  # pragma: no cover - Windows tidak memakai bit POSIX
        assert mode in (0o600, 0o666)
    else:
        assert mode == 0o600, "berkas berisi kunci harus hanya bisa dibaca pemiliknya"


def test_the_bootstrap_key_never_reaches_the_logs(fresh, caplog):
    _, settings = fresh
    key = json.loads(_bootstrap_file(settings).read_text(encoding="utf-8"))["key"]
    assert key not in caplog.text, "nilai kunci tidak boleh masuk log"


def test_it_is_listed_like_any_other_key_so_it_can_be_revoked(fresh):
    client, settings = fresh
    key = json.loads(_bootstrap_file(settings).read_text(encoding="utf-8"))["key"]
    headers = {"Authorization": f"Bearer {key}"}
    entries = client.get("/api/v1/settings/api-keys", headers=headers).json()["data"]["keys"]
    mine = [entry for entry in entries if entry.get("label") == BOOTSTRAP_LABEL]
    assert mine and mine[0]["state"] == "active"

    # Kunci yang sedang dipakai memang tidak boleh mencabut dirinya sendiri: pakai sesi kode akses.
    assert client.put("/api/v1/settings/access", json={"code": "kode-bootstrap-2"}, headers=headers).status_code == 200
    session = client.post("/api/v1/auth/login", json={"code": "kode-bootstrap-2"}).json()["data"]["token"]
    assert client.delete(
        f"/api/v1/settings/api-keys/{mine[0]['key_id']}",
        headers={"Authorization": f"Bearer {session}"},
    ).status_code == 200
    assert client.get("/api/v1/settings/access", headers=headers).status_code == 401
    assert client.get("/api/v1/settings/access", headers={"Authorization": f"Bearer {session}"}).status_code == 200


def test_a_revoked_bootstrap_key_is_not_handed_out_again(fresh):
    """Menghormati pencabutan: menjalankan ulang layanan tidak menghidupkan kembali kunci lama."""
    client, settings = fresh
    key = json.loads(_bootstrap_file(settings).read_text(encoding="utf-8"))["key"]
    headers = {"Authorization": f"Bearer {key}"}
    entries = client.get("/api/v1/settings/api-keys", headers=headers).json()["data"]["keys"]
    key_id = [entry for entry in entries if entry.get("label") == BOOTSTRAP_LABEL][0]["key_id"]
    client.put("/api/v1/settings/access", json={"code": "kode-bootstrap-3"}, headers=headers)
    session = client.post("/api/v1/auth/login", json={"code": "kode-bootstrap-3"}).json()["data"]["token"]
    client.delete(f"/api/v1/settings/api-keys/{key_id}", headers={"Authorization": f"Bearer {session}"})

    assert ensure_bootstrap_admin(settings) is None
    assert client.get("/api/v1/settings/access", headers=headers).status_code == 401


def test_an_env_admin_key_stops_the_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS_JSON", json.dumps({"rag_env_admin": {
        "user_id": "env", "organization_id": "env_org", "application_id": "app", "permissions": ["*"],
    }}))
    monkeypatch.setenv("API_KEYS_PATH", str(tmp_path / "api_keys.json"))
    monkeypatch.setenv("BOOTSTRAP_ADMIN_KEY_PATH", str(tmp_path / "bootstrap_admin_key.json"))
    from app.core.api_keys import reset_registries
    from app.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    reset_registries()
    settings = get_settings()
    assert ensure_bootstrap_admin(settings) is None
    assert not (tmp_path / "bootstrap_admin_key.json").exists()


def test_a_non_admin_env_key_still_triggers_the_bootstrap(tmp_path, monkeypatch):
    """Kunci read-only bukan jalan masuk ke panel: bootstrap tetap diperlukan."""
    monkeypatch.setenv("API_KEYS_JSON", json.dumps({"rag_env_reader": {
        "user_id": "env", "organization_id": "env_org", "application_id": "app", "permissions": ["read"],
    }}))
    monkeypatch.setenv("API_KEYS_PATH", str(tmp_path / "api_keys.json"))
    monkeypatch.setenv("BOOTSTRAP_ADMIN_KEY_PATH", str(tmp_path / "bootstrap_admin_key.json"))
    from app.core.api_keys import reset_registries
    from app.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    reset_registries()
    settings = get_settings()
    assert ensure_bootstrap_admin(settings) is not None
    assert (tmp_path / "bootstrap_admin_key.json").exists()
    reset_registries()
    reset_settings_cache()


def test_the_bootstrap_file_follows_the_key_registry_directory(tmp_path, monkeypatch):
    """Di container, berkas bootstrap harus ikut volume data (direktori registry)."""
    monkeypatch.setenv("API_KEYS_JSON", "")
    monkeypatch.setenv("API_KEYS_PATH", str(tmp_path / "data" / "api_keys.json"))
    monkeypatch.delenv("BOOTSTRAP_ADMIN_KEY_PATH", raising=False)
    from app.core.api_keys import reset_registries
    from app.core.bootstrap import bootstrap_path
    from app.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    reset_registries()
    settings = get_settings()
    assert bootstrap_path(settings) == tmp_path / "data" / "bootstrap_admin_key.json"
    entry = ensure_bootstrap_admin(settings)
    assert entry is not None
    assert (tmp_path / "data" / "bootstrap_admin_key.json").exists()
    reset_registries()
    reset_settings_cache()


def test_bootstrap_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS_JSON", "")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_KEY", "false")
    monkeypatch.setenv("API_KEYS_PATH", str(tmp_path / "api_keys.json"))
    monkeypatch.setenv("BOOTSTRAP_ADMIN_KEY_PATH", str(tmp_path / "bootstrap_admin_key.json"))
    from app.core.api_keys import reset_registries
    from app.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    reset_registries()
    settings = get_settings()
    assert ensure_bootstrap_admin(settings) is None
    assert not (tmp_path / "bootstrap_admin_key.json").exists()
    reset_registries()
    reset_settings_cache()
