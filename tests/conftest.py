"""Shared fixtures: hermetic settings (hash embeddings, mock LLM, local Qdrant)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterator

import pytest

TENANT_A_KEY = "test-key-org-a"
TENANT_B_KEY = "test-key-org-b"
READ_ONLY_KEY = "test-key-readonly"
SUPER_KEY = "test-key-super"          # izin "*": boleh membuat kunci untuk tenant lain
KMS_SECRET = "unit-test-shared-secret"


def _configure_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api_keys = (
        "{"
        f'"{TENANT_A_KEY}": {{"user_id": "user_a", "organization_id": "org_a", "application_id": "app_a", "permissions": ["read", "write", "admin"]}},'
        f'"{TENANT_B_KEY}": {{"user_id": "user_b", "organization_id": "org_b", "application_id": "app_b", "permissions": ["read", "write"]}},'
        f'"{READ_ONLY_KEY}": {{"user_id": "user_r", "organization_id": "org_a", "application_id": "app_ro", "permissions": ["read"]}},'
        f'"{SUPER_KEY}": {{"user_id": "user_s", "organization_id": "org_root", "application_id": "app_super", "permissions": ["*"]}}'
        "}"
    )
    env = {
        "APP_ENV": "test",
        "EMBEDDING_PROVIDER": "hash",
        "EMBEDDING_DIM": "384",
        "RERANKER_PROVIDER": "none",
        "RERANKER_ENABLED": "false",
        "LLM_PROVIDER": "mock",
        "QDRANT_URL": "",
        "QDRANT_LOCAL_PATH": str(tmp_path / "qdrant"),
        "QDRANT_COLLECTION": "test_chunks",
        "SPARSE_DIR": str(tmp_path / "sparse"),
        "JOB_STORE_PATH": str(tmp_path / "jobs.json"),
        "TABLE_STORE_PATH": str(tmp_path / "tables.sqlite"),
        "STORAGE_DIR": str(tmp_path / "storage"),
        "REGISTRY_PATH": str(tmp_path / "registry.json"),
        "JEV_ENABLED": "false",
        "JEV_MODE": "off",
        "API_KEYS_JSON": api_keys,
        "KMS_SHARED_SECRET": KMS_SECRET,
        "REQUIRE_TENANT_CONTEXT_TOKEN": "false",
        "RATE_LIMIT_PER_MINUTE": "1000",
        "STRICT_GROUNDING": "true",
        "RELEVANCE_THRESHOLD": "0.0",
        "MAX_UPLOAD_MB": "8",
        "SETTINGS_OVERRIDE_PATH": str(tmp_path / "settings.json"),
        "API_KEYS_PATH": str(tmp_path / "api_keys.json"),
        "INDEXING_WORKERS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_env(tmp_path, monkeypatch)
    from app.core.api_keys import reset_registries
    from app.core.config import reset_settings_cache, get_settings
    from app.qdrant.client import reset_client

    reset_settings_cache()
    reset_client()
    reset_registries()
    from app.api.deps import set_services

    set_services(None)
    settings = get_settings()
    settings.ensure_dirs()
    yield settings
    reset_client()
    reset_registries()
    set_services(None)
    reset_settings_cache()


@pytest.fixture()
def client(settings) -> Iterator["TestClient"]:  # noqa: F821
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def auth(key: str = TENANT_A_KEY) -> dict:
    return {"Authorization": f"Bearer {key}"}


def wait_for_job(client, document_id: str, key: str = TENANT_A_KEY, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        response = client.get(f"/api/v1/knowledge/{document_id}", headers=auth(key))
        last = response.json()
        status = (last.get("data") or {}).get("status")
        if status in {"completed", "failed", "deleted"}:
            return last.get("data") or {}
        time.sleep(0.1)
    return last.get("data") or {}
