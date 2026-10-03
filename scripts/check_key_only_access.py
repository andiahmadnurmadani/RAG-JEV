"""Uji cepat: dengan CONSOLE_API_KEY_ONLY (bawaan), kunci berizin read/write membuka Pengaturan."""

from __future__ import annotations

import json
import os
import tempfile


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ.update({
        "APP_ENV": "test",
        "EMBEDDING_PROVIDER": "hash",
        "EMBEDDING_DIM": "384",
        "RERANKER_PROVIDER": "none",
        "LLM_PROVIDER": "mock",
        "QDRANT_URL": "",
        "JEV_ENABLED": "false",
        "REQUIRE_TENANT_CONTEXT_TOKEN": "false",
        "QDRANT_LOCAL_PATH": f"{tmp}/q",
        "SPARSE_DIR": f"{tmp}/s",
        "JOB_STORE_PATH": f"{tmp}/j.json",
        "TABLE_STORE_PATH": f"{tmp}/t.sqlite",
        "STORAGE_DIR": f"{tmp}/st",
        "REGISTRY_PATH": f"{tmp}/r.json",
        "SETTINGS_OVERRIDE_PATH": f"{tmp}/set.json",
        "API_KEYS_PATH": f"{tmp}/ak.json",
        "ACCESS_PATH": f"{tmp}/acc.json",
        "SESSIONS_PATH": f"{tmp}/sess.json",
        "BOOTSTRAP_ADMIN_KEY_PATH": f"{tmp}/bk.json",
        "API_KEYS_JSON": json.dumps({
            "k_rw": {"user_id": "u", "organization_id": "org_a", "application_id": "a",
                     "permissions": ["read", "write"]}
        }),
    })
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    with client:
        headers = {"Authorization": "Bearer k_rw"}
        for label, method, path in (
            ("settings", "GET", "/api/v1/settings"),
            ("settings/api-keys", "GET", "/api/v1/settings/api-keys"),
            ("settings/access", "GET", "/api/v1/settings/access"),
            ("ready", "GET", "/api/v1/ready"),
        ):
            response = client.get(path, headers=headers)
            print(f"  {label:20s} -> HTTP {response.status_code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
