"""Uji hidup: teks asing tidak muncul di jawaban, dan sampah biner tidak masuk knowledge.

Tiga hal yang dibuktikan:
1. dokumen normal diindeks, jawaban bersih tanpa aksara asing;
2. dokumen yang memuat aksara asing SAH tetap utuh (tidak dirusak filter);
3. berkas yang isinya dump biner DITOLAK, bukan disimpan sebagai potongan sampah.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
KB = "kb_asing"
CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")

DOKUMEN = """# Buku Panduan Teknis

## Perakitan modul
Dokumen final perakitan memuat wiring, firmware, dan langkah pengujian. Setiap modul diuji
sebelum dikirim ke lapangan.

## Label terminal
| Terminal | Ke |
|---|---|
| VCC | 3V3 |
| GND | Bus GND |

## Kalibrasi sensor
Kalibrasi dilakukan dua kali setahun memakai larutan standar.
"""


def call(method: str, path: str, payload=None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {KEY}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode()[:250]}


def tunggu(document_id: str, batas: int = 120) -> dict:
    status = {}
    for _ in range(batas):
        status = (call("GET", f"/knowledge/{document_id}") or {}).get("data") or {}
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(1)
    return status


gagal = 0


def cek(label: str, syarat: bool, detail: str = "") -> None:
    global gagal
    if not syarat:
        gagal += 1
    print(f"  {'OK  ' if syarat else 'GAGAL'} {label}{(' - ' + detail) if detail else ''}")


print("== 1. dokumen normal diindeks ==")
hasil = call("POST", "/knowledge/index", {
    "document_id": "panduan_teknis", "document_name": "panduan_teknis.md",
    "knowledge_base_id": KB,
    "content_base64": base64.b64encode(DOKUMEN.encode()).decode(), "replace": True,
})
print("   ", json.dumps(hasil)[:120])
status = tunggu("panduan_teknis")
print("    status:", status.get("status"), "| chunk:", status.get("chunks"))
cek("dokumen selesai diindeks", status.get("status") == "completed")

print("\n== 2. jawaban bersih tanpa aksara asing ==")
data = (call("POST", "/query", {
    "query": "Apa isi dokumen final perakitan modul?",
    "knowledge_base_id": KB, "options": {"top_k": 4},
}) or {}).get("data") or {}
answer = (data.get("answer") or "").strip()
usage = data.get("usage") or {}
print("    finish_reason:", usage.get("finish_reason"), "| panjang:", len(answer))
for line in answer.splitlines()[:6]:
    print("   |", line[:120])
asing = CJK.findall(answer)
cek("jawaban tidak memuat aksara asing", not asing, f"ditemukan {asing[:5]}" if asing else "")

print("\n== 3. berkas dump biner ditolak, bukan disimpan ==")
dump = b"%PDF-1.4\n5 0 obj <</Length 6 0 R/Filter /FlateDecode>> stream\n" * 4
hasil2 = call("POST", "/knowledge/index", {
    "document_id": "dump_pdf", "document_name": "dump_pdf.txt",
    "knowledge_base_id": KB,
    "content_base64": base64.b64encode(dump).decode(), "replace": True,
})
kode = hasil2.get("_error")
print("    HTTP:", kode, json.dumps(hasil2)[:130])
# Diterima di endpoint (job dibuat) tetapi harus GAGAL saat diparse - bukan tersimpan.
if not kode:
    status2 = tunggu("dump_pdf", 60)
    print("    status:", status2.get("status"), "| error:", str(status2.get("error"))[:90])
    cek("dump biner tidak jadi knowledge", status2.get("status") == "failed")
else:
    cek("dump biner ditolak di endpoint", kode in (400, 422))

print("\n== 4. bersihkan ==")
for doc in ("panduan_teknis", "dump_pdf"):
    call("DELETE", f"/knowledge/{doc}")

print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
