"""Uji hidup di produksi: ambil knowledge dari web publik, lalu tanya. Tidak mencetak rahasia."""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
KB = "kb_web_prod"
TARGET = "https://www.iana.org/help/example-domains"


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
    web = ((ready.get("data") or {}).get("detail") or {}).get("web") or {}
    print("kebijakan web di produksi:", json.dumps(web))

    print("\n== 1. tolak alamat internal (SSRF) ==")
    for label, payload in (
        ("metadata cloud", {"web_url": "http://169.254.169.254/latest/meta-data/"}),
        ("localhost", {"web_url": "http://127.0.0.1:8000/api/v1/health"}),
        ("file://", {"file_url": "file:///etc/passwd"}),
    ):
        result = call("POST", "/knowledge/index", {"document_id": "ssrf_prod", "knowledge_base_id": KB, **payload}, key=key)
        error = result.get("error") or {}
        print(f"  {label:16s} -> HTTP {result.get('_error')} {error.get('code')} | {(error.get('message') or '')[:70]}")

    print("\n== 2. ambil halaman web publik ==")
    created = call(
        "POST",
        "/knowledge/index",
        {
            "document_id": "web_iana_prod",
            "knowledge_base_id": KB,
            "web_url": TARGET,
            "web_max_pages": 3,
            "web_max_depth": 1,
        },
        key=key,
    )
    print("  permintaan:", json.dumps(created)[:160])
    status = {}
    for _ in range(90):
        status = (call("GET", "/knowledge/web_iana_prod", key=key).get("data") or {})
        if status.get("status") in {"completed", "failed"}:
            break
        time.sleep(2)
    print(
        "  hasil: status={} halaman={} chunk={} token={} sumber={}".format(
            status.get("status"), status.get("pages"), status.get("chunks"), status.get("tokens"), status.get("source_url")
        )
    )
    if status.get("status") != "completed":
        print("  GAGAL:", status.get("error"))
        return 1

    print("\n== 3. tanya isi halaman ==")
    data = (call(
        "POST",
        "/query",
        {"query": "Domain contoh apa saja yang disediakan untuk dokumentasi?", "knowledge_base_id": KB, "options": {"top_k": 6}},
        key=key,
    ).get("data") or {})
    usage = data.get("usage") or {}
    print("  angka : konteks={} token={} keluar={} finish={!r} alasan={!r}".format(
        usage.get("context_chunks"), usage.get("context_tokens"), usage.get("output_tokens"),
        usage.get("finish_reason"), data.get("no_answer_reason")))
    print("  sumber:", [item.get("source_url") for item in (data.get("sources") or [])][:3])
    print("  jawab :", (data.get("answer") or "").strip().replace("\n", " ")[:300])

    print("\n== 4. bersihkan dokumen uji ==")
    print("  ", json.dumps(call("DELETE", "/knowledge/web_iana_prod", key=key))[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
