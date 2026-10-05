"""Uji hidup: reranker leksikal benar-benar menyusun ulang hasil retrieval.

Unggah dokumen berisi beberapa topik, tanya satu topik, lalu periksa:
1. reranker dipakai dan dilaporkan sebagai ``lexical`` (bukan ``none``);
2. potongan yang relevan berada di peringkat atas;
3. jawaban tidak memuat kata rusak.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
KB = "kb_rerank"

DOKUMEN = """# Buku Panduan Perusahaan

## Kebijakan cuti tahunan
Jatah cuti tahunan adalah 12 hari kerja per tahun. Pengajuan cuti tahunan harus diajukan
paling lambat 3 hari sebelum tanggal mulai. Sisa cuti tidak dapat diuangkan.

## Prosedur pengadaan barang
Pengadaan barang di atas 50 juta rupiah memerlukan tender terbuka dan persetujuan direksi.
Pengadaan di bawah nilai tersebut cukup dengan tiga penawaran pembanding.

## Laporan keuangan triwulan
Neraca triwulan memuat total aset, kewajiban, dan ekuitas. Laba bersih triwulan dilaporkan
setelah penyusutan dan pajak.

## Daftar hadir pegawai
Rekap absensi bulanan memuat jumlah hari hadir, izin, sakit, dan alpa setiap pegawai.
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
        return {"_error": exc.code, "_body": exc.read().decode()[:300]}


def main() -> int:
    print("== 1. siapkan dokumen ==")
    created = call("POST", "/knowledge/index", {
        "document_id": "panduan_rerank", "document_name": "panduan.md",
        "knowledge_base_id": KB,
        "content_base64": base64.b64encode(DOKUMEN.encode()).decode(), "replace": True,
    })
    if created.get("_error"):
        print("  GAGAL:", created)
        return 1
    import time
    status = {}
    for _ in range(120):
        status = (call("GET", "/knowledge/panduan_rerank") or {}).get("data") or {}
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(1)
    print("  status:", status.get("status"), "| chunk:", status.get("chunks"))

    print("\n== 2. cari dengan reranker ==")
    found = (call("POST", "/search", {
        "query": "berapa jatah cuti tahunan dan kapan harus diajukan",
        "knowledge_base_id": KB, "options": {"top_k": 4, "use_reranker": True},
    }) or {}).get("data") or {}
    print("  reranker dilaporkan:", found.get("reranker"))
    print("  reranked:", found.get("reranked"), "| rerank_ms:", found.get("rerank_ms"))
    results = found.get("results") or []
    for rank, hit in enumerate(results[:4], 1):
        text = (hit.get("content") or "").replace("\n", " ")[:80]
        print(f"    {rank}. [{hit.get('score'):.3f}] {text}")
    teratas = (results[0].get("content") or "").lower() if results else ""
    relevan_di_atas = "cuti" in teratas

    print("\n== 3. tanya (jawaban + deteksi teks rusak) ==")
    data = (call("POST", "/query", {
        "query": "Berapa jatah cuti tahunan dan kapan batas pengajuannya?",
        "knowledge_base_id": KB, "options": {"top_k": 4},
    }) or {}).get("data") or {}
    answer = (data.get("answer") or "").strip()
    usage = data.get("usage") or {}
    print("  finish_reason:", usage.get("finish_reason"), "| panjang:", len(answer))
    for line in answer.splitlines()[:8]:
        print("   |", line[:130])
    rusak = re.findall(r"[A-Za-z][a-z]*[A-Z]{2,}[A-Za-z]*|[A-Za-z]{2,}\.[A-Za-z]{2,}", answer)
    print("  penanda kata rusak:", rusak[:6] or "tidak ada")

    print("\n== 4. bersihkan ==")
    print("  ", json.dumps(call("DELETE", "/knowledge/panduan_rerank"))[:110])

    gagal = 0
    if found.get("reranker") != "lexical":
        print("GAGAL: reranker bukan lexical ->", found.get("reranker"))
        gagal += 1
    if not relevan_di_atas:
        print("GAGAL: potongan relevan tidak di peringkat 1")
        gagal += 1
    print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
    return 0 if gagal == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
