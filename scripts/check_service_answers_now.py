"""Uji akhir: apakah layanan SEKARANG bisa menjawab dengan model yang dipakai.

Mengirim pertanyaan lewat /query (jalur yang sama dengan pengguna) untuk beberapa knowledge base,
lalu melaporkan: status HTTP, panjang jawaban, finish_reason, dan apakah ada aksara asing.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


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


TOKEN = any_key()


def call(method: str, path: str, payload=None) -> tuple:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"[:200]


print("== 1. health & model ==")
status, data = call("GET", "/ready")
if status == 200:
    detail = data["data"]["detail"]
    print("  llm dependency:", data["data"]["dependencies"].get("llm"))
    print("  model         :", detail.get("llm_model"))
    print("  titik         :", detail["collection"]["points"])

print("\n== 2. daftar knowledge base yang ada ==")
status, data = call("GET", "/knowledge")
kbs: list[str] = []
if status == 200:
    docs = (data.get("data") or {}).get("documents") or []
    for doc in docs:
        kb = doc.get("knowledge_base_id")
        if kb and kb not in kbs:
            kbs.append(kb)
    print(f"  {len(docs)} dokumen, {len(kbs)} knowledge base")
    print("  contoh KB:", kbs[:6])

print("\n== 3. tanya lewat /query (jalur pengguna) ==")
gagal = 0
for kb in (kbs[:3] or ["kb_chat"]):
    status, data = call("POST", "/query", {
        "query": "Apa isi dokumen ini? Sebutkan pokok isinya secara singkat.",
        "knowledge_base_id": kb,
        "options": {"top_k": 5},
    })
    if status != 200:
        gagal += 1
        print(f"  KB {kb}: HTTP {status} -> {str(data)[:150]}")
        continue
    payload = data.get("data") or {}
    answer = (payload.get("answer") or "").strip()
    usage = payload.get("usage") or {}
    print(f"  KB {kb}: HTTP 200 | panjang {len(answer)} | finish_reason={usage.get('finish_reason')!r} "
          f"| aksara asing: {CJK.findall(answer)[:4] or 'tidak ada'}")
    print("     jawaban:", answer[:180].replace("\n", " ") or "(KOSONG)")

print("\n== 4. health check LLM (max_tokens=4, seperti kode) ==")
status, data = call("GET", "/ready")
if status == 200:
    print("  llm dependency:", data["data"]["dependencies"].get("llm"))

print(f"\n{'SEMUA LULUS' if gagal == 0 else f'{gagal} GAGAL'}")
