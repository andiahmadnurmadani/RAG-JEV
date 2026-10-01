"""Unggah berkas besar ke produksi lalu tanyakan isinya - dijalankan di dalam container.

Membaca kunci API dari registry di /data (tanpa mencetak nilainya), mengunggah PDF dari
/root/kms_telin.pdf, menunggu pekerjaan selesai, lalu menanyakan dua hal dan mencetak angkanya.
"""

import base64
import json
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
PDF = Path("/root/kms_telin.pdf")
DOCUMENT_ID = "doc_kms_telin_prod"


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
    with urllib.request.urlopen(request, timeout=600) as response:
        raw = response.read().decode()
        return json.loads(raw) if raw.strip() else {}


def main() -> int:
    key = active_key()
    documents = (call("GET", "/knowledge", key=key).get("data") or {})
    rows = documents.get("documents") or []
    print("dokumen sebelum:", len(rows))
    for row in rows[:10]:
        print("  -", row.get("document_name"), "| kb:", row.get("knowledge_base_id"), "|", row.get("status"))
    kb = rows[0].get("knowledge_base_id") if rows else "default"
    print("knowledge base dipakai:", kb)

    if not PDF.exists():
        raise SystemExit(f"berkas {PDF} tidak ada di container")
    content = base64.b64encode(PDF.read_bytes()).decode("ascii")
    print(f"mengunggah {PDF.name} ({PDF.stat().st_size / 1024:.0f} KB, base64 {len(content) / 1024 / 1024:.2f} MB) ...")
    started = time.time()
    call(
        "POST",
        "/knowledge/index",
        {
            "document_id": DOCUMENT_ID,
            "document_name": PDF.name,
            "knowledge_base_id": kb,
            "content_base64": content,
            "replace": True,
        },
        key=key,
    )
    status = {}
    while time.time() - started < 900:
        status = ((call("GET", f"/knowledge/{DOCUMENT_ID}", key=key) or {}).get("data") or {})
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(3)
    print(
        "indeks selesai: status={} potongan={} token={} ({:.0f}s)".format(
            status.get("status"), status.get("chunks") or status.get("chunk_count"), status.get("tokens"), time.time() - started
        )
    )
    if status.get("status") != "completed":
        print("GAGAL:", json.dumps(status)[:600])
        return 1

    for question in (
        "Sebutkan tabel yang berhubungan dengan knowledge atau helpdesk beserta kolomnya.",
        "Tolong jelaskan struktur lengkap database ini. Tabel apa saja yang ada?",
    ):
        started = time.time()
        data = (call("POST", "/query", {"query": question, "knowledge_base_id": kb, "options": {"top_k": 12}}, key=key)).get("data") or {}
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
        print((data.get("answer") or "").strip()[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
