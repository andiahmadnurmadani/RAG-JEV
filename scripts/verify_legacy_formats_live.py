"""Verifikasi live format berkas lengkap + jaminan tidak ada data yang hilang.

Dijalankan terhadap layanan yang hidup (default http://127.0.0.1:8099):

1. pastikan ekstensi lama (.doc/.ppt/.xls) benar-benar aktif di /ready;
2. unggah berkas nyata: Word 97 (.doc), PowerPoint 97 (.ppt), Excel 97 (.xls),
   berkas .doc yang isinya RTF/HTML, dan berkas .xlsx milik pengguna (100 baris x 25 kolom);
3. tunggu sampai pengindeksan selesai, lalu tanyakan isinya dan periksa jawabannya;
4. tampilkan tabel yang terbaca (GET /tables) untuk dibandingkan dengan isi berkas asli.

Jalankan:  .venv/Scripts/python.exe scripts/verify_legacy_formats_live.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("RAG_BASE_URL", "http://127.0.0.1:8099")
KEY = os.environ.get("RAG_API_KEY", "live-key-org-a")
ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "data" / "tmp" / "legacy"
REAL_XLSX = Path("C:/Users/Andi Ahmad Nurmadani/Downloads/Data_Penjualan_100_Data.xlsx")

BARU = [".doc", ".ppt", ".xls", ".docm", ".pptm", ".eml"]


def call(method: str, path: str, payload: dict | None = None, timeout: int = 180) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{BASE}/api/v1{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {KEY}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        raise RuntimeError(f"{method} {path} -> {exc.code} {body[:400]}") from exc


def upload(path: Path, knowledge_base: str) -> str:
    document_id = f"{knowledge_base}-{path.stem.lower().replace(' ', '_')}"
    payload = {
        "document_id": document_id,
        "knowledge_base_id": knowledge_base,
        "document_name": path.name,
        "content_base64": base64.b64encode(path.read_bytes()).decode(),
        "metadata": {"sumber": "verifikasi format"},
    }
    result = call("POST", "/knowledge/index", payload)
    print(f"  unggah {path.name:<32} -> job {result['data']['job_id'][:12]}")
    return document_id


def wait_indexed(knowledge_base: str, document_ids: list[str], timeout: int = 300) -> None:
    deadline = time.time() + timeout
    pending = set(document_ids)
    while pending and time.time() < deadline:
        listed = call("GET", f"/knowledge?knowledge_base_id={knowledge_base}")["data"]["documents"]
        status = {row["document_id"]: row["status"] for row in listed}
        pending = {doc for doc in pending if status.get(doc) not in ("completed", "indexed", "failed")}
        if pending:
            time.sleep(3)
    for doc in document_ids:
        row = [item for item in call("GET", f"/knowledge?knowledge_base_id={knowledge_base}")["data"]["documents"]
               if item["document_id"] == doc]
        state = row[0]["status"] if row else "hilang"
        print(f"  status {doc:<34} = {state}")
        if state not in ("completed", "indexed"):
            raise SystemExit(f"status tak terduga untuk {doc}: {state}")



def ask(question: str, knowledge_base: str) -> dict:
    result = call("POST", "/query", {"query": question, "knowledge_base_id": knowledge_base})
    data = result["data"]
    answer = (data.get("answer") or "").strip().replace("\n", " ")
    sources = [item.get("document_name", "?") for item in data.get("sources", [])]
    computed = data.get("computed") or []
    print(f"\n  T: {question}")
    print(f"  J: {answer[:300]}")
    print(f"  sumber: {sources} | computed: {bool(computed)}")
    if computed:
        print(f"     {json.dumps(computed, ensure_ascii=False)[:220]}")
    return data


def main() -> int:
    print("== 1. /ready ==")
    detail = call("GET", "/ready")["data"]["detail"]
    allowed = detail.get("allowed_extensions", [])
    print(f"  ekstensi aktif: {len(allowed)}")
    print(f"  format lama aktif: {[item for item in allowed if item in BARU]}")
    missing = [item for item in BARU if item not in allowed]
    if missing:
        print(f"  PERINGATAN: belum aktif -> {missing} (nyalakan di Pengaturan)")

    print("\n== 2. unggah berkas ==")
    berkas = [name for name in ("sop_cuti_lama.doc", "presentasi_lama.ppt", "laporan_penjualan_lama.xls",
                                "catatan_html.doc", "lpj_rtf.doc") if (LEGACY / name).exists()]
    ids = [upload(LEGACY / name, "kb_format") for name in berkas]
    if REAL_XLSX.exists():
        ids.append(upload(REAL_XLSX, "kb_nyata"))

    print("\n== 3. tunggu pengindeksan ==")
    wait_indexed("kb_format", ids[:len(berkas)])
    if len(ids) > len(berkas):
        wait_indexed("kb_nyata", ids[len(berkas):])

    print("\n== 4. tanya isi berkas ==")
    hasil = {}
    hasil["doc"] = ask("Apa isi SOP cuti tahunan dan berapa kuota cutinya?", "kb_format")
    hasil["ppt"] = ask("Apa target kuartal pada presentasi tahunan?", "kb_format")
    hasil["xls"] = ask("Berapa total penjualan pada lembar Penjualan berkas xls lama?", "kb_format")
    hasil["doc_html"] = ask("Apa isi catatan yang sebenarnya berkas HTML?", "kb_format")
    hasil["doc_rtf"] = ask("Apa isi laporan yang sebenarnya berkas RTF?", "kb_format")
    if len(ids) > len(berkas):
        hasil["xlsx_total"] = ask("Berapa total Total Penjualan seluruhnya?", "kb_nyata")
        hasil["xlsx_tanggal"] = ask("Berapa rata-rata kolom Tanggal?", "kb_nyata")

    print("\n== 5. tabel yang terbaca ==")
    tables = call("GET", "/tables")["data"]
    print(f"  jumlah tabel: {tables.get('tabel')}")
    for tabel in tables.get("tables", []):
        notes = "; ".join(tabel.get("notes") or [])
        print(f"  - {tabel.get('document_name')} / {tabel.get('sheet')}: "
              f"{tabel.get('row_count')} baris x {len(tabel.get('headers', []))} kolom"
              f"{' (TERPOTONG)' if tabel.get('truncated') else ''}"
              f"{(' | ' + notes) if notes else ''}")

    out = ROOT / "data" / "tmp" / "verify_legacy_live.json"
    out.write_text(json.dumps(hasil, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nhasil mentah: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
