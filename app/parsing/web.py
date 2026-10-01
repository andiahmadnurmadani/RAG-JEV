"""Sumber knowledge dari web: satu halaman, atau situs kecil sampai kedalaman tertentu.

Dua bentuk yang didukung:

1. **Satu halaman/satu berkas publik** - ``fetch_url()`` mengambil satu URL (HTML, PDF, DOCX,
   ...) dan mengembalikan isinya. Ini yang dipakai ``file_url`` pada ``/knowledge/index``.
2. **Crawl** - ``crawl()`` mulai dari satu URL, mengikuti tautan di dalamnya sampai batas
   kedalaman dan jumlah halaman, lalu mengembalikan daftar halaman siap diindeks.

Yang dijaga di sini (semuanya pernah jadi celah nyata pada layanan pengambil-URL):

* **SSRF** - setiap alamat diperiksa :mod:`app.core.urlguard` sebelum diminta, dan diperiksa
  ULANG di setiap pengalihan. Pengalihan adalah jalur klasik untuk lolos dari pemeriksaan awal.
* **Batas** - jumlah halaman, kedalaman, ukuran per halaman, dan waktu. Crawl tanpa batas
  adalah cara termudah menjadikan server ini alat pengambil-alih situs orang lain.
* **robots.txt** - dihormati secara bawaan. Situs yang melarang tidak dijelajahi, dan
  penolakannya dilaporkan sebagai catatan, bukan kegagalan diam-diam.
* **Host** - bawaannya hanya tautan pada host yang sama yang diikuti, supaya satu URL tidak
  menyeret seluruh internet masuk ke knowledge.
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.logging import get_logger
from app.core.urlguard import UrlRejected, assert_public_url, normalize_for_visit
from app.parsing.parser import _html_to_text, parse_document  # noqa: PLC2701 - satu paket

logger = get_logger(__name__)

HTML_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json", "text/tab-separated-values")
# Tautan yang tidak pernah berguna sebagai knowledge.
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:", "whatsapp:", "blob:")
SKIP_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".mp4", ".mp3", ".avi", ".mov", ".webm", ".wav",
)
# Alamat yang jelas bukan isi: akun, keranjang, pencarian, logout.
SKIP_PATH_PATTERNS = (
    r"/login", r"/logout", r"/signin", r"/signup", r"/register", r"/cart", r"/checkout",
    r"/wp-admin", r"/wp-login", r"/feed/?$", r"\?s=", r"/search", r"/tag/", r"/author/",
)

_HREF_RE = re.compile(r"""(?is)<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""")
_CANONICAL_RE = re.compile(r"""(?is)<link\b[^>]*?\brel\s*=\s*["']?canonical["']?[^>]*?\bhref\s*=\s*["']([^"']+)["']""")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")


@dataclass
class FetchedPage:
    """Hasil satu permintaan HTTP yang sudah lolos pengaman."""

    url: str
    final_url: str
    status: int
    content_type: str
    content: bytes

    @property
    def is_html(self) -> bool:
        return any(kind in self.content_type for kind in HTML_TYPES)

    @property
    def is_text(self) -> bool:
        return any(kind in self.content_type for kind in TEXT_TYPES)


@dataclass
class CrawledPage:
    """Satu halaman web yang sudah jadi teks siap diindeks."""

    url: str
    title: str
    text: str
    content_type: str = "text/html"
    parser: str = "html"
    bytes_len: int = 0
    depth: int = 0
    is_file: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "content_type": self.content_type,
            "parser": self.parser,
            "bytes": self.bytes_len,
            "depth": self.depth,
            "is_file": self.is_file,
        }


@dataclass
class CrawlResult:
    pages: List[CrawledPage] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    started_at: str = ""
    elapsed_ms: float = 0.0

    @property
    def total_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)


class WebFetchError(Exception):
    """Kegagalan mengambil halaman web yang boleh ditampilkan ke pemanggil."""


# --------------------------------------------------------------------------- #
# Mengambil satu URL dengan pengaman
# --------------------------------------------------------------------------- #


def _safe_get(
    client,
    url: str,
    *,
    settings,
    max_bytes: int,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    max_redirects: int = 5,
) -> FetchedPage:
    """GET satu URL, memeriksa SETIAP lompatan pengalihan sebelum diikuti."""
    allow_private = bool(getattr(settings, "allow_private_urls", False))
    current = url
    for _hop in range(max_redirects + 1):
        assert_public_url(current, allow_private=allow_private)
        request_headers = {"User-Agent": getattr(settings, "web_user_agent", "RAG-Service/1.0")}
        request_headers.update(headers or {})
        try:
            with client.stream(
                "GET", current, headers=request_headers, timeout=timeout, follow_redirects=False
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location") or ""
                    if not location:
                        raise WebFetchError(f"pengalihan tanpa Location dari {current}")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise WebFetchError(f"HTTP {response.status_code} dari {current}")
                declared = int(response.headers.get("content-length") or 0)
                if declared and declared > max_bytes:
                    raise WebFetchError(
                        f"halaman {current} menyatakan {declared / 1048576:.1f} MB, "
                        f"melebihi batas {max_bytes / 1048576:.1f} MB"
                    )
                buffer = bytearray()
                for block in response.iter_bytes():
                    buffer.extend(block)
                    if len(buffer) > max_bytes:
                        raise WebFetchError(
                            f"halaman {current} melebihi batas {max_bytes / 1048576:.1f} MB"
                        )
                return FetchedPage(
                    url=url,
                    final_url=current,
                    status=response.status_code,
                    content_type=(response.headers.get("content-type") or "").lower(),
                    content=bytes(buffer),
                )
        except WebFetchError:
            raise
        except UrlRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - jaringan
            raise WebFetchError(f"tidak bisa mengambil {current}: {exc}") from exc
    raise WebFetchError(f"terlalu banyak pengalihan saat mengambil {url}")


def fetch_url(url: str, settings) -> FetchedPage:
    """Ambil satu URL (HTML atau berkas) dengan seluruh pengaman di atas."""
    import httpx

    limit = int(getattr(settings, "max_upload_bytes", 32 * 1024 * 1024))
    timeout = float(getattr(settings, "file_fetch_timeout", 120.0))
    with httpx.Client() as client:
        return _safe_get(client, url, settings=settings, max_bytes=limit, timeout=timeout)


# --------------------------------------------------------------------------- #
# Mengubah halaman jadi teks
# --------------------------------------------------------------------------- #


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - deteksi terbaik-usaha
        pass
    return raw.decode("utf-8", "ignore")


def extract_links(html: str, base_url: str) -> List[str]:
    """Semua tautan yang bisa diikuti, sudah dijadikan absolut dan dibersihkan."""
    found: List[str] = []
    seen: Set[str] = set()
    for match in _HREF_RE.finditer(html or ""):
        raw = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith(SKIP_SCHEMES):
            continue
        absolute = urllib.parse.urljoin(base_url, raw)
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        path = (parsed.path or "").lower()
        if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        if any(re.search(pattern, absolute, re.I) for pattern in SKIP_PATH_PATTERNS):
            continue
        cleaned = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        if cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)
    return found


def page_to_text(page: FetchedPage) -> Tuple[str, str, List[str]]:
    """``(title, text, links)`` untuk halaman HTML/teks; berkas lain lewat parser biasa."""
    raw = page.content
    if page.is_html:
        html = _decode(raw)
        text = _html_to_text(html)
        match = _TITLE_RE.search(html)
        title = _html_to_text(match.group(1)).strip()[:160] if match else ""
        links = extract_links(html, page.final_url)
        return title, text, links
    if page.is_text:
        return "", _decode(raw).strip(), []
    # Bukan HTML/teks: biarkan parser berkas yang menangani (PDF, DOCX, ...).
    parsed = parse_document(raw, _name_from_url(page.final_url), page.content_type)
    return parsed.document_name, parsed.text, []


def _name_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = path.rsplit("/", 1)[-1] or "halaman"
    return urllib.parse.unquote(name) or "halaman"


def _canonical_of(html: str, base: str) -> str:
    match = _CANONICAL_RE.search(html or "")
    if not match:
        return ""
    return urllib.parse.urljoin(base, match.group(1).strip())


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #


class _Robots:
    """Pembungkus robots.txt sederhana dengan cache per host."""

    def __init__(self, client, settings) -> None:
        self._client = client
        self._settings = settings
        self._cache: Dict[str, Any] = {}

    def allows(self, url: str) -> Tuple[bool, str]:
        if not getattr(self._settings, "web_crawl_respect_robots", True):
            return True, ""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._cache:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = origin + "/robots.txt"
            try:
                page = _safe_get(
                    self._client,
                    robots_url,
                    settings=self._settings,
                    max_bytes=512 * 1024,
                    timeout=min(10.0, float(getattr(self._settings, "web_crawl_timeout", 20.0))),
                )
                parser.parse(_decode(page.content).splitlines())
            except Exception:  # noqa: BLE001 - robots tidak wajib ada
                parser.allow_all = True
            self._cache[origin] = parser
        parser = self._cache[origin]
        agent = getattr(self._settings, "web_user_agent", "*")
        try:
            allowed = parser.can_fetch(agent, url)
        except Exception:  # noqa: BLE001
            allowed = True
        return allowed, ("" if allowed else "dilarang robots.txt")


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #


def crawl(
    start_url: str,
    settings,
    *,
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
    same_host: Optional[bool] = None,
    follow_files: Optional[bool] = None,
) -> CrawlResult:
    """Jelajahi situs mulai dari ``start_url`` dan kembalikan halaman-halamannya sebagai teks."""
    import httpx

    started = time.perf_counter()
    if not getattr(settings, "web_crawl_enabled", True):
        raise WebFetchError("crawl web dimatikan di setelan layanan (WEB_CRAWL_ENABLED=false)")

    limit_pages = int(max_pages or getattr(settings, "web_crawl_max_pages", 20))
    limit_depth = int(max_depth if max_depth is not None else getattr(settings, "web_crawl_max_depth", 2))
    limit_pages = max(1, min(limit_pages, 200))
    limit_depth = max(0, min(limit_depth, 5))
    only_same_host = bool(getattr(settings, "web_crawl_same_host", True) if same_host is None else same_host)
    take_files = bool(getattr(settings, "web_crawl_follow_files", True) if follow_files is None else follow_files)
    page_bytes = int(getattr(settings, "web_crawl_max_page_bytes", 5 * 1024 * 1024))
    timeout = float(getattr(settings, "web_crawl_timeout", 20.0))

    result = CrawlResult(started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    start_host = urllib.parse.urlsplit(start_url).hostname or ""

    with httpx.Client() as client:
        robots = _Robots(client, settings)
        queue: List[Tuple[str, int]] = [(normalize_for_visit(start_url), 0)]
        seen: Set[str] = set()

        while queue and len(result.pages) < limit_pages:
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            allowed, reason = robots.allows(url)
            if not allowed:
                result.skipped.append({"url": url, "reason": reason})
                continue
            try:
                page = _safe_get(
                    client, url, settings=settings, max_bytes=page_bytes, timeout=timeout
                )
            except (WebFetchError, UrlRejected) as exc:
                result.skipped.append({"url": url, "reason": str(exc)})
                continue

            is_document = not (page.is_html or page.is_text)
            if is_document and not take_files:
                result.skipped.append({"url": url, "reason": "berkas yang ditautkan tidak diambil"})
                continue
            try:
                title, text, links = page_to_text(page)
            except Exception as exc:  # noqa: BLE001 - halaman rusak tidak menghentikan crawl
                result.skipped.append({"url": url, "reason": f"tidak bisa dibaca: {exc}"})
                continue
            if not text.strip():
                result.skipped.append({"url": url, "reason": "tidak ada teks terbaca"})
                continue

            result.pages.append(
                CrawledPage(
                    url=page.final_url,
                    title=title or _name_from_url(page.final_url),
                    text=text,
                    content_type=page.content_type or ("text/html" if page.is_html else ""),
                    parser="html" if page.is_html else ("text" if page.is_text else "file"),
                    bytes_len=len(page.content),
                    depth=depth,
                    is_file=is_document,
                )
            )
            if is_document or depth >= limit_depth:
                continue

            # Kanonik dari halaman ini dipakai supaya /a dan /a?x tidak jadi dua dokumen.
            html = _decode(page.content) if page.is_html else ""
            canonical = _canonical_of(html, page.final_url)
            for link in links:
                if link == normalize_for_visit(page.final_url):
                    continue
                if canonical and link == normalize_for_visit(canonical):
                    continue
                if only_same_host:
                    if (urllib.parse.urlsplit(link).hostname or "") != start_host:
                        continue
                target = normalize_for_visit(link)
                if target not in seen:
                    queue.append((target, depth + 1))

    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "crawl selesai: %d halaman, %d dilewati, %d karakter, %.0f ms",
        len(result.pages),
        len(result.skipped),
        result.total_chars,
        result.elapsed_ms,
    )
    return result


def describe_settings(settings) -> Dict[str, Any]:
    """Ringkasan kebijakan crawl untuk ``/ready`` dan layar Pengaturan."""
    return {
        "enabled": bool(getattr(settings, "web_crawl_enabled", True)),
        "max_pages": int(getattr(settings, "web_crawl_max_pages", 20)),
        "max_depth": int(getattr(settings, "web_crawl_max_depth", 2)),
        "same_host": bool(getattr(settings, "web_crawl_same_host", True)),
        "follow_files": bool(getattr(settings, "web_crawl_follow_files", True)),
        "respect_robots": bool(getattr(settings, "web_crawl_respect_robots", True)),
        "allow_private_urls": bool(getattr(settings, "allow_private_urls", False)),
    }