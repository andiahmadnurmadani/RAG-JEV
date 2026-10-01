"""Uji hidup: unggahan kecil tidak boleh menunggu ringkasan dokumen besar.

Skenario yang dikeluhkan: unggah berkas kecil, statusnya "queued" lama. Uji ini mengunggah
dokumen besar (ringkasannya puluhan panggilan LLM), lalu TANPA menunggu, mengunggah berkas
kecil dan mengukur berapa lama isinya siap. Kalau jalurnya benar-benar terpisah, berkas kecil
selesai cepat - ringkasan dokumen besar jalan di latar.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
KB = "kb_antrian_live"


def call(method: str, path: str, payload=None, timeout: float = 300.0) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {KEY}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_body": exc.read().decode()[:200]}


def big_document(sections: int = 60, repeat: int = 25) -> str:
    parts = ["# Laporan Besar", "", "Dokumen ini panjang dan akan diringkas bertahap.", ""]
    for index in range(1, sections + 1):
        parts.append(f"## Bagian {index}")
        parts.append(f"Bagian {index} memuat ketentuan besar_{index:02d}. " + ("Penjelasan rinci dan contoh. " * repeat))
        parts.append("")
    return "\n".join(parts)


def wait_status(document_id: str, timeout: float = 240.0) -> tuple[dict, float]:
    started = time.time()
    last = {}
    while time.time() - started < timeout:
        last = (call("GET", f"/knowledge/{document_id}") or {}).get("data") or {}
        if last.get("status") in {"completed", "failed"}:
            return last, time.time() - started
        time.sleep(0.5)
    return last, time.time() - started


def main() -> int:
    print("layanan:", BASE)

    print("\n== 1. unggah dokumen BESAR (ringkasannya lama) ==")
    big = big_document()
    created = call(
        "POST", "/knowledge/index",
        {"document_id": "besar_uji", "document_name": "laporan-besar.md", "knowledge_base_id": KB,
         "content_base64": base64.b64encode(big.encode()).decode(), "replace": True},
    )
    if created.get("_error"):
        print("  GAGAL:", created)
        return 1

    # Tunggu sampai tahap ringkasan mulai (isinya sudah masuk, ringkasannya berjalan).
    saw_summarizing = False
    deadline = time.time() + 90
    while time.time() < deadline:
        status = (call("GET", "/knowledge/besar_uji") or {}).get("data") or {}
        if status.get("stage") == "summarizing":
            saw_summarizing = True
            print("  tahap:", status.get("stage"), "| status:", status.get("status"),
                  "| chunk:", status.get("chunks"))
            break
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(0.4)
    if not saw_summarizing:
        print("  CATATAN: tidak sempat melihat tahap 'summarizing' (mungkin ringkasannya cepat).")

    print("\n== 2. TANPA menunggu, unggah berkas KECIL dan ukur ==")
    small = "Dokumen kecil berisi kata kecilkhusus."
    created = call(
        "POST", "/knowledge/index",
        {"document_id": "kecil_uji", "document_name": "kecil.md", "knowledge_base_id": KB,
         "content_base64": base64.b64encode(small.encode()).decode(), "replace": True},
    )
    if created.get("_error"):
        print("  GAGAL:", created)
        return 1
    status, elapsed = wait_status("kecil_uji", timeout=180)
    print(f"  berkas kecil selesai dalam {elapsed:.1f}s | status={status.get('status')} | chunk={status.get('chunks')}")
    print(f"  ringkasan: {status.get('summary_tokens')} token | catatan: {(status.get('summary_error') or '-')[:80]}")

    # Cek dokumen besar masih berjalan / sudah selesai - intinya berkas kecil tidak menunggunya.
    big_status = (call("GET", "/knowledge/besar_uji") or {}).get("data") or {}
    print("  dokumen besar saat itu: status={} stage={}".format(big_status.get("status"), big_status.get("stage")))

    ok = elapsed < 25
    print("\n  kesimpulan:", "unggahan kecil TIDAK menunggu ringkasan dokumen besar" if ok
          else f"MASIH MENUNGGU ({elapsed:.1f}s) - periksa pemisahan antrian")

    print("\n== 3. beri tahu bahwa isi kecil bisa dicari walau ringkasan besar belum selesai ==")
    found = (call("POST", "/search", {"query": "kecilkhusus", "knowledge_base_id": KB, "top_k": 5}) or {}).get("data") or {}
    hits = found.get("results") or []
    print("  hasil pencarian:", [hit.get("document_id") for hit in hits][:3])

    print("\n== 4. bersihkan ==")
    for document_id in ("besar_uji", "kecil_uji"):
        print("  hapus", document_id, json.dumps(call("DELETE", f"/knowledge/{document_id}"))[:90])

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
