"""Knowledge dari web: satu halaman, satu berkas publik, dan crawl situs kecil.

Uji ini memakai server HTTP lokal sebagai "internet" supaya hasilnya bisa diulang dan tidak
bergantung pada situs orang lain. Yang dikunci di sini:

* halaman web jadi dokumen yang bisa dicari, dan **setiap potongan menyimpan URL halamannya**
  (bukan URL akar) supaya sitasi menunjuk halaman yang benar;
* crawl mengikuti tautan pada host yang sama sampai batas halaman/kedalaman, tidak menyeberang
  host, dan menghormati robots.txt;
* URL ke alamat privat/internal DITOLAK sebelum pekerjaan dibuat (SSRF);
* berkas publik (``file_url``) juga melewati pengaman yang sama.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from typing import Dict

import pytest

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_web"

SITE: Dict[str, tuple] = {
    "/": (
        "text/html",
        "<html><head><title>Beranda Uji</title></head><body>"
        "<h1>Beranda</h1><p>Halaman utama berisi kata kunci kunciemas.</p>"
        '<a href="/panduan">Panduan</a> <a href="/lain">Lain</a> '
        '<a href="https://example.com/luar">Luar</a> <a href="mailto:x@y.z">Surel</a></body></html>',
    ),
    "/panduan": (
        "text/html",
        "<html><head><title>Panduan Uji</title></head><body>"
        "<h1>Panduan</h1><p>Bagian ini memuat prosedur khusus bernama prosedurkhusus.</p>"
        '<a href="/dalam">Dalam</a></body></html>',
    ),
    "/dalam": (
        "text/html",
        "<html><head><title>Dalam Uji</title></head><body><p>Isi terdalam kata dalamsekali.</p></body></html>",
    ),
    "/lain": (
        "text/html",
        "<html><head><title>Lain Uji</title></head><body><p>Halaman lain kata lainkata.</p></body></html>",
    ),
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /rahasia\n"),
    "/rahasia": ("text/html", "<html><body><p>Jangan diambil kata rahasiakata.</p></body></html>"),
    "/berkas.txt": ("text/plain", "Berkas publik berisi kata berkaspublik."),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - nama wajib dari BaseHTTPRequestHandler
        body = SITE.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, text = body
        payload = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # senyap saat uji
        return


@pytest.fixture(scope="module")
def site_url():
    """Server HTTP lokal sebagai pengganti situs publik."""
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{port}"
        server.shutdown()


@pytest.fixture
def public_settings(settings, site_url):
    """Setelan uji yang mengizinkan alamat lokal (server uji ini) tapi tetap menguji pengaman."""
    settings.allow_private_urls = True
    settings.web_crawl_max_pages = 10
    settings.web_crawl_max_depth = 2
    settings.web_crawl_respect_robots = True
    settings.web_crawl_follow_files = True
    return settings


def _index_web(client, url: str, *, document_id: str = "doc_web", **extra) -> dict:
    payload = {
        "document_id": document_id,
        "knowledge_base_id": KB,
        "document_name": "",
        "web_url": url,
        **extra,
    }
    response = client.post("/api/v1/knowledge/index", json=payload, headers=auth(TENANT_A_KEY))
    assert response.status_code == 202, response.text
    return wait_for_job(client, document_id, timeout=90.0)


def test_a_single_web_page_becomes_searchable_knowledge(client, public_settings, site_url):
    status = _index_web(client, site_url + "/panduan", document_id="doc_web_satu", web_max_pages=1, web_max_depth=0)
    assert status["status"] == "completed", status

    found = client.post(
        "/api/v1/search",
        json={"query": "prosedurkhusus", "knowledge_base_id": KB, "top_k": 5},
        headers=auth(TENANT_A_KEY),
    )
    assert found.status_code == 200, found.text
    hits = found.json()["data"]["results"]
    assert hits, "halaman web harus bisa ditemukan lewat pencarian"
    top = hits[0]
    assert "prosedurkhusus" in top["content"]
    # Yang penting: sitasinya menunjuk halaman itu, bukan alamat akar.
    assert top["source_url"].endswith("/panduan"), top["source_url"]


def test_crawl_follows_same_host_links_and_records_each_page_url(client, public_settings, site_url):
    status = _index_web(client, site_url + "/", document_id="doc_web_crawl", web_max_pages=10, web_max_depth=2)
    assert status["status"] == "completed", status
    # Beranda + /panduan + /lain + /dalam = 4 halaman (satu host, kedalaman 2).
    assert status["pages"] == 4, status

    for needle, expected_suffix in (
        ("kunciemas", "/"),
        ("prosedurkhusus", "/panduan"),
        ("dalamsekali", "/dalam"),
        ("lainkata", "/lain"),
    ):
        found = client.post(
            "/api/v1/search",
            json={"query": needle, "knowledge_base_id": KB, "top_k": 8},
            headers=auth(TENANT_A_KEY),
        )
        hits = found.json()["data"]["results"]
        assert hits, f"'{needle}' tidak ditemukan"
        urls = [hit["source_url"] or "" for hit in hits]
        if expected_suffix == "/":
            # Beranda: alamat akar boleh ditulis dengan atau tanpa garis miring di ujung.
            matched = any(url.rstrip("/") == site_url.rstrip("/") for url in urls)
        else:
            matched = any(url.endswith(expected_suffix) for url in urls)
        assert matched, f"'{needle}' harus menunjuk {expected_suffix}: {urls}"


def test_crawl_does_not_cross_hosts_or_take_robots_forbidden_pages(client, public_settings, site_url):
    _index_web(client, site_url + "/", document_id="doc_web_batas", web_max_pages=20, web_max_depth=2)
    for needle in ("rahasia",):
        found = client.post(
            "/api/v1/search",
            json={"query": needle + "kata", "knowledge_base_id": KB, "top_k": 8},
            headers=auth(TENANT_A_KEY),
        )
        hits = found.json()["data"]["results"]
        assert not any("/rahasia" in (hit["source_url"] or "") for hit in hits), "halaman terlarang robots.txt ikut terambil"


def test_a_public_text_file_can_be_indexed_by_url(client, public_settings, site_url):
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_berkas_publik",
            "knowledge_base_id": KB,
            "file_url": site_url + "/berkas.txt",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202, response.text
    status = wait_for_job(client, "doc_berkas_publik", timeout=60.0)
    assert status["status"] == "completed", status
    assert status["source_url"].endswith("/berkas.txt")

    found = client.post(
        "/api/v1/search",
        json={"query": "berkaspublik", "knowledge_base_id": KB, "top_k": 5},
        headers=auth(TENANT_A_KEY),
    )
    hits = found.json()["data"]["results"]
    assert any("berkaspublik" in hit["content"] for hit in hits)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/api/v1/health",
        "file:///etc/passwd",
        "http://user:pass@example.com/x",
        "ftp://example.com/x",
    ],
)
def test_private_and_dangerous_urls_are_refused_before_a_job_exists(client, settings, url):
    """Pengaman SSRF: URL non-publik ditolak 422, dan tidak ada pekerjaan yang dibuat."""
    settings.allow_private_urls = False
    for field in ("web_url", "file_url"):
        response = client.post(
            "/api/v1/knowledge/index",
            json={"document_id": "doc_ssrf", "knowledge_base_id": KB, field: url},
            headers=auth(TENANT_A_KEY),
        )
        assert response.status_code == 422, (field, url, response.text)
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    listing = client.get(f"/api/v1/knowledge?knowledge_base_id={KB}", headers=auth(TENANT_A_KEY))
    assert not any(row["document_id"] == "doc_ssrf" for row in listing.json()["data"]["documents"])


def test_a_page_without_readable_text_fails_honestly(client, public_settings, site_url):
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_web_kosong",
            "knowledge_base_id": KB,
            "web_url": site_url + "/tidak-ada",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    status = wait_for_job(client, "doc_web_kosong", timeout=60.0)
    assert status["status"] == "failed"
    assert "tidak ada teks" in (status["error"] or "") or "HTTP 404" in (status["error"] or ""), status["error"]


def test_web_crawl_can_be_switched_off(client, settings, site_url):
    settings.allow_private_urls = True
    settings.web_crawl_enabled = False
    try:
        response = client.post(
            "/api/v1/knowledge/index",
            json={"document_id": "doc_web_mati", "knowledge_base_id": KB, "web_url": site_url + "/"},
            headers=auth(TENANT_A_KEY),
        )
        assert response.status_code == 422
        assert "WEB_CRAWL_ENABLED" in response.json()["error"]["message"]
    finally:
        settings.web_crawl_enabled = True


def test_mixing_two_sources_in_one_request_is_refused(client, public_settings, site_url):
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_dua_sumber",
            "knowledge_base_id": KB,
            "web_url": site_url + "/",
            "text": "sekaligus teks",
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
