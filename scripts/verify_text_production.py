"""Verifikasi produksi (rag.aiones.app): teks asing & sampah biner lewat API publik.

Dijalankan DI DALAM container. Kunci dibaca dari /data dan tidak pernah dicetak.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
KB = "kb_uji_teks"
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

DOKUMEN = """# Panduan Teknis Perangkat

## Perakitan akhir
Dokumen final perakitan memuat wiring, firmware, dan langkah pengujian. Setiap unit diuji
selama 24 jam sebelum dikirim ke lapangan.

## Daftar komponen
| Komponen | Jumlah | Catatan |
|---|---|---|
| Sensor NPK | 1 | kalibrasi pabrik |
| Modul LoRa | 1 | jarak 5 km |
| Baterai 18650 | 1 | daya 3.7 V |

## Kalibrasi
Kalibrasi sensor dilakukan dua kali setahun memakai larutan standar.
"""


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
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode()[:250]}


gagal = 0


def cek(label: str, syarat: bool, detail: str = "") -> None:
    global gagal
    if not syarat:
        gagal += 1
    print(f"  {'OK  ' if syarat else 'GAGAL'} {label}{(' - ' + detail) if detail else ''}")


print("== 1. setelan aktif ==")
sections = ((call("GET", "/settings") or {}).get("data") or {}).get("sections") or {}
llm = sections.get("llm") or {}
print("   strip_foreign_scripts:", llm.get("strip_foreign_scripts"))
cek("filter aksara asing aktif", llm.get("strip_foreign_scripts") is not False)

print("\n== 2. dokumen normal diindeks ==")
call("POST", "/knowledge/index", {
    "document_id": "panduan_prod", "document_name": "panduan_prod.md",
    "knowledge_base_id": KB,
    "content_base64": base64.b64encode(DOKUMEN.encode()).decode(), "replace": True,
})
status = {}
for _ in range(120):
    status = (call("GET", "/knowledge/panduan_prod") or {}).get("data") or {}
    if status.get("status") in {"completed", "failed"}:
        break
    time.sleep(1)
print("   status:", status.get("status"), "| chunk:", status.get("chunks"))
cek("dokumen selesai", status.get("status") == "completed")

print("\n== 3. jawaban nyata dari model: bersih & relevan ==")
for tanya in ("Apa isi dokumen final perakitan?",
              "Sebutkan komponen dan jumlahnya dalam tabel.",
              "Bagaimana cara kalibrasi sensor?"):
    data = ((call("POST", "/query", {
        "query": tanya, "knowledge_base_id": KB, "options": {"top_k": 4},
    })) or {}).get("data") or {}
    answer = (data.get("answer") or "").strip()
    usage = data.get("usage") or {}
    asing = CJK.findall(answer)
    print(f"\n   tanya: {tanya}")
    print("   finish_reason:", usage.get("finish_reason"), "| panjang:", len(answer))
    for line in answer.splitlines()[:6]:
        print("   |", line[:115])
    cek("jawaban tidak kosong", bool(answer))
    cek("tidak ada aksara asing", not asing, str(asing[:5]) if asing else "")

print("\n== 4. dump biner ditolak ==")
dump = b"%PDF-1.4\n5 0 obj <</Length 6 0 R/Filter /FlateDecode>> stream\n" * 4
hasil = call("POST", "/knowledge/index", {
    "document_id": "dump_prod", "document_name": "dump_prod.txt",
    "knowledge_base_id": KB,
    "content_base64": base64.b64encode(dump).decode(), "replace": True,
})
if hasil.get("_error"):
    cek("ditolak di endpoint", hasil["_error"] in (400, 422), str(hasil["_error"]))
else:
    st = {}
    for _ in range(60):
        st = (call("GET", "/knowledge/dump_prod") or {}).get("data") or {}
        if st.get("status") in {"completed", "failed"}:
            break
        time.sleep(1)
    print("   status:", st.get("status"), "| error:", str(st.get("error"))[:90])
    cek("dump biner gagal, bukan jadi knowledge", st.get("status") == "failed")

print("\n== 5. bersihkan ==")
for doc in ("panduan_prod", "dump_prod"):
    call("DELETE", f"/knowledge/{doc}")

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
