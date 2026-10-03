"""Verifikasi produksi: kunci biasa membuka Pengaturan, dan pencarian hidup lagi.

Dijalankan DI DALAM container; kunci dibaca dari /data dan tidak pernah dicetak.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"


def any_key() -> str:
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
    token = any_key()
    print("== akses Pengaturan dengan kunci biasa (izin read/write, bukan admin) ==")
    for path in ("/settings", "/settings/api-keys", "/settings/access", "/ready"):
        status, _ = call("GET", path, token=token)
        print(f"  {path:22s} -> HTTP {status}")

    print("\n== pencarian (yang tadinya 500) ==")
    for label, payload in (
        ("kb_chat", {"query": "database", "knowledge_base_id": "kb_chat", "options": {"top_k": 3}}),
        ("kb_utama", {"query": "database", "knowledge_base_id": "kb_utama", "options": {"top_k": 3}}),
    ):
        status, body = call("POST", "/search", payload, token)
        try:
            data = json.loads(body)
            results = (data.get("data") or {}).get("results") or []
            print(f"  {label:9s} -> HTTP {status} | {len(results)} hasil | "
                  f"{[item.get('document_id') for item in results]}")
        except Exception:  # noqa: BLE001
            print(f"  {label:9s} -> HTTP {status} | {body[:200]}")

    print("\n== tanya (jawaban Markdown) ==")
    status, body = call(
        "POST", "/query",
        {"query": "Sebutkan singkat apa isi knowledge base ini.",
         "knowledge_base_id": "kb_chat", "options": {"top_k": 4}},
        token,
    )
    try:
        data = json.loads(body)
        answer = ((data.get("data") or {}).get("answer") or "").strip()
        print(f"  HTTP {status} | {len(answer)} karakter")
        for line in answer.splitlines()[:8]:
            print("   |", line[:130])
    except Exception:  # noqa: BLE001
        print(f"  HTTP {status} | {body[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
