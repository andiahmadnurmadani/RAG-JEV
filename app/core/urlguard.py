"""Pengaman URL: hanya alamat publik yang boleh diambil layanan ini.

Kenapa ini perlu ada sebelum fitur "dari web" dan "berkas publik":

``file_url`` dan crawl web membuat server ini mengeluarkan permintaan HTTP atas nama pemanggil.
Tanpa pengaman, satu baris payload bisa menyuruh server membaca
``http://169.254.169.254/latest/meta-data/`` (kredensial cloud), ``http://127.0.0.1:8000/``
(API internal ini sendiri), atau port lain di jaringan dalam - lalu isinya ikut jadi knowledge
dan bisa dibaca lewat pencarian. Itu SSRF, dan akibatnya bukan sekadar error.

Aturan di sini:

* skema hanya ``http``/``https``;
* tidak boleh ada kredensial di dalam URL (``http://user:pass@host/``);
* nama host dilarang: localhost, *.local, *.internal, dan nama metadata cloud;
* **semua** alamat hasil resolusi DNS harus publik (menutup DNS rebinding: nama yang
  sesekali menunjuk ke 127.0.0.1 tetap ditolak);
* diperiksa lagi setelah setiap pengalihan (redirect) - karena itu jalur klasik untuk lolos.

Pemeriksaan IP memakai ``ipaddress``, jadi IPv6, IPv4-mapped IPv6, link-local, dan rentang
khusus ikut tertutup. ``allow_private`` hanya untuk pemasangan yang memang internal
(uji lokal / intranet), dan bawaannya mati.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import List, Optional
from urllib.parse import urlparse

# Nama-nama yang tidak pernah menunjuk ke internet publik.
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")

# Rentang yang selalu ditolak meski bukan "privat" menurut ipaddress.
EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmark
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:db8::/32"),  # dokumentasi
)


class UrlRejected(ValueError):
    """URL ditolak karena bukan alamat publik yang boleh diambil."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


def _is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # IPv4-mapped (::ffff:127.0.0.1) harus dinilai sebagai IPv4-nya, bukan sebagai IPv6 publik.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    for network in EXTRA_BLOCKED_NETWORKS:
        if ip.version == network.version and ip in network:
            return False
    return True


def _is_metadata_ip(raw: str) -> bool:
    """Alamat metadata cloud / link-local: tidak pernah boleh diambil, bahkan di intranet."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_link_local) or str(ip) == "169.254.169.254"


def resolve_host(hostname: str, *, port: Optional[int] = None) -> List[str]:
    """Semua alamat yang ditunjuk nama host (IPv4 + IPv6)."""
    try:
        infos = socket.getaddrinfo(hostname, port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlRejected(f"nama host tidak bisa diterjemahkan: {hostname}") from exc
    addresses: List[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def assert_public_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
    """Kembalikan URL bila aman diambil; sebaliknya lempar :class:`UrlRejected`.

    ``resolve=False`` melewatkan pemeriksaan DNS (dipakai saat menguji bentuk URL saja).
    """
    raw = (url or "").strip()
    if not raw:
        raise UrlRejected("URL kosong")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise UrlRejected(f"skema '{parsed.scheme or '(kosong)'}' tidak didukung; pakai http atau https")
    if not parsed.hostname:
        raise UrlRejected("URL tidak menyebut nama host")
    if parsed.username or parsed.password:
        raise UrlRejected("URL tidak boleh memuat kredensial (user:pass@)")
    host = parsed.hostname.lower()
    # Alamat metadata cloud dan link-local TIDAK PERNAH sah, bahkan di pemasangan yang
    # mengizinkan alamat privat: itu bukan intranet, itu kredensial mesin.
    if host in BLOCKED_HOSTNAMES or any(host.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        raise UrlRejected(f"host '{host}' bukan alamat publik")
    if allow_private:
        return raw
    # Host yang ditulis sebagai IP langsung diperiksa tanpa DNS.
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        # Link-local/metadata (169.254.x) selalu ditolak; alamat privat lain hanya bila
        # pemasangannya memang mengizinkan alamat privat.
        if not _is_public_ip(host.strip("[]")):
            if allow_private and not _is_metadata_ip(host.strip("[]")):
                return raw
            raise UrlRejected(f"alamat IP '{host}' bukan alamat publik")
        return raw
    if not resolve:
        return raw
    addresses = resolve_host(host, port=parsed.port)
    if not addresses:
        raise UrlRejected(f"nama host '{host}' tidak menunjuk ke alamat mana pun")
    for address in addresses:
        if _is_metadata_ip(address):
            raise UrlRejected(
                f"nama host '{host}' menunjuk ke alamat metadata/link-local ({address}) - permintaan dihentikan"
            )
        if not _is_public_ip(address) and not allow_private:
            raise UrlRejected(
                f"nama host '{host}' menunjuk ke alamat non-publik ({address}) - permintaan dihentikan"
            )
    return raw


def same_host(url: str, other: str) -> bool:
    left, right = urlparse(url), urlparse(other)
    return bool(left.hostname) and left.hostname.lower() == (right.hostname or "").lower()


def normalize_for_visit(url: str) -> str:
    """Bentuk kanonik untuk dedupe: tanpa fragmen, tanpa garis miring di ujung."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    rebuilt = parsed._replace(path=path, fragment="", query=parsed.query)
    text = rebuilt.geturl()
    return text.rstrip("/") if path == "/" else text