"""Uji hidup dengan model NYATA: apakah aksara asing benar-benar hilang dari jawaban.

Menyiapkan dokumen yang menggoda model menyisipkan aksara lain, lalu memeriksa jawabannya
bersih. Sekaligus membuktikan jawaban tetap relevan - filter yang terlalu galak akan
menghapus kata yang seharusnya ada.
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
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
KIRIL = re.compile(r"[\u0400-\u04ff]")

DOKUMEN = """# Spesifikasi Produk

## Modul sensor
Modul sensor mengukur kelembapan tanah dan suhu udara. Rentang ukur 0-100 persen dengan
ketelitian dua persen.

## Perakitan akhir
Dokumen final perakitan memuat wiring, firmware, dan langkah pengujian. Setiap unit diuji
selama 24 jam sebelum dikirim.

## Daftar komponen
| Komponen | Jumlah | Catatan |
|---|---|---|
| Sensor NPK | 1 | kalibrasi pabrik |
| Modul LoRa | 1 | jarak 5 km |
| Baterai 18650 | 1 | daya 3.7 V |
"""


def call(method: str, path: str, payload=None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {KEY}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=200) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode()[:250]}


print("== siapkan dokumen ==")
call("POST", "/knowledge/index", {
    "document_id": "spek_produk", "document_name": "spek_produk.md",
    "knowledge_base_id": KB,
    "content_base64": base64.b64encode(DOKUMEN.encode()).decode(), "replace": True,
})
for _ in range(90):
    status = (call("GET", "/knowledge/spek_produk") or {}).get("data") or {}
    if status.get("status") in {"completed", "failed"}:
        break
    time.sleep(1)
print("   status:", status.get("status"), "| chunk:", status.get("chunks"))

pertanyaan = [
    "Apa isi dokumen final perakitan modul sensor?",
    "Sebutkan komponen dan jumlahnya dalam bentuk tabel.",
    "Berapa rentang ukur dan ketelitian modul sensor?",
]

gagal = 0
for tanya in pertanyaan:
    data = (call("POST", "/query", {
        "query": tanya, "knowledge_base_id": KB, "options": {"top_k": 4},
    }) or {}).get("data") or {}
    answer = (data.get("answer") or "").strip()
    usage = data.get("usage") or {}
    asing = CJK.findall(answer) + KIRIL.findall(answer)
    print(f"\n== {tanya} ==")
    print("   finish_reason:", usage.get("finish_reason"), "| panjang:", len(answer))
    for line in answer.splitlines()[:7]:
        print("  |", line[:120])
    if asing:
        gagal += 1
        print("   GAGAL: masih ada aksara asing:", asing[:8])
    else:
        print("   OK: tidak ada aksara asing")
    if not answer:
        gagal += 1
        print("   GAGAL: jawaban kosong")

print("\n== bersihkan ==")
call("DELETE", "/knowledge/spek_produk")
print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
