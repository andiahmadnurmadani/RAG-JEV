"""Security primitives: API keys, KMS tenant tokens, file validation, redaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core.config import Settings
from app.core.errors import AppError

# --------------------------------------------------------------------------- #
# Trusted tenant context (PRD 6, 19, 21)
# --------------------------------------------------------------------------- #

REQUIRED_CONTEXT_FIELDS = ("user_id", "organization_id", "application_id")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_context_token(settings: Settings, token: str) -> Dict[str, Any]:
    """Verify an HS256 trusted-context token issued by KMS / the API gateway.

    The token is the *only* accepted source of ``organization_id`` for a request
    that carries no API-key binding (PRD 6 / 19 / 48).
    """
    if not settings.kms_shared_secret:
        raise AppError("TENANT_CONTEXT_MISSING", "KMS shared secret is not configured")
    parts = token.split(".")
    if len(parts) != 3:
        raise AppError("AUTH_INVALID", "Malformed tenant context token")
    header_b64, payload_b64, signature_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(settings.kms_shared_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        raise AppError("AUTH_INVALID", "Tenant context signature mismatch")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise AppError("AUTH_INVALID", "Tenant context payload is not valid JSON") from exc
    exp = payload.get("exp")
    if exp is not None and float(exp) < time.time():
        raise AppError("AUTH_INVALID", "Tenant context token expired")
    for field in REQUIRED_CONTEXT_FIELDS:
        if not payload.get(field):
            raise AppError("TENANT_CONTEXT_MISSING", f"Tenant context token missing '{field}'")
    payload.setdefault("permissions", [])
    return payload


def issue_context_token(settings: Settings, payload: Dict[str, Any]) -> str:
    """Helper for tests and for the KMS mock: sign a trusted-context token."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    body.setdefault("exp", int(time.time()) + 3600)
    payload_b64 = _b64url_encode(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload_b64}".encode("ascii")
    sig = hmac.new(settings.kms_shared_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload_b64}.{_b64url_encode(sig)}"


def resolve_api_key(settings: Settings, presented: str) -> Dict[str, Any]:
    """Map a presented credential onto its trusted context (constant-time compare).

    Tiga sumber, satu jawaban: kunci di ``API_KEYS_JSON`` (tanam saat deploy), kunci yang
    dibuat dari layar Pengaturan (registry menyimpan hash saja), dan sesi konsol hasil
    menukar kode akses (``sess_...``, berlaku 12 jam / 7 hari bila "ingat saya").
    """
    if not presented:
        raise AppError("AUTH_INVALID", "Missing API key")

    from app.core.access import SESSION_PREFIX, access_store, session_context, session_store

    if presented.startswith(SESSION_PREFIX):
        store = session_store(settings)
        generation = access_store(settings).generation()
        session = store.resolve(presented, generation)
        if session is None:
            raise AppError(
                "AUTH_INVALID",
                "Sesi tidak berlaku lagi (kedaluwarsa, dikeluarkan, atau kode akses sudah diganti)",
            )
        return session_context(settings, session)

    for key, ctx in settings.api_keys.items():
        if constant_time_equals(key, presented):
            out = dict(ctx)
            out.setdefault("permissions", [])
            out["key_id"] = None
            out["source"] = "env"
            return out

    from app.core.api_keys import registry_for

    context = registry_for(settings).resolve(presented)
    if context is None:
        raise AppError("AUTH_INVALID", "Invalid API key")
    return context


def require_permission(context: Dict[str, Any], permission: str) -> None:
    permissions: Iterable[str] = context.get("permissions") or []
    allowed = "write" in permissions and permission in {"write", "read"} or permission in permissions
    if not allowed and "*" not in permissions:
        raise AppError(
            "AUTH_FORBIDDEN",
            f"Application is not permitted to perform '{permission}'",
            details={"permission": permission},
        )


# --------------------------------------------------------------------------- #
# Request-body guard: organization_id must never arrive from the prompt (PRD 6)
# --------------------------------------------------------------------------- #

FORBIDDEN_BODY_FIELDS = ("organization_id", "org_id", "tenant_id")


def reject_client_tenant_fields(body: Any, where: str) -> None:
    """Fail *before* any work is done if the client tries to choose its tenant."""
    if isinstance(body, dict):
        for field in FORBIDDEN_BODY_FIELDS:
            if field in body:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"'{field}' must not be supplied by the client; tenant comes from the trusted context",
                    details={"field": field, "location": where},
                )
        for value in body.values():
            reject_client_tenant_fields(value, where)


# --------------------------------------------------------------------------- #
# File validation (PRD 34)
# --------------------------------------------------------------------------- #

FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

MIME_ALIASES = {
    "text/x-markdown": "text/markdown",
    "application/x-pdf": "application/pdf",
    "text/xml": "application/xml",
    "application/octet-stream": "application/octet-stream",
}


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "document")
    name = Path(name).name  # drop any directory component
    name = FILENAME_SAFE.sub("_", name).strip("._") or "document"
    return name[:180]


ALLOWED_UPLOAD_SUFFIXES = {
    ".pdf", ".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".json",
    ".xlsx", ".xlsm", ".csv", ".tsv", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".epub",
    ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".log", ".sql", ".ini", ".conf", ".cfg", ".rst", ".tex",
    ".xhtml", ".env",
}


def allowed_upload_suffixes(settings: Optional[Settings] = None) -> set:
    """Ekstensi yang boleh diunggah sekarang (dari setelan runtime, fallback ke bawaan)."""
    target = settings
    if target is None:
        from app.core.config import get_settings

        target = get_settings()
    try:
        from app.parsing.formats import enabled_extensions

        configured = str(getattr(target, "upload_extensions", "") or "").strip()
        resolved = set(enabled_extensions(target))
        if resolved or configured:
            # Daftar eksplisit dihormati apa pun hasilnya: kalau operator hanya memilih format
            # yang belum didukung mesin ini, hasilnya kosong (menolak semua) - bukan diam-diam
            # kembali ke daftar bawaan yang jauh lebih lebar.
            return resolved
    except Exception:  # noqa: BLE001 - katalog bermasalah tidak boleh membuka jalan upload
        pass
    return set(ALLOWED_UPLOAD_SUFFIXES)


def validate_display_label(filename: str, settings: Optional[Settings] = None) -> None:
    """The human label of a document: an extension is optional, a *wrong* one is not allowed.

    Inline text whose label is ``SOP Cuti 2026`` is legitimate (the extension check rejected
    it until this existed). A label that advertises a type we refuse to store (``payload.exe``)
    is still refused: the label is what users see and what the caller claims the artifact is.
    """
    allowed = allowed_upload_suffixes(settings)
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in allowed:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE",
            f"Unsupported file type '{suffix}'",
            details={"allowed": sorted(allowed)},
        )


def validate_payload_size(settings: Settings, content_base64: Optional[str] = None, text: Optional[str] = None) -> None:
    """Tolak payload yang sudah pasti kelebihan batas, sebelum job dibuat.

    ``content_base64`` dihitung panjangnya dari string (tanpa mendekode 2 MB base64 hanya
    untuk menolaknya), ``text`` dari panjang UTF-8. Sumber ``file_url`` tidak bisa diketahui
    sebelum diunduh - itu tetap diperiksa worker sebagai backstop.
    """
    limit = settings.max_upload_mb * 1024 * 1024
    estimated = 0
    if content_base64:
        stripped = content_base64.strip()
        padding = len(stripped) - len(stripped.rstrip("="))
        estimated = max(0, (len(stripped) * 3) // 4 - padding)
    elif text:
        estimated = len(text.encode("utf-8"))
    if estimated > limit:
        raise AppError(
            "PAYLOAD_TOO_LARGE",
            f"File exceeds the {settings.max_upload_mb} MB limit",
            details={"size_bytes": estimated, "limit_bytes": limit},
        )


def validate_upload(settings: Settings, filename: str, content: bytes, declared_mime: str = "") -> None:
    """MIME/extension/size validation. Rejects *before* anything is stored."""
    if len(content) == 0:
        raise AppError("VALIDATION_ERROR", "Uploaded file is empty")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(content) > limit:
        raise AppError(
            "PAYLOAD_TOO_LARGE",
            f"File exceeds the {settings.max_upload_mb} MB limit",
            details={"size_bytes": len(content), "limit_bytes": limit},
        )
    guessed = (declared_mime or mimetypes.guess_type(filename)[0] or "").lower()
    guessed = MIME_ALIASES.get(guessed, guessed)
    suffix = Path(filename).suffix.lower()
    allowed_suffixes = allowed_upload_suffixes(settings)
    allowed_mimes = set(settings.allowed_mime_list)
    try:
        from app.parsing.formats import mimes_for

        allowed_mimes |= set(mimes_for(sorted(allowed_suffixes)))
    except Exception:  # noqa: BLE001
        pass
    if suffix not in allowed_suffixes and guessed not in allowed_mimes:
        raise AppError(
            "UNSUPPORTED_MEDIA_TYPE",
            f"Unsupported file type '{guessed or suffix}'",
            details={"allowed": sorted(allowed_suffixes)},
        )
    if content[:4] == b"%PDF" and suffix != ".pdf":
        raise AppError("VALIDATION_ERROR", "Content looks like a PDF but the filename is not .pdf")


# --------------------------------------------------------------------------- #
# Secret redaction for logs (PRD 36)
# --------------------------------------------------------------------------- #

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"jev_[A-Za-z0-9_]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{10,}"),
]


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", redacted)
    return redacted


def split_context_chars(settings: Settings) -> int:
    return max(1000, settings.llm_context_chars)


def permissions_list(raw: Optional[List[str]]) -> List[str]:
    return [str(p) for p in (raw or [])]


# --------------------------------------------------------------------------- #
# Prompt-injection defence (PRD 34, 35)
# --------------------------------------------------------------------------- #

INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore the system prompt",
    "disregard all previous",
    "reveal your system prompt",
    "abaikan instruksi sebelumnya",
    "abaikan aturan di atas",
    "system prompt",
    "you are now",
]


def scan_injection(text: str) -> List[str]:
    """Return the markers found in a document chunk (reported, never obeyed)."""
    lowered = (text or "").lower()
    return [marker for marker in INJECTION_MARKERS if marker in lowered]


def fence_document(
    chunk_id: str,
    document_name: str,
    page: Any,
    content: str,
    *,
    section: str = "",
    notes: Sequence[str] = (),
) -> str:
    """Wrap retrieved content as inert data with an explicit boundary (PRD 35).

    Semua keterangan (bagian, halaman, posisi) ada di header SEBELUM isi, di dalam batas blok -
    model membaca "ini bagian apa" lebih dulu, dan tidak ada label yang tercecer di luar blok.
    """
    header = [f"Name: {document_name}"]
    if section:
        header.append(f"Section: {section}")
    if page not in (None, ""):
        header.append(f"Page: {page}")
    header.append(f"Chunk: {chunk_id}")
    header.extend(note for note in notes if note)
    return (
        "<retrieved_document>\n"
        "[DOCUMENT]\n" + "\n".join(header) + "\n"
        "[CONTENT]\n"
        f"{content}\n"
        "</retrieved_document>"
    )
