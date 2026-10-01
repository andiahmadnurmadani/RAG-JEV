"""Uji hidup: unggah dokumen, ringkasan dibuat, lalu minta ringkasannya lewat /query.

Dijalankan terhadap layanan lokal (scripts/run_live.sh). Memakai model sungguhan, jadi hasilnya
menunjukkan apakah ringkasannya benar-benar terbentuk dan dipakai menjawab.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
KB = "kb_ringkas_live"

DOCUMENT = """# SOP Pengelolaan Arsip Perusahaan

## Ruang lingkup
Dokumen ini mengatur pengelolaan arsip aktif dan arsip inaktif di seluruh unit kerja.

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

## Pemindahan
Pemindahan arsip ke gudang inaktif dilakukan paling lambat tiga bulan setelah arsip dinyatakan
inaktif, memakai daftar pertelaan arsip.

## Akses
Akses arsip vital hanya diberikan kepada pengelola arsip dan pejabat yang berwenang, dengan
pencatatan pada buku peminjaman.
"""


def call(method: str, path: str, payload=None, timeout: float = 300.0, retries: int = 3) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(retries):
        request = urllib.request.Request(BASE + path, data=body, method=method)
        request.add_header("Authorization", f"Bearer {KEY}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code in (502, 503, 504) and attempt + 1 < retries:
                print(f"  (ulang {attempt + 1}: HTTP {exc.code})")
                time.sleep(4 * (attempt + 1))
                continue
            return {"_error": exc.code, "_body": detail}
    return {"_error": -1}


def main() -> int:
    print("layanan:", BASE)

    print("\n== 1. unggah dokumen (ringkasan dibuat saat indeks) ==")
    started = time.time()
    created = call(
        "POST",
        "/knowledge/index",
        {
            "document_id": "sop_arsip",
            "document_name": "SOP Pengelolaan Arsip.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(DOCUMENT.encode()).decode(),
            "replace": True,
        },
    )
    if created.get("_error"):
        print("  GAGAL mengirim:", created)
        return 1
    status = {}
    for _ in range(120):
        status = (call("GET", "/knowledge/sop_arsip") or {}).get("data") or {}
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(2)
    print(
        "  status={} chunk={} token={} ({:.0f}s)".format(
            status.get("status"), status.get("chunks"), status.get("tokens"), time.time() - started
        )
    )
    if status.get("status") != "completed":
        print("  GAGAL:", status.get("error"))
        return 1

    summary = (status.get("summary") or "").strip()
    print(f"  ringkasan: {status.get('summary_tokens')} token")
    print("  ---")
    for line in (summary or "(kosong)").splitlines()[:12]:
        print("   ", line[:150])
    print("  ---")
    if not summary:
        print("  CATATAN: ringkasan kosong -", status.get("summary_error"))
        return 1

    print("\n== 2. minta ringkasan lewat /query ==")
    started = time.time()
    data = (call(
        "POST",
        "/query",
        {"query": "Tolong ringkas isi dokumen ini.", "knowledge_base_id": KB, "options": {"top_k": 6}},
    ) or {}).get("data") or {}
    usage = data.get("usage") or {}
    print(
        "  angka : konteks={} ringkasan={} token={} keluar={} finish={!r} alasan={!r} ({:.0f}s)".format(
            usage.get("context_chunks"),
            usage.get("context_summary_chunks"),
            usage.get("context_tokens"),
            usage.get("output_tokens"),
            usage.get("finish_reason"),
            data.get("no_answer_reason"),
            time.time() - started,
        )
    )
    print("  sumber:", [item.get("chunk_id") for item in (data.get("sources") or [])][:5])
    answer = (data.get("answer") or "").strip()
    print("  jawab :")
    for line in answer.splitlines()[:14]:
        print("   ", line[:150])

    if usage.get("context_summary_chunks", 0) < 1:
        print("\n  CATATAN: ringkasan tidak ikut ke konteks.")
        return 1
    if usage.get("context_chunks", 0) < 1:
        print("\n  CATATAN: isi dokumen tidak ikut ke konteks (seharusnya tetap ikut).")
        return 1

    print("\n== 3. pertanyaan faktual harus dijawab dari isi, bukan ringkasan ==")
    data = (call(
        "POST",
        "/query",
        {"query": "Berapa tahun masa retensi arsip kepegawaian?", "knowledge_base_id": KB, "options": {"top_k": 6}},
    ) or {}).get("data") or {}
    usage = data.get("usage") or {}
    print("  angka : konteks={} ringkasan={} finish={!r}".format(
        usage.get("context_chunks"), usage.get("context_summary_chunks"), usage.get("finish_reason")))
    print("  jawab :", (data.get("answer") or "").strip().replace("\n", " ")[:220])

    print("\n== 4. bersihkan dokumen uji ==")
    print("  ", json.dumps(call("DELETE", "/knowledge/sop_arsip"))[:150])
    print("\nHASIL : ringkasan dibuat saat upload dan dipakai saat diminta")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
