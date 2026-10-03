"""Uji produksi (read-only): apakah pencarian & tanya masih bekerja setelah galat /ready.

Dijalankan DI DALAM container. Kunci dibaca dari registry; tidak pernah dicetak.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"


def key() -> str:
    for path in (Path("/data/api_keys.json"), Path("/data/bootstrap_admin_key.json")):
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("key"):
            return data["key"]
        records = data.get("keys") if isinstance(data, dict) else data
        items = records.values() if isinstance(records, dict) else (records or [])
        for record in items:
            if not record.get("revoked_at") and not record.get("revoked") and record.get("key"):
                return record["key"]
    raise SystemExit("tidak ada kunci")


def call(method: str, path: str, payload=None, token: str = "") -> tuple[int, str]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode()[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    token = key()
    for label, method, path, payload in (
        ("health", "GET", "/health", None),
        ("ready", "GET", "/ready", None),
        ("search", "POST", "/search", {"query": "cuti", "knowledge_base_id": "kb_chat", "top_k": 3}),
        ("settings", "GET", "/settings", None),
    ):
        status, body = call(method, path, payload, token)
        print(f"  {label:9s} -> HTTP {status} | {body[:220]}")

    print("\n== langsung ke klien Qdrant (lewat app) ==")
    sys.path.insert(0, "/srv")
    from app.core.config import get_settings
    from app.qdrant import collections, repository

    settings = get_settings()
    for label, call_it in (
        ("collection_info", lambda: collections.collection_info(settings)),
        ("count_document(semua)", lambda: repository.list_document_chunks(
            settings, organization_id="default", document_id="__none__", knowledge_base_id="kb_chat")),
    ):
        try:
            print(f"  {label}: ok ->", str(call_it())[:180])
        except Exception as exc:  # noqa: BLE001
            print(f"  {label}: GAGAL {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
