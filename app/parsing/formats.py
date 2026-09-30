"""Katalog format berkas yang bisa jadi knowledge (satu sumber kebenaran).

Setiap format menyebut ekstensi, MIME yang biasanya dilaporkan browser, dan - kalau
ekstraksinya butuh sesuatu di luar pustaka standar - apa yang dibutuhkan. Katalog ini
dipakai untuk:

* validasi upload (`app/core/security.py`), supaya daftar ekstensi yang boleh datang
  dari setelan runtime, bukan dari konstanta di kode;
* laporan `/ready` dan layar Pengaturan, supaya operator melihat format mana yang
  benar-benar tersedia di mesin ini dan mana yang belum (mis. gambar butuh OCR);
* routing parser (`app/parsing/parser.py`).
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Katalog
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormatSpec:
    key: str
    label: str
    group: str
    extensions: Tuple[str, ...]
    mimes: Tuple[str, ...] = ()
    parser: str = "text"
    requires_module: str = ""
    requires_binary: str = ""
    note: str = ""

    @property
    def requirement(self) -> str:
        return self.requires_module or self.requires_binary


CATALOG: Tuple[FormatSpec, ...] = (
    # ---- Dokumen ---------------------------------------------------------- #
    FormatSpec("pdf", "PDF", "Dokumen", (".pdf",), ("application/pdf",), "pdf"),
    FormatSpec("docx", "Word modern", "Dokumen", (".docx", ".docm"),
               ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",), "docx"),
    FormatSpec("odt", "OpenDocument Text", "Dokumen", (".odt",), ("application/vnd.oasis.opendocument.text",), "odf"),
    FormatSpec("rtf", "Rich Text", "Dokumen", (".rtf",), ("application/rtf", "text/rtf"), "rtf"),
    FormatSpec("epub", "EPUB", "Dokumen", (".epub",), ("application/epub+zip",), "epub"),
    FormatSpec("doc", "Word lama (.doc)", "Dokumen", (".doc",), ("application/msword",), "doc_binary",
               note="format biner Word 97-2003, dibaca tanpa pustaka tambahan"),
    # ---- Presentasi ------------------------------------------------------- #
    FormatSpec("pptx", "PowerPoint modern", "Presentasi", (".pptx", ".pptm"),
               ("application/vnd.openxmlformats-officedocument.presentationml.presentation",), "pptx"),
    FormatSpec("odp", "OpenDocument Presentation", "Presentasi", (".odp",),
               ("application/vnd.oasis.opendocument.presentation",), "odf"),
    FormatSpec("ppt", "PowerPoint lama (.ppt)", "Presentasi", (".ppt",), ("application/vnd.ms-powerpoint",), "ppt_binary",
               note="format biner PowerPoint 97-2003, dibaca tanpa pustaka tambahan"),
    # ---- Spreadsheet ------------------------------------------------------ #
    FormatSpec("xlsx", "Excel modern", "Spreadsheet", (".xlsx", ".xlsm"),
               ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",), "xlsx"),
    FormatSpec("ods", "OpenDocument Spreadsheet", "Spreadsheet", (".ods",),
               ("application/vnd.oasis.opendocument.spreadsheet",), "odf"),
    FormatSpec("csv", "CSV", "Spreadsheet", (".csv",), ("text/csv",), "csv"),
    FormatSpec("tsv", "TSV", "Spreadsheet", (".tsv",), ("text/tab-separated-values",), "csv"),
    FormatSpec("xls", "Excel lama (.xls)", "Spreadsheet", (".xls",), ("application/vnd.ms-excel",), "xls_binary",
               note="format biner Excel 97-2003 (BIFF8), dibaca tanpa pustaka tambahan"),
    # ---- Teks ------------------------------------------------------------- #
    FormatSpec("txt", "Teks biasa", "Teks", (".txt",), ("text/plain",), "text"),
    FormatSpec("md", "Markdown", "Teks", (".md", ".markdown"), ("text/markdown",), "text"),
    FormatSpec("html", "HTML", "Teks", (".html", ".htm", ".xhtml"), ("text/html",), "html"),
    FormatSpec("json", "JSON", "Teks", (".json",), ("application/json",), "json"),
    FormatSpec("jsonl", "JSON Lines", "Teks", (".jsonl", ".ndjson"), ("application/x-ndjson",), "text"),
    FormatSpec("xml", "XML", "Teks", (".xml",), ("application/xml", "text/xml"), "xml"),
    FormatSpec("yaml", "YAML", "Teks", (".yaml", ".yml"), ("application/yaml", "text/yaml"), "text"),
    FormatSpec("log", "Log", "Teks", (".log",), ("text/plain",), "text"),
    FormatSpec("sql", "SQL", "Teks", (".sql",), ("application/sql",), "text"),
    FormatSpec("ini", "INI / konfigurasi", "Teks", (".ini", ".conf", ".cfg", ".env"), ("text/plain",), "text"),
    FormatSpec("rst", "reStructuredText", "Teks", (".rst",), ("text/x-rst",), "text"),
    FormatSpec("tex", "LaTeX", "Teks", (".tex",), ("text/x-tex",), "text"),
    FormatSpec("eml", "Email (.eml)", "Teks", (".eml",), ("message/rfc822",), "eml",
               note="isi surat + nama lampiran ikut dibaca"),
    # ---- Gambar (butuh OCR) ----------------------------------------------- #
    FormatSpec("image", "Gambar dengan teks (OCR)", "Gambar", (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"),
               ("image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"), "image_ocr",
               requires_binary="tesseract", note="perlu tesseract-ocr terpasang untuk membaca teks"),
)

BY_KEY: Dict[str, FormatSpec] = {spec.key: spec for spec in CATALOG}

# Ekstensi yang aktif kalau operator belum memilih apa pun di layar Pengaturan.
# Gambar sengaja tidak ikut: butuh OCR dan hasilnya paling tidak pasti.
DEFAULT_ENABLED_KEYS: Tuple[str, ...] = (
    "pdf", "docx", "doc", "odt", "rtf", "epub", "pptx", "ppt", "odp",
    "xlsx", "xls", "ods", "csv", "tsv",
    "txt", "md", "html", "json", "jsonl", "xml", "yaml", "log", "sql", "ini", "rst", "tex", "eml",
)


def extension_map() -> Dict[str, FormatSpec]:
    mapping: Dict[str, FormatSpec] = {}
    for spec in CATALOG:
        for ext in spec.extensions:
            mapping[ext] = spec
    return mapping


EXTENSION_MAP: Dict[str, FormatSpec] = extension_map()
ALL_EXTENSIONS: Tuple[str, ...] = tuple(sorted(EXTENSION_MAP))


def default_extensions() -> List[str]:
    out: List[str] = []
    for key in DEFAULT_ENABLED_KEYS:
        out.extend(BY_KEY[key].extensions)
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# Ketersediaan di mesin ini
# --------------------------------------------------------------------------- #


def _module_available(name: str) -> bool:
    if not name:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - spec lookup edge cases
        return False


def _binary_available(name: str) -> bool:
    return bool(shutil.which(name)) if name else True


def availability(spec: FormatSpec) -> bool:
    """True bila ekstraksi teks format ini bisa dijalankan di mesin ini."""
    return _module_available(spec.requires_module) and _binary_available(spec.requires_binary)


def missing_requirement(spec: FormatSpec) -> str:
    if spec.requires_module and not _module_available(spec.requires_module):
        return f"pustaka {spec.requires_module} belum terpasang"
    if spec.requires_binary and not _binary_available(spec.requires_binary):
        return f"program {spec.requires_binary} belum terpasang"
    return spec.note


def describe() -> List[Dict[str, object]]:
    """Bentuk katalog untuk API/UI."""
    rows: List[Dict[str, object]] = []
    for spec in CATALOG:
        available = availability(spec)
        rows.append({
            "key": spec.key,
            "label": spec.label,
            "group": spec.group,
            "extensions": list(spec.extensions),
            "mimes": list(spec.mimes),
            "available": available,
            "note": spec.note if available else missing_requirement(spec),
            "default": spec.key in DEFAULT_ENABLED_KEYS,
        })
    return rows


# --------------------------------------------------------------------------- #
# Ekstensi efektif (setelan runtime)
# --------------------------------------------------------------------------- #


def normalize_extensions(values: Sequence[str]) -> List[str]:
    """Rapikan daftar ekstensi: huruf kecil, ada titik, unik, terurut, terdaftar."""
    out: List[str] = []
    for raw in values:
        item = str(raw or "").strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        if item not in EXTENSION_MAP:
            raise ValueError(f"ekstensi tidak dikenal: {item}")
        if item not in out:
            out.append(item)
    return sorted(out)


def enabled_extensions(settings) -> List[str]:
    """Ekstensi yang boleh diunggah sekarang: pilihan operator, kalau tidak ada -> default.

    Format yang tidak tersedia di mesin ini tidak ikut, jadi UI dan validasi selalu sama
    dengan apa yang benar-benar bisa diekstrak.
    """
    configured = getattr(settings, "upload_extensions", "") or ""
    raw = [part for part in str(configured).split(",") if part.strip()]
    if not raw:
        raw = default_extensions()
    try:
        wanted = normalize_extensions(raw)
    except ValueError:
        wanted = default_extensions()
    usable = [ext for ext in wanted if availability(EXTENSION_MAP[ext])]
    return usable


def mimes_for(extensions: Sequence[str]) -> List[str]:
    out: List[str] = []
    for ext in extensions:
        spec = EXTENSION_MAP.get(ext)
        if not spec:
            continue
        for mime in spec.mimes:
            if mime not in out:
                out.append(mime)
    return out
