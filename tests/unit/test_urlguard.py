"""Pengaman URL: hanya alamat publik yang boleh diambil layanan ini.

Fitur "knowledge dari web" dan "berkas publik" membuat server mengeluarkan permintaan HTTP atas
nama pemanggil. Uji ini mengunci batasnya: alamat internal, metadata cloud, dan skema aneh
ditolak; alamat publik lolos. Kalau uji ini gagal, itu tanda layanan bisa dipakai membaca
jaringan dalam (SSRF).
"""

from __future__ import annotations

import pytest

from app.core.urlguard import UrlRejected, assert_public_url, normalize_for_visit, same_host


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/health",
        "http://127.1.2.3/",
        "http://[::1]/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
        "http://172.16.5.4/",
        "http://0.0.0.0/",
        "http://100.64.1.1/",  # CGNAT
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://localhost:8000/",
        "http://something.local/",
        "http://server.internal/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_internal_and_metadata_addresses_are_refused(url):
    with pytest.raises(UrlRejected):
        assert_public_url(url, resolve=False)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,halo",
        "//example.com/x",
        "",
    ],
)
def test_non_http_schemes_are_refused(url):
    with pytest.raises(UrlRejected):
        assert_public_url(url, resolve=False)


def test_credentials_inside_the_url_are_refused():
    with pytest.raises(UrlRejected):
        assert_public_url("http://user:secret@example.com/", resolve=False)


def test_public_addresses_pass():
    for url in ("https://example.com/a?b=1", "http://example.com", "https://sub.example.co.id/x/y"):
        assert assert_public_url(url, resolve=False) == url


def test_metadata_hosts_stay_refused_even_when_private_urls_are_allowed():
    """Pemasangan intranet boleh mengambil alamat privat, TAPI tidak alamat metadata mesin."""
    for url in ("http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/"):
        with pytest.raises(UrlRejected):
            assert_public_url(url, allow_private=True, resolve=False)
    # Alamat privat biasa tetap boleh saat pemasangannya memang mengizinkan.
    assert assert_public_url("http://10.0.0.5/x", allow_private=True, resolve=False) == "http://10.0.0.5/x"


def test_a_hostname_resolving_to_a_private_address_is_refused(monkeypatch):
    """DNS rebinding: nama publik yang menunjuk 127.0.0.1 tetap ditolak."""
    monkeypatch.setattr("app.core.urlguard.resolve_host", lambda host, port=None: ["127.0.0.1"])
    with pytest.raises(UrlRejected):
        assert_public_url("http://situs-palsu.example/x")
    monkeypatch.setattr("app.core.urlguard.resolve_host", lambda host, port=None: ["93.184.216.34"])
    assert assert_public_url("http://situs-nyata.example/x")


def test_a_hostname_mixing_public_and_private_addresses_is_refused(monkeypatch):
    monkeypatch.setattr(
        "app.core.urlguard.resolve_host", lambda host, port=None: ["93.184.216.34", "10.1.2.3"]
    )
    with pytest.raises(UrlRejected):
        assert_public_url("http://campur.example/x")


def test_normalize_for_visit_ignores_fragments_and_trailing_slashes():
    assert normalize_for_visit("https://contoh.test/a/") == normalize_for_visit("https://contoh.test/a")
    assert normalize_for_visit("https://contoh.test/a#bagian") == normalize_for_visit("https://contoh.test/a")
    assert normalize_for_visit("https://contoh.test/a?x=1") != normalize_for_visit("https://contoh.test/a?x=2")


def test_same_host_compares_hostnames_only():
    assert same_host("https://contoh.test/a", "http://contoh.test/b")
    assert not same_host("https://contoh.test/a", "https://lain.test/b")
