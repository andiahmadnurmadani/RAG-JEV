"""Verifikasi produksi: tanya dokumen besar yang sudah ada di knowledge, tanpa mencetak rahasia.

Dijalankan di dalam container aplikasi produksi. Membaca registry kunci API sendiri, memakai
kunci aktif pertama untuk memanggil API, dan hanya mencetak angka + jawaban (tanpa nilai kunci).
"""

import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"


def active_key() -> str:
    candidates = [
        Path("/data/api_keys.json"),
        Path("/data/bootstrap_admin_key.json"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "key" in data:
            return data["key"]
        records = data.get("keys") if isinstance(data, dict) else data
        if isinstance(records, list):
            for record in records:
                if record.get("revoked_at") or record.get("revoked"):
                    continue
                if record.get("key"):
                    return record["key"]
        if isinstance(records, dict):
            for record in records.values():
                if record.get("revoked_at") or record.get("revoked"):
                    continue
                if record.get("key"):
                    return record["key"]
    raise SystemExit("kunci aktif tidak ditemukan")


def call(method: str, path: str, payload=None, key: str = "") -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Organization-Id", "default")
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode())


def main() -> int:
    key = active_key()
    documents = call("GET", "/knowledge", key=key)
    rows = documents.get("data", {}).get("documents") or documents.get("data") or []
    print("dokumen di produksi:")
    for row in rows[:20]:
        print("  -", row.get("document_name"), "|", row.get("status"), "|", row.get("chunks") or row.get("chunk_count"), "potongan")

    target = None
    for row in rows:
        if "KMS Telin" in (row.get("document_name") or ""):
            target = row
            break
    if target is None:
        print("CATATAN: dokumen KMS Telin belum ada di produksi - unggah dulu lewat UI.")
        return 0

    print("\ndokumen target:", target.get("document_name"), "|", target.get("document_id"))
    for question in (
        "Sebutkan tabel yang berhubungan dengan knowledge atau helpdesk beserta kolomnya.",
        "Berapa jumlah tabel di database ini secara keseluruhan?",
    ):
        started = time.time()
        result = call(
            "POST",
            "/query",
            {
                "query": question,
                "knowledge_base_id": target.get("knowledge_base_id") or "default",
                "options": {"top_k": 12},
            },
            key=key,
        )
        data = result.get("data", {})
        usage = data.get("usage") or {}
        coverage = (usage.get("document_coverage") or [{}])[0]
        print("\n" + "=" * 70)
        print("TANYA :", question)
        print(
            "angka : konteks={} pelengkap={} token={} keluar={} finish={!r} alasan={!r} ({:.0f}s)".format(
                usage.get("context_chunks"),
                usage.get("context_expanded_chunks"),
                usage.get("context_tokens"),
                usage.get("output_tokens"),
                usage.get("finish_reason"),
                data.get("no_answer_reason"),
                time.time() - started,
            )
        )
        print("lapor :", coverage)
        print("JAWAB :")
        print((data.get("answer") or "").strip()[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
