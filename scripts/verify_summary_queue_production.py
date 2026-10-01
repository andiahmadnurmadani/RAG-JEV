"""Uji produksi ragjev.kii.lat: unggahan kecil tidak menunggu ringkasan dokumen besar.

Dijalankan DI DALAM container (kunci dibaca dari registry di /data, tidak pernah dicetak).
Skenario: unggah dokumen besar -> tunggu tahap "summarizing" -> unggah berkas kecil TANPA
menunggu -> ukur berapa lama isi berkas kecil siap. Kalau jalur antriannya benar-benar
terpisah, berkas kecil selesai dalam hitungan detik walau ringkasan dokumen besar masih jalan.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
KB = "kb_antrian_prod"
BIG = "antrian_besar_prod"
SMALL = "antrian_kecil_prod"


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
                time.sleep(5 * (attempt + 1))
                continue
            return {"_error": exc.code, "_body": detail}
    return {"_error": -1}


def big_document(sections: int = 50, repeat: int = 22) -> str:
    parts = ["# Laporan Besar (uji antrian)", "", "Dokumen panjang, akan diringkas bertahap.", ""]
    for index in range(1, sections + 1):
        parts.append(f"## Bagian {index}")
        parts.append(f"Bagian {index} memuat ketentuan besar_{index:02d}. " + ("Penjelasan rinci. " * repeat))
        parts.append("")
    return "\n".join(parts)


def status_of(document_id: str, key: str) -> dict:
    return (call("GET", f"/knowledge/{document_id}", key=key).get("data") or {})


def wait_until(document_id: str, key: str, timeout: float = 300.0) -> tuple[dict, float]:
    started = time.time()
    last: dict = {}
    while time.time() - started < timeout:
        last = status_of(document_id, key)
        if last.get("status") in {"completed", "failed"}:
            return last, time.time() - started
        time.sleep(0.5)
    return last, time.time() - started


def main() -> int:
    key = active_key()
    ready = ((call("GET", "/ready", key=key).get("data") or {}).get("detail") or {})
    worker = ready.get("worker") or {}
    print("layanan :", BASE)
    print("versi   :", ready.get("version"), "| app.js:", ready.get("ui_asset"))
    print("worker  :", {k: worker.get(k) for k in ("workers", "summary_workers", "queued", "summary_queued")})

    print("\n== 1. unggah dokumen BESAR ==")
    created = call(
        "POST", "/knowledge/index",
        {"document_id": BIG, "document_name": "laporan-besar-uji.md", "knowledge_base_id": KB,
         "content_base64": base64.b64encode(big_document().encode()).decode(), "replace": True},
        key=key,
    )
    if created.get("_error"):
        print("  GAGAL:", json.dumps(created)[:300])
        return 1
    saw = None
    deadline = time.time() + 120
    while time.time() < deadline:
        status = status_of(BIG, key)
        if status.get("stage") == "summarizing":
            saw = status
            break
        if status.get("status") in {"completed", "failed"}:
            saw = status
            break
        time.sleep(0.4)
    print("  tahap terlihat :", (saw or {}).get("stage"), "| status:", (saw or {}).get("status"),
          "| chunk:", (saw or {}).get("chunks"))

    print("\n== 2. unggah berkas KECIL tanpa menunggu, lalu ukur ==")
    created = call(
        "POST", "/knowledge/index",
        {"document_id": SMALL, "document_name": "kecil-uji.md", "knowledge_base_id": KB,
         "content_base64": base64.b64encode("Berkas kecil berisi kata kecilkhusus.".encode()).decode(),
         "replace": True},
        key=key,
    )
    if created.get("_error"):
        print("  GAGAL:", json.dumps(created)[:300])
        return 1
    small_status, elapsed = wait_until(SMALL, key, timeout=180)
    print(f"  berkas kecil selesai dalam {elapsed:.1f}s | status={small_status.get('status')} "
          f"| chunk={small_status.get('chunks')} | ringkasan={small_status.get('summary_tokens')} token")
    big_now = status_of(BIG, key)
    print("  dokumen besar saat itu: status={} stage={}".format(big_now.get("status"), big_now.get("stage")))

    ok = elapsed < 25
    print("\n  KESIMPULAN:", "unggahan kecil TIDAK menunggu ringkasan dokumen besar"
          if ok else f"MASIH MENUNGGU ({elapsed:.1f}s) - periksa pemisahan antrian")

    print("\n== 3. isi berkas kecil sudah bisa dicari? ==")
    hits = ((call("POST", "/search", {"query": "kecilkhusus", "knowledge_base_id": KB, "top_k": 5}, key=key)
             .get("data") or {}).get("results") or [])
    print("  hasil:", [hit.get("document_id") for hit in hits][:3])

    print("\n== 4. bersihkan dokumen uji ==")
    for document_id in (BIG, SMALL):
        print("  hapus", document_id, "->", json.dumps(call("DELETE", f"/knowledge/{document_id}", key=key))[:110])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
