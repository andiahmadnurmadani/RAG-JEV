"""Tanya langsung ke layanan produksi: apakah jawaban masih memuat aksara asing?

Dijalankan di dalam container. Kunci dibaca dari /data dan tidak pernah dicetak.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


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


def ask(question: str, kb: str) -> dict:
    body = json.dumps({"query": question, "knowledge_base_id": kb, "options": {"top_k": 5}}).encode()
    request = urllib.request.Request(BASE + "/query", data=body, method="POST")
    request.add_header("Authorization", f"Bearer {TOKEN}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=220) as response:
            return json.loads(response.read().decode()).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        return {"_err": f"{type(exc).__name__}: {exc}"[:100]}


for question, kb in (
    ("Apa saja isi dokumen ini? Sebutkan ringkas.", "kb_chat"),
    ("Ringkas isi knowledge base ini.", "kb_utama"),
    ("Sebutkan topik utama dan detail pentingnya.", "kb_chat"),
    ("Jelaskan isi dokumen yang tersedia.", "kb_prj_pamjaya"),
):
    data = ask(question, kb)
    if data.get("_err"):
        print(f"TANYA: {question} | KB: {kb}\n  GAGAL: {data['_err']}\n")
        continue
    answer = (data.get("answer") or "").strip()
    foreign = CJK.findall(answer)
    print(f"TANYA: {question} | KB: {kb}")
    print("  finish_reason:", (data.get("usage") or {}).get("finish_reason"), "| panjang:", len(answer))
    print("  aksara asing :", foreign[:8] if foreign else "TIDAK ADA")
    print("  jawaban      :", answer[:170].replace("\n", " ") or "(kosong)")
    print()
