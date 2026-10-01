"""Verifikasi live "dokumen besar" terhadap layanan yang SEDANG jalan (model LLM nyata).

Keluhan yang dijawab skrip ini: PDF struktur database 20 halaman diunggah, ditanya, dan
jawabannya berbunyi "tidak lengkap - chunk terpotong, info tidak ditemukan di konteks".
Skrip mengulang jalur itu dari ujung ke ujung - unggah berkas aslinya, tanya dengan pertanyaan
yang menyangkut seluruh dokumen, lalu periksa angka kelengkapannya.

Pemakaian:
    bash scripts/run_live.sh                     # di terminal lain
    .venv/Scripts/python.exe scripts/verify_large_document_live.py
    .venv/Scripts/python.exe scripts/verify_large_document_live.py --pdf "C:/path/lain.pdf"
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:8099/api/v1"
DEFAULT_KEY = "live-key-org-a"          # kunci demo (scripts/api_keys_demo.json), izin read+write+admin
DEFAULT_PDF = Path(r"C:\Users\Andi Ahmad Nurmadani\Downloads\Struktur Lengkap Database KMS Telin (2).pdf")
KB = "kb_large_live"
QUESTIONS = (
    "Tolong jelaskan struktur lengkap database ini. Tabel apa saja yang ada, dan sebutkan kolom yang kamu temukan.",
    "Apakah ada tabel yang berhubungan dengan knowledge atau helpdesk? Sebutkan tabel dan kolomnya.",
)


@dataclass
class Live:
    base: str
    key: str

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 300.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def index(self, pdf: Path, document_id: str) -> dict:
        payload = {
            "document_id": document_id,
            "document_name": pdf.name,
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(pdf.read_bytes()).decode("ascii"),
            "replace": True,
        }
        self.call("POST", "/knowledge/index", payload)
        deadline = time.time() + 300
        while time.time() < deadline:
            record = (self.call("GET", f"/knowledge/{document_id}") or {}).get("data") or {}
            if record.get("status") in {"completed", "failed"}:
                return record
            time.sleep(0.5)
        raise SystemExit("indexing tidak selesai dalam 300 detik")

    def ask(self, question: str) -> dict:
        return (self.call("POST", "/query", {"query": question, "knowledge_base_id": KB}) or {}).get("data") or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="verifikasi dokumen besar pada layanan yang jalan")
    parser.add_argument("--pdf", default=str(DEFAULT_PDF), help="berkas yang diuji")
    parser.add_argument("--base", default=DEFAULT_BASE, help="alamat layanan")
    parser.add_argument("--key", default=DEFAULT_KEY, help="kunci API layanan")
    parser.add_argument("--document-id", default="doc_large_live", help="id dokumen di indeks")
    args = parser.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        print(f"berkas tidak ada: {pdf}")
        return 2

    live = Live(base=args.base.rstrip("/"), key=args.key)
    print(f"layanan  : {live.base}")
    print(f"berkas   : {pdf} ({pdf.stat().st_size / 1024:.0f} KB)")

    record = live.index(pdf, args.document_id)
    print(f"status   : {record.get('status')} - {record.get('chunks')} potongan, {record.get('tokens')} token")
    if record.get("status") != "completed":
        print("pengindeksan gagal:", record)
        return 1

    failed = False
    for question in QUESTIONS:
        print("\n" + "=" * 78)
        print("TANYA :", question)
        started = time.time()
        try:
            data = live.ask(question)
        except urllib.error.HTTPError as error:
            print("GAGAL :", error.code, error.read().decode("utf-8", "replace")[:300])
            failed = True
            continue
        usage = data.get("usage") or {}
        coverage = (usage.get("document_coverage") or [{}])[0]
        print(
            "angka : retrieved={} konteks={} pelengkap={} token={} keluar={} berhenti={!r} alasan={!r} ({:.0f}s)".format(
                usage.get("retrieved_chunks"),
                usage.get("context_chunks"),
                usage.get("context_expanded_chunks"),
                usage.get("context_tokens"),
                usage.get("output_tokens"),
                usage.get("finish_reason"),
                data.get("no_answer_reason"),
                time.time() - started,
            )
        )
        print(f"lapor : {coverage}")
        print("JAWAB :")
        print((data.get("answer") or "").strip()[:1500])
        if not coverage.get("complete"):
            print("CATATAN: dokumen tidak utuh di konteks - naikkan CONTEXT_MAX_TOKENS.")
            failed = True
        if data.get("no_answer_reason") == "answer_truncated":
            print("CATATAN: jawaban terpotong batas token keluaran - naikkan LLM_MAX_TOKENS.")
            failed = True
        answer = (data.get("answer") or "").lower()
        for marker in ("tidak lengkap", "tidak ditemukan"):
            if marker in answer:
                print(f"CATATAN: jawaban masih memuat '{marker}' - periksa apakah itu memang soal datanya.")
    print("\n" + "=" * 78)
    print("HASIL :", "gagal" if failed else "lengkap - seluruh dokumen masuk konteks")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
