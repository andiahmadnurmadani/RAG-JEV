#!/usr/bin/env python3
"""Pemeriksaan pasca-deploy RAG Service (lihat docs/deployment.md §7).

Default: **read-only** — hanya memanggil endpoint baca + membuktikan batas autentikasi.
Dengan ``--with-write``: menambah satu dokumen uji ke knowledge base, menunggu selesai,
mengajukan satu pertanyaan, lalu **menghapusnya kembali** (self-cleaning).

Pemakaian:
    BASE_URL=http://localhost:8000 API_KEY=kunci-org-a \\
        python scripts/deploy_smoke.py

    BASE_URL=http://localhost:8000 API_KEY=kunci-org-a API_KEY_READONLY=kunci-org-b \\
        python scripts/deploy_smoke.py --with-write

Keluar dengan kode != 0 bila ada pemeriksaan yang gagal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = (os.getenv("BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
READONLY_KEY = os.getenv("API_KEY_READONLY", "")
KB = os.getenv("SMOKE_KB", "deploy_smoke")
PREFIX = os.getenv("API_PREFIX", "/api/v1")

results: list[tuple[bool, str]] = []


def call(path: str, key: str | None = None, method: str = "GET", body: dict | None = None,
         timeout: float = 60.0) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except Exception as exc:  # koneksi ditolak, DNS, timeout
        return 0, f"{type(exc).__name__}: {exc}"


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((condition, name))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def error_code(payload: object) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("code", ""))
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test pasca-deploy RAG Service")
    parser.add_argument("--with-write", action="store_true",
                        help="uji tulis (index + query + delete) pada knowledge base uji")
    parser.add_argument("--timeout", type=float, default=60.0, help="timeout per permintaan (detik)")
    args = parser.parse_args()

    print(f"target   : {BASE}{PREFIX}")
    print(f"kunci    : baca={'ada' if API_KEY else 'TIDAK ADA'} "
          f"readonly={'ada' if READONLY_KEY else '-'}\n")

    # ---- 1. liveness / readiness -------------------------------------------
    status, payload = call(f"{PREFIX}/health", timeout=args.timeout)
    check("GET {prefix}/health -> 200 status ok".replace("{prefix}", PREFIX),
          status == 200 and isinstance(payload, dict) and payload.get("status") in {"ok", "degraded"},
          f"HTTP {status} {str(payload)[:120]}")

    status, payload = call(f"{PREFIX}/ready", timeout=args.timeout)
    deps = ((payload or {}).get("data") or {}).get("dependencies", {}) if isinstance(payload, dict) else {}
    check("GET {prefix}/ready -> 200, dependensi terbaca".replace("{prefix}", PREFIX),
          status == 200 and bool(deps), f"HTTP {status}")
    for name, state in (deps or {}).items():
        print(f"         - {name:12} {state}")

    status, payload = call("/ui/", timeout=args.timeout)
    check("GET /ui/ -> 200 (konsol statis)",
          status == 200 and isinstance(payload, str) and "<html" in payload.lower(), f"HTTP {status}")

    # ---- 2. batas autentikasi ----------------------------------------------
    status, payload = call(f"{PREFIX}/knowledge", timeout=args.timeout)
    check("tanpa kunci -> 401 AUTH_INVALID", status == 401 and error_code(payload) == "AUTH_INVALID",
          f"HTTP {status} {error_code(payload)}")

    status, payload = call(f"{PREFIX}/knowledge", key="kunci-salah", timeout=args.timeout)
    check("kunci salah -> 401", status == 401, f"HTTP {status} {error_code(payload)}")

    if READONLY_KEY:
        status, payload = call(f"{PREFIX}/settings", key=READONLY_KEY, method="PUT", body={}, timeout=args.timeout)
        check("kunci non-admin -> 403 AUTH_FORBIDDEN pada PUT /settings",
              status == 403 and error_code(payload) == "AUTH_FORBIDDEN", f"HTTP {status} {error_code(payload)}")

    if not API_KEY:
        print("\n(tanpa API_KEY: pemeriksaan ber-kunci dilewati)")
        return report()

    # ---- 3. endpoint ber-kunci ---------------------------------------------
    status, payload = call(f"{PREFIX}/knowledge", key=API_KEY, timeout=args.timeout)
    ok_list = status == 200 and isinstance(payload, dict) and payload.get("success") is True
    docs = ((payload or {}).get("data") or {}).get("documents") or [] if isinstance(payload, dict) else []
    count = ((payload or {}).get("data") or {}).get("count") if isinstance(payload, dict) else None
    check("GET /knowledge -> 200 (terautentikasi)", ok_list, f"HTTP {status} count={count}")

    status, payload = call(f"{PREFIX}/tables", key=API_KEY, timeout=args.timeout)
    check("GET /tables -> 200", status == 200, f"HTTP {status}")

    status, payload = call(f"{PREFIX}/metrics", key=API_KEY, timeout=args.timeout)
    check("GET /metrics -> 200", status == 200, f"HTTP {status}")

    # /search dan /query selalu ter-scope ke satu knowledge base.
    kbs = {d.get("knowledge_base_id") for d in docs if isinstance(d, dict) and d.get("knowledge_base_id")}
    if KB in kbs:
        search_kb = KB
    else:
        search_kb = sorted(kbs)[0] if kbs else KB
    if kbs:
        status, payload = call(f"{PREFIX}/search", key=API_KEY, method="POST",
                               body={"query": "uji koneksi deploy", "knowledge_base_id": search_kb, "top_k": 3},
                               timeout=args.timeout)
        check(f"POST /search -> 200 (kb={search_kb})", status == 200, f"HTTP {status} {str(payload)[:120]}")
    else:
        check("POST /search dilewati (belum ada knowledge base)", True, "tidak ada KB untuk diuji")

    # ---- 4. opsional: jalur tulis ------------------------------------------
    if args.with_write:
        doc_id = f"deploy-smoke-{int(time.time())}"
        marker = f"SMOKE-{int(time.time())}"
        status, payload = call(f"{PREFIX}/knowledge/index", key=API_KEY, method="POST", body={
            "document_id": doc_id,
            "knowledge_base_id": KB,
            "document_name": "deploy smoke",
            "text": f"Catatan uji deploy. Penanda unik {marker}. Warna favorit tim ops: biru laut.",
            "metadata": {"sumber": "deploy_smoke"},
        }, timeout=args.timeout)
        check("POST /knowledge/index -> 202", status in (200, 202), f"HTTP {status} {str(payload)[:120]}")

        state = ""
        deadline = time.time() + 120
        while time.time() < deadline:
            status, payload = call(f"{PREFIX}/knowledge/{doc_id}", key=API_KEY, timeout=args.timeout)
            state = str(((payload or {}).get("data") or {}).get("status", "")) if isinstance(payload, dict) else ""
            if state in {"completed", "indexed", "failed"}:
                break
            time.sleep(2)
        check("pengindeksan selesai (completed)", state == "completed", f"status={state or 'timeout'}")

        status, payload = call(f"{PREFIX}/query", key=API_KEY, method="POST",
                               body={"query": "warna favorit tim ops?", "knowledge_base_id": KB},
                               timeout=args.timeout)
        check("POST /query -> 200", status == 200, f"HTTP {status}")
        if isinstance(payload, dict):
            answer = str(((payload.get("data") or {}).get("answer") or ""))[:160]
            print(f"         jawaban: {answer or '(kosong)'}")

        status, payload = call(f"{PREFIX}/knowledge/{doc_id}", key=API_KEY, method="DELETE", timeout=args.timeout)
        check("DELETE /knowledge/{id} -> 200", status == 200, f"HTTP {status}")
        status, payload = call(f"{PREFIX}/knowledge/{doc_id}", key=API_KEY, timeout=args.timeout)
        gone = status == 404 or "deleted" in json.dumps(payload).lower()
        check("dokumen uji benar-benar terhapus", gone, f"HTTP {status}")

    return report()


def report() -> int:
    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} pemeriksaan lulus")
    if failed:
        print("gagal: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
