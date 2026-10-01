"""Unggah berkas contoh ke layanan yang sedang jalan, lalu buktikan teksnya bisa ditemukan.

Berkas dibuat oleh scripts/make_format_samples.py (pustaka Office asli). Tiap berkas punya
penanda unik (KUA2026XXX) di dalam teks atau di sel tabel / catatan pembicara, jadi pencarian
penanda itu membuktikan teksnya benar-benar terbaca dan tersimpan, bukan sekadar "upload sukses".

    .venv/Scripts/python.exe scripts/verify_format_samples_live.py [http://127.0.0.1:8099]
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
SAMPLES = Path(__file__).resolve().parent.parent / "data" / "tmp" / "fmt"
KB = "kb_formats"
KEYS = Path(__file__).resolve().parent / "api_keys_demo.json"

MARKERS = {
    "kebijakan_cuti.docx": "KUA2026DOCX",
    "kuota_cuti.xlsx": "KUA2026XLSX",
    "rapat_rutin.pptx": "KUA2026NOTES",
    "kebijakan_cuti.odt": "KUA2026ODT",
    "buku_uji.epub": "KUA2026EPUB",
    "sop_cuti.rtf": "KUA2026RTF",
    "sop_cuti.md": "KUA2026MD",
    "catatan_latin.txt": "KUA2026LATIN",
    "kuota.csv": "KUA2026CSV",
    "kebijakan.yaml": "KUA2026YAML",
    "sop.html": "KUA2026HTML",
    "audit.log": "KUA2026LOG",
}


def key_for_admin() -> str:
    entries = json.loads(KEYS.read_text(encoding="utf-8"))
    for key, entry in entries.items():
        if "admin" in (entry.get("permissions") or []):
            return key
    raise SystemExit("tidak ada kunci admin di scripts/api_keys_demo.json")


def call(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + key)
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, {"raw": payload[:200]}


def main() -> int:
    key = key_for_admin()
    status, ready = call("GET", "/api/v1/ready", key)
    detail = ready.get("data", {}).get("detail", {})
    print(f"layanan: {BASE} status={ready.get('data', {}).get('status')} "
          f"format={len(detail.get('allowed_extensions', []))} max={detail.get('max_upload_mb')} MB")
    print("kunci uji:", key[:14] + "...")

    if not SAMPLES.exists():
        raise SystemExit(f"folder contoh belum ada: {SAMPLES} (jalankan scripts/make_format_samples.py)")

    failures: list[str] = []
    for name, marker in MARKERS.items():
        path = SAMPLES / name
        payload = {
            "document_id": "fmt_" + path.stem,
            "knowledge_base_id": KB,
            "document_name": name,
            "content_base64": base64.b64encode(path.read_bytes()).decode(),
        }
        status, response = call("POST", "/api/v1/knowledge/index", key, payload)
        if status != 202:
            failures.append(f"{name}: unggah {status} {response.get('error', response)}")
            print(f"  {name:22} TOLAK {status} {response.get('error', {}).get('code', '')}")
            continue
        document_id = payload["document_id"]
        job = {}
        for _ in range(60):
            _, listing = call("GET", f"/api/v1/knowledge/{document_id}", key)
            job = listing.get("data", {})
            if job.get("status") in {"completed", "failed", "deleted"}:
                break
            time.sleep(0.5)
        if job.get("status") != "completed":
            failures.append(f"{name}: job {job.get('status')} {job.get('error')}")
            print(f"  {name:22} job {job.get('status')}")
            continue

        _, found = call("POST", "/api/v1/search", key, {
            "query": marker,
            "knowledge_base_id": KB,
            "options": {"top_k": 3, "use_hybrid": True, "document_ids": [document_id]},
        })
        results = found.get("data", {}).get("results", [])
        hit = next((row for row in results if marker in (row.get("content") or "")), None)
        snippet = " ".join((hit.get("content") or "").split())[:96] if hit else ""
        ok = bool(hit)
        if not ok:
            failures.append(f"{name}: penanda tidak ditemukan di hasil retrieval")
        print(f"  {name:22} chunks={job.get('chunks'):>2} pages={job.get('pages')} "
              f"marker={'ADA' if ok else 'HILANG'} {snippet}")

    print("---")
    if failures:
        print(f"GAGAL ({len(failures)}):")
        for item in failures:
            print("  -", item)
        return 1
    print(f"SEMUA LULUS: {len(MARKERS)} berkas terbaca, terindeks, dan teksnya bisa ditemukan kembali.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
