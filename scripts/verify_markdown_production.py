"""Uji produksi: keluaran LLM benar-benar berformat Markdown.

Dijalankan DI DALAM container (kunci dibaca dari registry di /data, tidak pernah dicetak).
Unggah dokumen kecil berstruktur (tabel + daftar), tanya sesuatu yang jawabannya wajar
berbentuk daftar/tabel, lalu cetak jawabannya untuk diperiksa: apakah ada penanda Markdown
(judul `#`, butir `-`/`1.`, tabel `|`, tebal `**`, kode `` ` ``) - bukan teks polos.
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
KB = "kb_markdown_prod"
DOC = "markdown_uji_prod"

DOKUMEN = """# Panduan Cuti Pegawai

## Jenis cuti
Perusahaan menyediakan tiga jenis cuti: cuti tahunan, cuti sakit, dan cuti melahirkan.

## Ketentuan cuti tahunan
- Jatah 12 hari kerja per tahun.
- Diajukan paling lambat 3 hari sebelum tanggal mulai.
- Sisa cuti tidak dapat diuangkan.

## Tabel jatah cuti

| Jenis | Jatah | Catatan |
| --- | --- | --- |
| Cuti tahunan | 12 hari | hangus akhir tahun |
| Cuti sakit | 14 hari | perlu surat dokter di atas 2 hari |
| Cuti melahirkan | 90 hari | berlaku untuk pegawai perempuan |

## Kolom wajib di pengajuan
Kolom `employee_id`, `jenis_cuti`, dan `tanggal_mulai` wajib diisi di sistem.
"""


def active_key() -> str:
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
    raise SystemExit("kunci aktif tidak ditemukan")


def call(method: str, path: str, payload=None, key: str = "", retries: int = 3) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        request = urllib.request.Request(BASE + path, data=body, method=method)
        request.add_header("Authorization", f"Bearer {key}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (502, 503, 504) and attempt + 1 < retries:
                print(f"  (ulang {attempt + 1}: HTTP {exc.code})")
                time.sleep(5 * (attempt + 1))
                continue
            return {"_error": exc.code, "_body": detail}
    return {"_error": -1}


def markdown_markers(text: str) -> dict:
    return {
        "judul": len(re.findall(r"(?m)^#{1,6}\s", text)),
        "butir": len(re.findall(r"(?m)^\s*[-*+]\s", text)),
        "bernomor": len(re.findall(r"(?m)^\s*\d+[.)]\s", text)),
        "tabel": len(re.findall(r"(?m)^\s*\|.*\|", text)),
        "tebal": len(re.findall(r"\*\*[^*]+\*\*", text)),
        "kode": len(re.findall(r"`[^`]+`", text)),
    }


def main() -> int:
    key = active_key()
    print("layanan:", BASE)

    print("\n== 1. unggah dokumen berstruktur ==")
    created = call(
        "POST", "/knowledge/index",
        {"document_id": DOC, "document_name": "panduan-cuti-uji.md", "knowledge_base_id": KB,
         "content_base64": base64.b64encode(DOKUMEN.encode()).decode(), "replace": True},
        key=key,
    )
    if created.get("_error"):
        print("  GAGAL:", json.dumps(created)[:300])
        return 1
    status = {}
    for _ in range(120):
        status = (call("GET", f"/knowledge/{DOC}", key=key).get("data") or {})
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(1)
    print("  status:", status.get("status"), "| chunk:", status.get("chunks"))

    print("\n== 2. tanya (jawaban wajar berbentuk daftar/tabel) ==")
    data = (call(
        "POST", "/query",
        {"query": "Sebutkan jatah setiap jenis cuti dalam bentuk tabel, lalu sebutkan kolom wajib di pengajuan.",
         "knowledge_base_id": KB, "options": {"top_k": 6}},
        key=key,
    ).get("data") or {})
    answer = (data.get("answer") or "").strip()
    print("  panjang jawaban:", len(answer), "karakter")
    print("  finish_reason:", (data.get("usage") or {}).get("finish_reason"))
    print("  penanda Markdown:", json.dumps(markdown_markers(answer)))
    print("\n  --- jawaban (30 baris pertama) ---")
    for line in answer.splitlines()[:30]:
        print("  |", line[:150])

    total = sum(markdown_markers(answer).values())
    print("\n  KESIMPULAN:", "jawaban BERFORMAT MARKDOWN" if total >= 3
          else f"jawaban tampak teks polos (penanda={total})")

    print("\n== 3. bersihkan ==")
    print("  ", json.dumps(call("DELETE", f"/knowledge/{DOC}", key=key))[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
