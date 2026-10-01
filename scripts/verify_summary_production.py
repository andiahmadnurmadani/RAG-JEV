"""Uji hidup di produksi: unggah dokumen, ringkasan dibuat, minta ringkasan, tanya fakta.

Tidak mencetak rahasia: kunci dibaca dari registry di dalam container.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
KB = "kb_ringkas_prod"
DOCUMENT_ID = "sop_arsip_prod"

DOCUMENT = """# SOP Pengelolaan Arsip Perusahaan

## Ruang lingkup
Dokumen ini mengatur pengelolaan arsip aktif dan inaktif di seluruh unit kerja.

## Klasifikasi arsip
Arsip dibagi tiga: arsip dinamis (masa pakai di bawah 2 tahun), arsip statis (permanen), dan
arsip vital (menyangkut hak dan kewajiban hukum perusahaan).

## Retensi
Arsip keuangan disimpan 10 tahun. Arsip kepegawaian disimpan 30 tahun setelah pegawai berhenti.
Arsip operasional disimpan 5 tahun. Arsip yang mengandung data pribadi wajib dihapus setelah
masa retensinya berakhir, kecuali diwajibkan lain oleh peraturan.

## Pemusnahan
Pemusnahan hanya boleh dilakukan setelah mendapat persetujuan tim penilai arsip, dituangkan
dalam berita acara yang ditandatangani pimpinan unit dan pengelola arsip.

## Akses
Akses arsip vital hanya diberikan kepada pengelola arsip dan pejabat yang berwenang, dengan
pencatatan pada buku peminjaman.
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


def main() -> int:
    key = active_key()
    ready = call("GET", "/ready", key=key)
    detail = ((ready.get("data") or {}).get("detail") or {})
    print("model LLM produksi:", detail.get("llm_model"), "| max_tokens:", detail.get("llm_max_tokens"))
    summary_settings = ((call("GET", "/settings", key=key).get("data") or {}).get("sections") or {}).get("summary")
    print("setelan ringkasan:", json.dumps(summary_settings))

    print("\n== 1. unggah dokumen ==")
    started = time.time()
    created = call(
        "POST",
        "/knowledge/index",
        {
            "document_id": DOCUMENT_ID,
            "document_name": "SOP Pengelolaan Arsip (uji).md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(DOCUMENT.encode()).decode(),
            "replace": True,
        },
        key=key,
    )
    if created.get("_error"):
        print("  GAGAL:", json.dumps(created)[:300])
        return 1
    status = {}
    for _ in range(120):
        status = (call("GET", f"/knowledge/{DOCUMENT_ID}", key=key).get("data") or {})
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(2)
    print(
        "  status={} chunk_isi={} token={} ({:.0f}s)".format(
            status.get("status"), status.get("chunks"), status.get("tokens"), time.time() - started
        )
    )
    if status.get("status") != "completed":
        print("  GAGAL:", status.get("error"))
        return 1

    summary = (status.get("summary") or "").strip()
    print(f"  ringkasan: {status.get('summary_tokens')} token")
    for line in (summary or "(kosong)").splitlines()[:10]:
        print("   ", line[:150])
    if not summary:
        print("  CATATAN: ringkasan kosong -", status.get("summary_error"))

    print("\n== 2. minta ringkasan ==")
    data = (call(
        "POST", "/query",
        {"query": "Tolong ringkas isi dokumen ini.", "knowledge_base_id": KB, "options": {"top_k": 6}},
        key=key,
    ).get("data") or {})
    usage = data.get("usage") or {}
    print("  angka : konteks_isi={} ringkasan={} token={} finish={!r}".format(
        usage.get("context_chunks"), usage.get("context_summary_chunks"),
        usage.get("context_tokens"), usage.get("finish_reason")))
    print("  sumber:", [item.get("chunk_id") for item in (data.get("sources") or [])][:4])
    print("  jawab :", (data.get("answer") or "").strip().replace("\n", " ")[:260])

    print("\n== 3. pertanyaan faktual (harus dari isi, bukan ringkasan) ==")
    data = (call(
        "POST", "/query",
        {"query": "Berapa tahun masa retensi arsip kepegawaian?", "knowledge_base_id": KB, "options": {"top_k": 6}},
        key=key,
    ).get("data") or {})
    usage = data.get("usage") or {}
    print("  angka : konteks_isi={} ringkasan={} finish={!r}".format(
        usage.get("context_chunks"), usage.get("context_summary_chunks"), usage.get("finish_reason")))
    print("  jawab :", (data.get("answer") or "").strip().replace("\n", " ")[:220])

    print("\n== 4. bersihkan dokumen uji ==")
    print("  ", json.dumps(call("DELETE", f"/knowledge/{DOCUMENT_ID}", key=key))[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
