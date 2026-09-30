"""Live verification, part 2: update/replace semantics, auth modes, rate limiting."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
results = []


def call(method, path, key=KEY, payload=None, timeout=120, extra_headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if key:
        request.add_header("Authorization", "Bearer " + key)
    for name, value in (extra_headers or {}).items():
        request.add_header(name, value)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, (json.loads(raw) if raw else {})


def record(label, ok, detail):
    results.append((label, ok))
    print(("PASS " if ok else "FAIL ") + label + " -> " + str(detail)[:300])


# 1. update an existing document: replace semantics must drop the old chunks
V1 = "# SOP Uji Update\n\nVersi pertama: tenggat pengajuan tujuh hari kerja sebelum tanggal mulai.\n"
V2 = "# SOP Uji Update\n\nVersi kedua: tenggat pengajuan berubah menjadi sepuluh hari kerja sebelum tanggal mulai.\nAturan tambahan: pengajuan lewat sistem HR wajib disertai lampiran surat persetujuan atasan.\n"

status, body = call(
    "POST",
    "/knowledge/index",
    payload={"document_id": "doc_update", "knowledge_base_id": "kb_upd", "document_name": "sop_update.md", "text": V1},
)
record("index v1", status == 202, status)
for _ in range(60):
    _, body = call("GET", "/knowledge/doc_update")
    if body["data"]["status"] in ("completed", "failed"):
        break
    time.sleep(0.5)
v1 = body["data"]
record("v1 completed", v1["status"] == "completed", {k: v1.get(k) for k in ("status", "chunks", "vectors_in_store")})

status, body = call(
    "PUT",
    "/knowledge/doc_update",
    payload={"document_id": "doc_update", "knowledge_base_id": "kb_upd", "document_name": "sop_update.md", "text": V2, "replace": True},
)
record("PUT update to v2", status in (200, 202), status)
for _ in range(60):
    _, body = call("GET", "/knowledge/doc_update")
    if body["data"]["status"] in ("completed", "failed"):
        break
    time.sleep(0.5)
v2 = body["data"]
record("v2 completed", v2["status"] == "completed", {k: v2.get(k) for k in ("status", "chunks", "vectors_in_store")})

_, body = call("POST", "/search", payload={"query": "tujuh hari kerja sebelum tanggal mulai", "knowledge_base_id": "kb_upd", "top_k": 5})
hits = body["data"]["results"]
content = " ".join(hit["content"] for hit in hits)
record(
    "replaced content is gone, new content is searchable",
    "sepuluh hari kerja" in content and "tujuh hari kerja" not in content,
    {"chunks": [h["chunk_id"] for h in hits], "has_old": "tujuh hari kerja" in content, "has_new": "sepuluh hari kerja" in content},
)
record("no orphan vectors after update", v2["vectors_in_store"] == v2["chunks"], {"vectors": v2["vectors_in_store"], "chunks": v2["chunks"]})

# 2. auth: unknown key and missing header
status, body = call("GET", "/knowledge/doc_update", key="key-yang-tidak-dikenal")
record("unknown API key -> 401 AUTH_INVALID", status == 401 and body["error"]["code"] == "AUTH_INVALID", {"http": status, "code": body.get("error", {}).get("code")})
status, body = call("GET", "/knowledge/doc_update", key=None)
record("missing credential -> 401", status == 401, {"http": status, "code": body.get("error", {}).get("code")})

# 3. rate limiting answers with the PRD code, not a generic 429 body.
#    The limit must be low enough to be reachable from this script: run the service with
#    RATE_LIMIT_PER_MINUTE=5 (see docs/verification.md) and point BASE at that instance.
limited = None
for _ in range(200):
    status, body = call("GET", "/knowledge/nonexistent_doc")
    if status == 429:
        limited = body
        break
record(
    "rate limiter returns RATE_LIMITED",
    limited is not None and limited.get("error", {}).get("code") == "RATE_LIMITED",
    limited or "not reached: start an instance with a low RATE_LIMIT_PER_MINUTE to exercise this",
)

print("\n=== SUMMARY ===")
for label, ok in results:
    print(("PASS " if ok else "FAIL ") + label)
print(f"\n{sum(1 for _, ok in results if ok)}/{len(results)} checks passed")
