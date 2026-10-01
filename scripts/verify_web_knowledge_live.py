"""Uji hidup: knowledge dari web, dari satu halaman sampai crawl, lewat layanan yang berjalan.

Dijalankan terhadap layanan lokal (scripts/run_live.sh). Situs uji disajikan server HTTP lokal
supaya tidak bergantung pada situs orang lain, dan setiap halaman memuat kata kunci unik.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
import urllib.error
import urllib.request
from typing import Dict

BASE = "http://127.0.0.1:8099/api/v1"
KEY = "live-key-org-a"
KB = "kb_web_live"

SITE: Dict[str, tuple] = {
    "/": (
        "text/html",
        "<html><head><title>Portal SOP</title></head><body>"
        "<h1>Portal SOP</h1><p>Halaman depan memuat kata sandi khusus portalrahasia.</p>"
        '<a href="/sop/cuti">SOP Cuti</a> <a href="/sop/lembur">SOP Lembur</a> '
        '<a href="https://example.com/luar">Situs luar</a></body></html>',
    ),
    "/sop/cuti": (
        "text/html",
        "<html><head><title>SOP Cuti</title></head><body>"
        "<h2>Pengajuan cuti</h2><p>Karyawan mengajukan cuti minimal tiga hari kerja cutitigahari.</p>"
        "<table><tr><td>Jenis</td><td>Kuota</td></tr><tr><td>Tahunan</td><td>12</td></tr></table>"
        '<a href="/sop/cuti/lampiran">Lampiran</a></body></html>',
    ),
    "/sop/cuti/lampiran": (
        "text/html",
        "<html><head><title>Lampiran Cuti</title></head><body>"
        "<p>Formulir lampiran memakai kode lampirancuti.</p></body></html>",
    ),
    "/sop/lembur": (
        "text/html",
        "<html><head><title>SOP Lembur</title></head><body>"
        "<p>Lembur dihitung per jam dan butuh persetujuan atasan lemburperjam.</p></body></html>",
    ),
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /internal\n"),
    "/internal/rahasia": (
        "text/html",
        "<html><body><p>Halaman internal kata internalsensitif.</p></body></html>",
    ),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        item = SITE.get(self.path)
        if item is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, text = item
        payload = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        return


def call(method: str, path: str, payload=None, timeout: float = 300.0, retries: int = 3) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    last: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(BASE + path, data=body, method=method)
        request.add_header("Authorization", f"Bearer {KEY}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            # 502/503/504 biasanya dari gateway LLM, bukan dari layanan ini: ulangi.
            if exc.code in (502, 503, 504) and attempt + 1 < retries:
                print(f"    (ulang {attempt + 1}/{retries - 1}: HTTP {exc.code})")
                time.sleep(4 * (attempt + 1))
                last = RuntimeError(f"HTTP {exc.code}: {detail}")
                continue
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc
    raise last or RuntimeError("permintaan gagal")


def wait_document(document_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = (call("GET", f"/knowledge/{document_id}") or {}).get("data") or {}
        if last.get("status") in {"completed", "failed"}:
            return last
        time.sleep(2)
    return last


def main() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        site = f"http://127.0.0.1:{port}"
        print("situs uji :", site)
        print("layanan   :", BASE)

        print("\n== 1. satu halaman web ==")
        call(
            "POST",
            "/knowledge/index",
            {
                "document_id": "web_halaman_tunggal",
                "knowledge_base_id": KB,
                "web_url": site + "/sop/lembur",
                "web_max_pages": 1,
                "web_max_depth": 0,
            },
        )
        status = wait_document("web_halaman_tunggal")
        print("  status:", status.get("status"), "| halaman:", status.get("pages"), "| sumber:", status.get("source_url"))
        print("  chunk:", status.get("chunks"), "| token:", status.get("tokens"))
        if status.get("status") != "completed":
            print("  GAGAL:", status.get("error"))
            return 1

        print("\n== 2. crawl situs (kedalaman 2) ==")
        call(
            "POST",
            "/knowledge/index",
            {
                "document_id": "web_situs",
                "knowledge_base_id": KB,
                "web_url": site + "/",
                "web_max_pages": 10,
                "web_max_depth": 2,
            },
        )
        status = wait_document("web_situs")
        print("  status:", status.get("status"), "| halaman:", status.get("pages"), "| chunk:", status.get("chunks"))
        if status.get("status") != "completed":
            print("  GAGAL:", status.get("error"))
            return 1

        print("\n== 3. tanya lewat /query, periksa halaman yang dirujuk ==")
        failed = False
        for question, needle, expected in (
            ("Apa aturan pengajuan cuti?", "cutitigahari", "/sop/cuti"),
            ("Berapa kuota cuti tahunan?", "12", "/sop/cuti"),
            ("Bagaimana aturan lembur?", "lemburperjam", "/sop/lembur"),
            ("Apa kode pada formulir lampiran?", "lampirancuti", "/sop/cuti/lampiran"),
        ):
            started = time.time()
            data = (call("POST", "/query", {"query": question, "knowledge_base_id": KB, "options": {"top_k": 8}}) or {}).get("data") or {}
            answer = (data.get("answer") or "")
            sources = data.get("sources") or []
            urls = [item.get("source_url") or "" for item in sources]
            usage = data.get("usage") or {}
            print("\n  TANYA :", question)
            print(
                "  angka : konteks={} token={} keluar={} finish={!r} ({:.0f}s)".format(
                    usage.get("context_chunks"), usage.get("context_tokens"),
                    usage.get("output_tokens"), usage.get("finish_reason"), time.time() - started,
                )
            )
            print("  sumber:", urls[:4])
            print("  jawab :", answer.strip().replace("\n", " ")[:220])
            if needle.lower() not in answer.lower():
                print(f"  CATATAN: jawaban tidak memuat '{needle}'")
            if expected not in " ".join(urls):
                print(f"  CATATAN: tidak ada sumber yang menunjuk {expected}")
                failed = True

        print("\n== 4. halaman terlarang robots.txt tidak terindeks ==")
        found = (call("POST", "/search", {"query": "internalsensitif", "knowledge_base_id": KB, "top_k": 8}) or {}).get("data") or {}
        hits = found.get("results") or []
        leaked = [hit for hit in hits if "/internal/" in (hit.get("source_url") or "")]
        print("  hasil:", len(hits), "| dari /internal:", len(leaked))
        if leaked:
            print("  GAGAL: halaman terlarang ikut terindeks")
            failed = True

        print("\n== 5. daftar dokumen ==")
        listing = (call("GET", f"/knowledge?knowledge_base_id={KB}") or {}).get("data") or {}
        for row in listing.get("documents") or []:
            print(
                "  - {} | {} | {} chunk | {} | {}".format(
                    row.get("document_name"), row.get("status"), row.get("chunks"),
                    row.get("source_url") or "-", row.get("document_id"),
                )
            )

        server.shutdown()

    print("\nHASIL :", "gagal" if failed else "berhasil - knowledge dari web bisa diambil, dirujuk, dan dijawab")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
