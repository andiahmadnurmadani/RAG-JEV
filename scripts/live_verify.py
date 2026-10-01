"""Live end-to-end verification against the running RAG API (real LLM, real PDF)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY_A = "live-key-org-a"
KEY_B = "live-key-org-b"
PDF = "file:///C:/Users/Andi Ahmad Nurmadani/Downloads/Modul_Hosting_Kroombox_v2.2.pdf"

SOP = """SOP Penanganan Insiden Keamanan Informasi - PT Telin
Pasal 1 - Tujuan
SOP ini mengatur tata cara pelaporan dan penanganan insiden keamanan informasi di lingkungan perusahaan.
Pasal 2 - Definisi Insiden
Insiden keamanan informasi adalah setiap kejadian yang mengancam kerahasiaan, keutuhan, atau ketersediaan aset informasi.
Contoh insiden: kebocoran data pelanggan, serangan ransomware, akses tidak sah ke basis data produksi, dan kehilangan perangkat kerja.
Pasal 3 - Klasifikasi Insiden
Insiden diklasifikasikan menjadi tiga tingkat. Tingkat 1 adalah insiden kritis dengan dampak lintas unit dan potensi pelanggaran regulasi.
Tingkat 2 adalah insiden mayor yang mengganggu layanan produksi lebih dari empat jam.
Tingkat 3 adalah insiden minor dengan dampak terbatas pada satu pengguna atau satu perangkat.
Pasal 4 - Batas Waktu Pelaporan
Setiap pegawai wajib melaporkan insiden paling lambat dua jam sejak pertama kali diketahui melalui kanal resmi security@perusahaan.co.id.
Insiden Tingkat 1 wajib dieskalasi ke CISO dalam waktu tiga puluh menit dan kepada regulator paling lambat tiga hari kerja.
Pasal 5 - Tim Tanggap Insiden
Tim tanggap insiden terdiri dari analis SOC, pemilik sistem, perwakilan hukum, dan perwakilan komunikasi korporat.
Ketua tim ditunjuk oleh CISO dan bertanggung jawab menyusun laporan pasca-insiden paling lambat tujuh hari kerja setelah insiden ditutup.
Pasal 6 - Sanksi
Keterlambatan pelaporan tanpa alasan yang sah dikenakan sanksi sesuai peraturan kepegawaian yang berlaku.
"""


def call(method, path, key, payload=None, timeout=300):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + key)
    if data:
        request.add_header("Content-Type", "application/json")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode())
            return response.status, body, round((time.time() - started) * 1000)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}"), round((time.time() - started) * 1000)


def wait_for(document_id, key, tries=90):
    for _ in range(tries):
        status, body, _ = call("GET", f"/knowledge/{document_id}", key)
        data = (body or {}).get("data", {})
        if data.get("status") in {"completed", "failed"}:
            return data
        time.sleep(1)
    return {"status": "timeout"}


results = []


def record(label, ok, detail):
    results.append((label, ok, detail))
    print(("PASS " if ok else "FAIL ") + label + " -> " + str(detail)[:400])


# 1. liveness + readiness
status, body, _ = call("GET", "/health", KEY_A)
record("GET /health (liveness)", status == 200 and body.get("status") == "ok", body)
status, body, _ = call("GET", "/ready", KEY_A)
ready_before = (body.get("data") or {})
record(
    "GET /ready reports every dependency",
    status == 200 and set(ready_before.get("dependencies", {})) >= {"qdrant", "embedding", "reranker", "llm", "jev"},
    ready_before.get("dependencies"),
)

# 2. index a real PDF (file://) and an inline SOP into the SAME knowledge base
status, body, _ = call(
    "POST",
    "/knowledge/index",
    KEY_A,
    {
        "document_id": "doc_kroombox",
        "knowledge_base_id": "kb_docs",
        "document_name": "Modul_Hosting_Kroombox_v2.2.pdf",
        "file_url": PDF,
    },
)
record("index PDF Kroombox (file://)", status in (200, 202), f"http={status} {(body.get('data') or {}).get('status')}")
pdf_doc = wait_for("doc_kroombox", KEY_A)
record(
    "PDF indexing completes",
    pdf_doc.get("status") == "completed",
    {k: pdf_doc.get(k) for k in ("status", "chunks", "pages", "vectors_in_store", "tokens")},
)

status, body, _ = call(
    "POST",
    "/knowledge/index",
    KEY_A,
    {
        "document_id": "doc_sop_insiden",
        "knowledge_base_id": "kb_docs",
        "document_name": "SOP-Insiden-Keamanan.txt",
        "text": SOP,
    },
)
record("index inline SOP (same KB)", status in (200, 202), f"http={status}")
sop_doc = wait_for("doc_sop_insiden", KEY_A)
record(
    "SOP indexing completes (2nd doc, same KB)",
    sop_doc.get("status") == "completed",
    {k: sop_doc.get(k) for k in ("status", "chunks", "pages", "vectors_in_store")},
)

status, body, _ = call("GET", "/ready", KEY_A)
ready_after = (body.get("data") or {})
record(
    "/ready turns healthy once the collection is populated",
    ready_after.get("status") == "ready" and (ready_after.get("detail", {}).get("collection", {}) or {}).get("points", 0) > 0,
    {"status": ready_after.get("status"), "collection": ready_after.get("detail", {}).get("collection")},
)

# 3. /search — multi-document retrieval in one KB without the LLM
status, body, ms = call(
    "POST",
    "/search",
    KEY_A,
    {"query": "batas waktu pelaporan insiden dan sanksi keterlambatan", "knowledge_base_id": "kb_docs", "top_k": 5},
)
hits = (body.get("data") or {}).get("results", [])
record(
    "/search finds the SOP document",
    status == 200 and any(h["document_id"] == "doc_sop_insiden" for h in hits),
    [(h["document_id"], h["chunk_id"], h["score"]) for h in hits],
)

status, body, ms = call(
    "POST",
    "/search",
    KEY_A,
    {"query": "deploy aplikasi lewat panel hosting", "knowledge_base_id": "kb_docs", "top_k": 5},
)
hits = (body.get("data") or {}).get("results", [])
record(
    "/search finds the PDF document",
    status == 200 and any(h["document_id"] == "doc_kroombox" for h in hits),
    [(h["document_id"], h["chunk_id"], h["score"]) for h in hits],
)

# 4. /query with the real LLM
status, body, ms = call(
    "POST",
    "/query",
    KEY_A,
    {
        "query": "Berapa lama batas waktu pelaporan insiden keamanan informasi dan apa sanksinya jika terlambat?",
        "knowledge_base_id": "kb_docs",
        "options": {"top_k": 5, "strict_grounding": True, "include_sources": True},
    },
)
answer = (body.get("data") or {})
document_ids = {s["document_id"] for s in answer.get("sources", [])}
record(
    "/query answers from the SOP document (real LLM)",
    status == 200 and answer.get("grounded") is True and "doc_sop_insiden" in document_ids,
    {
        "latency_ms": ms,
        "grounded": answer.get("grounded"),
        "sources": sorted(document_ids),
        "usage": answer.get("usage"),
        "answer": (answer.get("answer") or "")[:300],
    },
)

# 5. jump an answerable question from the OTHER document in the same KB
status, body, ms = call(
    "POST",
    "/query",
    KEY_A,
    {"query": "Bagaimana cara mengakses panel hosting dan fitur apa yang tersedia?", "knowledge_base_id": "kb_docs"},
)
answer = (body.get("data") or {})
record(
    "/query second document in same KB",
    status == 200 and answer.get("grounded") is True and "doc_kroombox" in {s["document_id"] for s in answer.get("sources", [])},
    {"grounded": answer.get("grounded"), "sources": sorted({s["document_id"] for s in answer.get("sources", [])})},
)

# 6. /extract
status, body, ms = call(
    "POST",
    "/extract",
    KEY_A,
    {
        "query": "batas waktu pelaporan insiden tingkat 1",
        "knowledge_base_id": "kb_docs",
        "output_schema": {
            "type": "object",
            "properties": {
                "batas_pelaporan": {"type": "string", "description": "batas waktu pelaporan insiden"},
                "kanal_resmi": {"type": "string", "description": "kanal pelaporan resmi"},
            },
        },
    },
)
extracted = body.get("data") or {}
record(
    "/extract returns schema-shaped data",
    status == 200 and bool(extracted.get("items")) and not extracted.get("not_found", False),
    {"items": extracted.get("items"), "sources": sorted({s["document_id"] for s in extracted.get("sources", [])})},
)

# 7. cross-tenant: a second organization must see nothing of org_a
status, body, ms = call(
    "POST",
    "/query",
    KEY_B,
    {"query": "berapa batas waktu pelaporan insiden keamanan?", "knowledge_base_id": "kb_docs"},
)
org_b = (body.get("data") or {})
record(
    "org_b cannot retrieve org_a documents",
    status == 200 and not org_b.get("sources") and org_b.get("grounded") is False,
    {"grounded": org_b.get("grounded"), "sources": org_b.get("sources"), "answer": (org_b.get("answer") or "")[:120]},
)

# 8. tenant fields in the body are rejected up front
status, body, ms = call(
    "POST",
    "/query",
    KEY_A,
    {"query": "halo", "knowledge_base_id": "kb_docs", "organization_id": "org_b"},
)
record("body-supplied organization_id is rejected", status == 422, {"http": status, "error": (body.get("error") or {}).get("code")})

# 9. GET /knowledge/{id} is tenant scoped too
status, body, ms = call("GET", "/knowledge/doc_sop_insiden", KEY_B)
record("org_b cannot read org_a document metadata", status == 404, {"http": status, "code": (body.get("error") or {}).get("code")})

# 9b. an undocumented option is rejected instead of silently ignored
status, body, _ = call(
    "POST",
    "/query",
    KEY_A,
    {"query": "halo", "knowledge_base_id": "kb_docs", "options": {"answer_language": "id"}},
)
record("unknown query option is rejected (fail-closed)", status == 422, {"http": status, "code": (body.get("error") or {}).get("code")})

# 10. metrics
status, body, ms = call("GET", "/metrics", KEY_A)
metrics = body.get("counters", {})
record(
    "GET /metrics reports traffic, indexing and retrieval counters",
    status == 200 and metrics.get("http_requests_total", 0) > 0 and metrics.get("documents_indexed", 0) >= 2,
    metrics,
)

# 11. delete the SOP document and confirm it is gone
status, body, ms = call("DELETE", "/knowledge/doc_sop_insiden", KEY_A)
record("DELETE document", status in (200, 202, 204), f"http={status}")
time.sleep(2)
status, body, ms = call("POST", "/search", KEY_A, {"query": "batas waktu pelaporan insiden", "knowledge_base_id": "kb_docs"})
remaining = {(h["document_id"], h["chunk_id"]) for h in (body.get("data") or {}).get("results", [])}
record(
    "deleted document no longer retrievable",
    all(document_id != "doc_sop_insiden" for document_id, _ in remaining),
    sorted(remaining),
)

print("\n=== SUMMARY ===")
for label, ok, _ in results:
    print(("PASS " if ok else "FAIL ") + label)
print(f"\n{sum(1 for _, ok, _ in results if ok)}/{len(results)} checks passed")
