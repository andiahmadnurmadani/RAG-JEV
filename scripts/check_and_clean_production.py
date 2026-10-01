"""Cek hasil di produksi: daftar dokumen + angka dua pertanyaan, lalu bersihkan unggahan ganda."""

import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
MINE = "doc_kms_telin_prod"


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


def call(method: str, path: str, payload=None, key: str = "") -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as error:
        return {"_error": error.code, "_body": error.read().decode()[:300]}


def main() -> int:
    key = active_key()
    rows = (call("GET", "/knowledge", key=key).get("data") or {}).get("documents") or []
    print("dokumen di produksi:", len(rows))
    for row in rows:
        print(
            "  - {} | id={} | kb={} | status={} | potongan={}".format(
                row.get("document_name"),
                row.get("document_id"),
                row.get("knowledge_base_id"),
                row.get("status"),
                row.get("chunks") or row.get("chunk_count"),
            )
        )

    kms = [r for r in rows if "KMS Telin" in (r.get("document_name") or "")]
    kb = (kms[0] if kms else (rows[0] if rows else {})).get("knowledge_base_id") or "default"

    for question in (
        "Sebutkan tabel yang berhubungan dengan knowledge atau helpdesk beserta kolomnya.",
        "Tolong jelaskan struktur lengkap database ini. Tabel apa saja yang ada?",
    ):
        started = time.time()
        data = (call("POST", "/query", {"query": question, "knowledge_base_id": kb, "options": {"top_k": 12}}, key=key)).get("data") or {}
        usage = data.get("usage") or {}
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
        for coverage in usage.get("document_coverage") or []:
            print("  lapor:", coverage)
        answer = (data.get("answer") or "").strip()
        print("JAWAB :", answer[:600].replace("\n", " ⏎ "))
        print("panjang jawaban:", len(answer), "karakter | ada 'tidak ditemukan':", "tidak ditemukan" in answer.lower())

    if any(r.get("document_id") == MINE for r in rows):
        print("\nmenghapus duplikat unggahan skrip:", MINE)
        print("hasil hapus:", json.dumps(call("DELETE", f"/knowledge/{MINE}", key=key))[:200])
        rows = (call("GET", "/knowledge", key=key).get("data") or {}).get("documents") or []
        print("dokumen tersisa:", [r.get("document_name") for r in rows])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
