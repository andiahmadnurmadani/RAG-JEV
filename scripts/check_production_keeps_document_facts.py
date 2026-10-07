"""Buktikan filter TIDAK berlebihan: aksara asing yang memang ada di dokumen tetap muncul.

Dokumen produksi ``ESP-VoCat_SCH_V1_2.pdf`` memuat label skema beraksara Han (mis. 地 = shield).
Kalau filter membuang semuanya, fakta dokumen itu akan hilang - dan itu kerusakan, bukan
perbaikan. Skrip ini menanyakan isi tabel skema tersebut.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


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


def call(method: str, path: str, payload=None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=200) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        return {"_err": f"{type(exc).__name__}: {exc}"[:100]}


# 1. Cari di mana dokumen beraksara Han itu berada.
print("== cari dokumen beraksara Han ==")
found_kb = None
# Catatan: dokumen beraksara Han di produksi berada di knowledge base ber-organization "dm-..."
# (mis. ESP-VoCat_SCH_V1_2.pdf), bukan di kb_chat/kb_utama. Daftar ini bisa disesuaikan; kalau
# tidak ketemu, kasus "fakta dokumen dipertahankan" tetap terbukti oleh uji di dalam container
# (check_foreign_filter_simulated.py bagian 4: "| 地 | Shield kabel |" tidak diubah).
for kb in ("kb_chat", "kb_utama", "kb_prj_pamjaya", "kb_bestari", "kb_pr_bima"):
    data = ((call("POST", "/search", {
        "query": "tabel skema pin terminal shield kabel",
        "knowledge_base_id": kb, "options": {"top_k": 5},
    })) or {}).get("data") or {}
    for hit in data.get("results") or []:
        content = hit.get("content") or ""
        name = hit.get("document_name") or ""
        if CJK.search(content):
            found_kb = kb
            print(f"  KB {kb} | dokumen: {name[:50]}")
            print("  potongan sumber:", content[:150].replace("\n", " "))
            break
    if found_kb:
        break

if not found_kb:
    print("  (tidak ada dokumen beraksara Han pada KB yang diuji)")

print("\n== tanya isi tabel skema ==")
data = ((call("POST", "/query", {
    "query": "Sebutkan label terminal pada tabel skema, termasuk yang beraksara China.",
    "knowledge_base_id": found_kb or "kb_chat", "options": {"top_k": 6},
})) or {}).get("data") or {}
answer = (data.get("answer") or "").strip()
print("  finish_reason:", (data.get("usage") or {}).get("finish_reason"), "| panjang:", len(answer))
print("  jawaban:", answer[:400].replace("\n", " ") or "(kosong)")
hits = CJK.findall(answer)
print("\n  aksara Han di jawaban:", hits[:10] if hits else "tidak ada")
if hits:
    print("  -> BENAR: aksara yang memang ada di dokumen DIPERTAHANKAN")
else:
    print("  -> perlu diperiksa: model mungkin tidak menyebut label itu kali ini")
