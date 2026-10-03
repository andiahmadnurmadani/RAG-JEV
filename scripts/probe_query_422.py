"""Periksa pesan galat 422 pada /search dan /query (read-only, di dalam container)."""

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
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    token = any_key()
    print("== /search tanpa knowledge_base_id ==")
    status, body = call("POST", "/search", {"query": "database", "top_k": 3}, token)
    print(" ", status, body[:400])

    print("\n== /query tanpa knowledge_base_id ==")
    status, body = call("POST", "/query", {"query": "apa isi kb ini?", "top_k": 4}, token)
    print(" ", status, body[:400])

    print("\n== /query DENGAN knowledge_base_id ==")
    status, body = call(
        "POST", "/query",
        {"query": "Apa isi knowledge base ini? Sebutkan singkat.",
         "knowledge_base_id": "kb_chat", "top_k": 4},
        token,
    )
    print(" ", status)
    if status == 200:
        data = json.loads(body)["data"]
        answer = (data.get("answer") or "").strip()
        print(f"  panjang jawaban: {len(answer)} karakter")
        for line in answer.splitlines()[:10]:
            print("   |", line[:140])
    else:
        print(" ", body[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
