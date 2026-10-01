"""Settings screen API: LLM + Jev configuration, and probe helpers.

Everything here is **global** configuration (it changes generation and routing for
every tenant), so every route requires the ``admin`` permission. Keys are write-only:
reads return ``api_key_set`` + a short hint, never the value.

The probe routes exist so the screen can answer "is this URL/key/model usable?" before
saving, instead of leaving a broken configuration behind. They talk to operator-supplied
URLs, so the URL is validated first (:func:`validate_probe_url`): http/https only, no
credentials in the URL, no link-local/metadata endpoints, no redirects followed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.api.deps import Services, services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.core import settings_store
from app.core.access import (
    MAX_ACTIVE_SESSIONS,
    MIN_CODE_LENGTH,
    access_store,
    session_lifetime,
    session_store,
    validate_code,
)
from app.core.api_keys import ALLOWED_PERMISSIONS, DEFAULT_PERMISSIONS, MAX_ACTIVE_KEYS, registry_for
from app.core.errors import AppError, ok
from app.core.logging import get_logger
from app.jev.systemone import SystemOneClient
from app.rag.generator import build_llm_client

logger = get_logger(__name__)
router = APIRouter(tags=["settings"])

_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata", "localhost.localdomain"}
_MAX_MODELS = 500


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: Optional[Dict[str, Any]] = None
    jev: Optional[Dict[str, Any]] = None
    uploads: Optional[Dict[str, Any]] = None
    retrieval: Optional[Dict[str, Any]] = None
    web: Optional[Dict[str, Any]] = None
    summary: Optional[Dict[str, Any]] = None


class ModelsProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: Optional[str] = None
    api_key: Optional[str] = None


class JevProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Optional[str] = None
    url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


class ApiKeyCreateRequest(BaseModel):
    """Permintaan membuat kunci baru.

    ``organization_id``/``user_id``/``application_id`` di sini **bukan** tenant dari klien:
    ini operator admin yang menentukan konteks kunci *baru* - dan hanya boleh berbeda dari
    konteksnya sendiri bila kunci yang dipakainya berizin ``*``. Kosong = warisi konteks
    pemanggil (default yang aman).
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    permissions: Optional[List[str]] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    application_id: Optional[str] = None
    expires_in_days: Optional[int] = None


class AccessCodeRequest(BaseModel):
    """Ganti kode akses konsol.

    ``current_code`` wajib bila pemanggil masuk sebagai **sesi** (kode lama membuktikan bahwa
    yang mengganti memang pemiliknya, bukan sesi yang dibajak). Kunci API berizin ``admin``
    boleh mengganti tanpa kode lama karena ia tidak berasal dari kode itu.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    current_code: Optional[str] = None


def _settings_path(services: Services) -> Path:
    return Path(services.settings.settings_override_path)


def _require_admin(context: TrustedContext) -> None:
    context.require("admin")


def _effective(value: Optional[str], fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def validate_probe_url(url: str) -> str:
    """Reject anything that could turn a probe into a request forgery or a file read."""
    url = (url or "").strip()
    if not url:
        raise AppError("VALIDATION_ERROR", "base_url is required for a probe")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise AppError("VALIDATION_ERROR", "base_url must start with http:// or https://", details={"scheme": parsed.scheme})
    if not parsed.hostname:
        raise AppError("VALIDATION_ERROR", "base_url has no host")
    if parsed.username or parsed.password:
        raise AppError("VALIDATION_ERROR", "base_url must not embed credentials")
    if parsed.hostname.lower() in _BLOCKED_HOSTS:
        raise AppError("VALIDATION_ERROR", "base_url points at a link-local/metadata host")
    return url.rstrip("/")


def _decorate_uploads(body: Dict[str, Any], settings) -> Dict[str, Any]:
    """Tambahkan daftar format efektif + katalog ke balasan settings.

    ``sections.uploads.extensions`` yang tersimpan bisa kosong (berarti "pakai default"),
    jadi layar Pengaturan selalu menerima daftar konkret yang benar-benar berlaku, plus
    katalog lengkap (grup, label, ketersediaan di mesin ini) untuk menggambar centang.
    """
    from app.parsing import formats

    section = body.setdefault("sections", {}).setdefault("uploads", {})
    configured = list(section.get("extensions") or [])
    section["extensions"] = formats.enabled_extensions(settings)
    section["configured"] = configured
    section["available_extensions"] = [ext for ext in formats.ALL_EXTENSIONS
                                       if formats.availability(formats.EXTENSION_MAP[ext])]
    section["max_upload_mb"] = int(getattr(settings, "max_upload_mb", 0) or 0)
    body["catalog"] = formats.describe()
    return body


def _validate_uploads(updates: Dict[str, Dict[str, Any]]) -> None:
    """Tolak ekstensi tak dikenal sebelum apa pun ditulis."""
    from app.parsing import formats

    section = updates.get("uploads")
    if not section:
        return
    if "extensions" in section:
        try:
            normalized = formats.normalize_extensions(section["extensions"])
        except ValueError as exc:
            raise AppError(
                "VALIDATION_ERROR",
                str(exc),
                details={"available": list(formats.ALL_EXTENSIONS)},
            ) from exc
        if not normalized:
            raise AppError("VALIDATION_ERROR", "Pilih minimal satu ekstensi")
        unusable = [ext for ext in normalized if not formats.availability(formats.EXTENSION_MAP[ext])]
        if len(unusable) == len(normalized):
            raise AppError(
                "VALIDATION_ERROR",
                "Tidak ada format terpilih yang bisa dibaca di mesin ini",
                details={"unavailable": unusable},
            )
        section["extensions"] = normalized
        if unusable:
            logger.info("ekstensi tanpa dukungan di mesin ini diabaikan: %s", ",".join(unusable))
    if "max_upload_mb" in section:
        try:
            limit = int(section["max_upload_mb"])
        except (TypeError, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", "max_upload_mb harus berupa angka bulat") from exc
        if not 1 <= limit <= 512:
            raise AppError("VALIDATION_ERROR", "max_upload_mb harus antara 1 dan 512", details={"max_upload_mb": limit})


def _validate_retrieval(updates: Dict[str, Dict[str, Any]]) -> None:
    """Batas kewajaran sebelum disimpan: nilai ngawur membuat SEMUA permintaan gagal.

    Anggaran konteks yang jauh melebihi jendela model bukan "lebih lengkap", tapi panggilan
    yang ditolak penyedia - dan itu lebih buruk daripada jawaban sebagian. Jadi nilainya
    dibatasi, bukan diterima apa adanya.
    """

    section = updates.get("retrieval")
    if not section:
        section = None
    limits = {
        "context_max_tokens": (2000, 200_000),
        "final_top_k": (1, 50),
        "max_chunks_per_document": (1, 200),
        "reranker_enabled": None,
        "strict_grounding": None,
        "context_expand_documents": None,
    }
    for field, bounds in limits.items():
        if not section or field not in section or bounds is None:
            continue
        low, high = bounds
        try:
            value = int(section[field])
        except (TypeError, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", f"{field} harus berupa angka bulat") from exc
        if not low <= value <= high:
            raise AppError(
                "VALIDATION_ERROR",
                f"{field} harus antara {low} dan {high}",
                details={field: value},
            )
        section[field] = value

    llm = updates.get("llm") or {}
    if "max_tokens" in llm:
        try:
            value = int(llm["max_tokens"])
        except (TypeError, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", "max_tokens harus berupa angka bulat") from exc
        if not 64 <= value <= 64_000:
            raise AppError(
                "VALIDATION_ERROR",
                "max_tokens harus antara 64 dan 64000",
                details={"max_tokens": value},
            )
        llm["max_tokens"] = value

    web = updates.get("web") or {}
    for field, low, high in (
        ("max_pages", 1, 200),
        ("max_depth", 0, 5),
    ):
        if field not in web:
            continue
        try:
            value = int(web[field])
        except (TypeError, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", f"web.{field} harus berupa angka bulat") from exc
        if not low <= value <= high:
            raise AppError(
                "VALIDATION_ERROR",
                f"web.{field} harus antara {low} dan {high}",
                details={f"web.{field}": value},
            )
        web[field] = value

    summary = updates.get("summary") or {}
    for field, low, high in (
        ("window_tokens", 1000, 200_000),
        ("max_tokens", 256, 32_000),
        ("max_documents", 1, 20),
    ):
        if field not in summary:
            continue
        try:
            value = int(summary[field])
        except (TypeError, ValueError) as exc:
            raise AppError("VALIDATION_ERROR", f"summary.{field} harus berupa angka bulat") from exc
        if not low <= value <= high:
            raise AppError(
                "VALIDATION_ERROR",
                f"summary.{field} harus antara {low} dan {high}",
                details={f"summary.{field}": value},
            )
        summary[field] = value


# --------------------------------------------------------------------------- #
@router.get("/settings")
def read_settings(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Masked current configuration (env + stored overrides)."""
    _require_admin(context)
    services = services_from_request(request)
    body = settings_store.describe(services.settings, _settings_path(services))
    return ok(_decorate_uploads(body, services.settings))


@router.put("/settings")
def update_settings(
    payload: SettingsUpdateRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Persist overrides, apply them to the live process, and report what changed."""
    _require_admin(context)
    services = services_from_request(request)
    path = _settings_path(services)

    updates = settings_store.extract_updates(payload.model_dump(exclude_none=True))
    if not updates:
        raise AppError("VALIDATION_ERROR", "no known settings field in the request")
    _validate_uploads(updates)
    _validate_retrieval(updates)

    merged = settings_store.merge(settings_store.read_overrides(path), updates)
    settings_store.write_overrides(path, merged)
    applied = settings_store.apply_overrides(services.settings, merged)

    # Rebind the objects that cached configuration at construction time.
    services.generator.rebind_llm_client(build_llm_client(services.settings))
    services.jev.reset_health_cache()
    logger.info("runtime settings updated fields=%s", ",".join(applied))

    body = _decorate_uploads(settings_store.describe(services.settings, path), services.settings)
    body["applied"] = applied
    return ok(body)


# --------------------------------------------------------------------------- #
@router.post("/settings/llm/models")
def list_llm_models(
    payload: ModelsProbeRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Ask an OpenAI-compatible endpoint which models it serves."""
    _require_admin(context)
    services = services_from_request(request)
    base = validate_probe_url(_effective(payload.base_url, services.settings.llm_base_url))
    key = _effective(payload.api_key, services.settings.llm_api_key)

    import httpx

    url = base + "/models"
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    started = time.perf_counter()
    try:
        response = httpx.get(url, headers=headers, timeout=min(services.settings.llm_timeout, 20.0))
    except Exception as exc:  # noqa: BLE001
        raise AppError("LLM_FAILED", f"Tidak bisa menghubungi {url}: {exc}") from exc
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    if response.status_code >= 400:
        raise AppError(
            "LLM_FAILED",
            f"Endpoint menjawab HTTP {response.status_code} untuk {url}",
            details={"status": response.status_code, "latency_ms": latency_ms},
        )
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        raise AppError("LLM_FAILED", "Respons /models bukan JSON yang bisa dibaca") from exc

    entries: List[Dict[str, Any]] = []
    raw = body.get("data") if isinstance(body, dict) else None
    if isinstance(raw, list):
        for item in raw[:_MAX_MODELS]:
            if isinstance(item, dict) and item.get("id"):
                entries.append({"id": str(item["id"]), "owned_by": item.get("owned_by") or ""})
            elif isinstance(item, str):
                entries.append({"id": item, "owned_by": ""})
    if not entries:
        raise AppError("LLM_FAILED", "Endpoint tidak mengembalikan daftar model di 'data[].id'")

    entries.sort(key=lambda item: item["id"].lower())
    return ok({"base_url": base, "count": len(entries), "models": entries, "latency_ms": latency_ms})


@router.post("/settings/jev/probe")
def probe_jev(
    payload: JevProbeRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """One cheap decision question: does this Jev endpoint answer, and how fast?"""
    _require_admin(context)
    services = services_from_request(request)
    settings = services.settings
    provider = _effective(payload.provider, settings.jev_provider)
    if provider != "systemone":
        raise AppError(
            "VALIDATION_ERROR",
            "Probe hanya tersedia untuk JEV_PROVIDER=systemone; transport MCP diuji lewat tools/call",
            details={"provider": provider},
        )

    stub = settings.model_copy(
        update={
            "jev_enabled": True,
            "jev_provider": "systemone",
            "jev_systemone_url": validate_probe_url(_effective(payload.url, settings.jev_systemone_url)),
            "jev_model": _effective(payload.model, settings.jev_model),
            "jev_api_key": _effective(payload.api_key, settings.jev_api_key),
        }
    )
    client = SystemOneClient(stub)
    try:
        answer = client.probe()
    except AppError as exc:
        raise AppError("JEV_FAILED", str(exc), details={"latency_ms": client.last_latency_ms}) from exc
    return ok(
        {
            "provider": "systemone",
            "url": stub.jev_systemone_url,
            "model": stub.jev_model,
            "answer": answer,
            "latency_ms": client.last_latency_ms,
        }
    )


# --------------------------------------------------------------------------- #
# Kunci API: dibuat dan dicabut dari layar Pengaturan
# --------------------------------------------------------------------------- #
def _env_hint(key: str) -> str:
    """Potongan kunci dari env untuk dikenali di daftar (lebih pendek dari mask biasa)."""
    if len(key) <= 8:
        return f"{'*' * len(key)} ({len(key)} karakter)"
    return f"{key[:4]}...{key[-2:]} ({len(key)} karakter)"


def _env_key_entries(settings) -> List[Dict[str, Any]]:
    """Kunci dari ``API_KEYS_JSON``: terlihat di daftar, tapi bukan milik layar ini."""
    entries: List[Dict[str, Any]] = []
    for key, ctx in settings.api_keys.items():
        entries.append(
            {
                "key_id": None,
                "label": "API_KEYS_JSON",
                "hint": _env_hint(key),
                "source": "env",
                "revocable": False,
                "state": "active",
                "organization_id": ctx.get("organization_id"),
                "user_id": ctx.get("user_id"),
                "application_id": ctx.get("application_id"),
                "permissions": [str(p) for p in (ctx.get("permissions") or [])],
                "created_at": None,
                "created_by": None,
                "expires_at": None,
                "last_used_at": None,
            }
        )
    return entries


def _tenant_fields(payload: ApiKeyCreateRequest, context: TrustedContext) -> Dict[str, str]:
    """Konteks kunci baru: warisi pemanggil, atau ditentukan pemanggil bila ia berizin ``*``."""
    inherited = {
        "organization_id": context.organization_id,
        "user_id": context.user_id,
        "application_id": context.application_id,
    }
    given = {
        field: str(getattr(payload, field)).strip()
        for field in ("organization_id", "user_id", "application_id")
        if str(getattr(payload, field) or "").strip()
    }
    if not given:
        return inherited
    if "*" not in context.permissions:
        raise AppError(
            "AUTH_FORBIDDEN",
            "Membuat kunci untuk tenant lain butuh izin '*' pada kunci yang dipakai sekarang",
            details={"fields": sorted(given), "organization_id": context.organization_id},
        )
    return {**inherited, **given}


@router.get("/settings/api-keys")
def list_api_keys(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Daftar kunci yang bisa memanggil layanan ini. Nilai kunci tidak pernah ikut."""
    _require_admin(context)
    settings = services_from_request(request).settings
    keys = _env_key_entries(settings) + registry_for(settings).public()
    keys.sort(key=lambda item: (str(item.get("source")) != "registry", str(item.get("label") or "").lower()))
    return ok(
        {
            "keys": keys,
            "active": len([item for item in keys if item.get("state") == "active"]),
            "max_active_keys": MAX_ACTIVE_KEYS,
            "allowed_permissions": list(ALLOWED_PERMISSIONS),
            "default_permissions": list(DEFAULT_PERMISSIONS),
            "context": {**context.as_dict(), "key_id": context.key_id, "source": context.source},
        }
    )


@router.post("/settings/api-keys")
def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Buat kunci baru. Nilai kunci dikembalikan **sekali** di sini dan tidak disimpan apa adanya."""
    _require_admin(context)
    settings = services_from_request(request).settings
    tenant = _tenant_fields(payload, context)
    key, entry = registry_for(settings).create(
        label=payload.label,
        permissions=payload.permissions,
        organization_id=tenant["organization_id"],
        user_id=tenant["user_id"],
        application_id=tenant["application_id"],
        created_by=f"{context.user_id}@{context.organization_id}",
        expires_in_days=payload.expires_in_days,
    )
    logger.info(
        "kunci api dibuat key_id=%s organization_id=%s oleh=%s",
        entry.get("key_id"),
        tenant["organization_id"],
        context.user_id,
    )
    return ok(
        {
            "key": key,
            "entry": entry,
            "note": "Kunci ini hanya ditampilkan sekali; simpan sebelum menutup layar.",
        }
    )


@router.delete("/settings/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Cabut kunci dari registry. Kunci dari env ditolak di sini (dikelola lewat API_KEYS_JSON)."""
    _require_admin(context)
    settings = services_from_request(request).settings
    if not str(key_id or "").startswith("key_"):
        raise AppError(
            "VALIDATION_ERROR",
            "Hanya kunci buatan layar ini yang bisa dicabut; kunci API_KEYS_JSON diubah lewat env",
            details={"key_id": key_id},
        )
    if context.key_id and str(key_id) == str(context.key_id):
        raise AppError(
            "AUTH_FORBIDDEN",
            "Kunci yang sedang dipakai tidak bisa mencabut dirinya sendiri; buat kunci pengganti, pakai kunci itu, lalu cabut yang lama",
            details={"key_id": key_id},
        )
    entry = registry_for(settings).revoke(key_id)
    logger.info("kunci api dicabut key_id=%s oleh=%s", key_id, context.user_id)
    return ok({"entry": entry})


# --------------------------------------------------------------------------- #
# Kode akses konsol + sesi peramban
# --------------------------------------------------------------------------- #
def _access_payload(settings, context: TrustedContext) -> Dict[str, Any]:
    """Status kode akses + daftar sesi aktif; tidak pernah memuat kode atau tokennya."""
    status = access_store(settings).status()
    lifetime_default = session_lifetime(settings, False)
    lifetime_remember = session_lifetime(settings, True)
    sessions = session_store(settings).public(status["generation"] or "")
    sessions.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "access": access_store(settings).public_status(),
        "sessions": sessions,
        "active_sessions": len([item for item in sessions if item["state"] == "active" and not item["stale_code"]]),
        "max_active_sessions": MAX_ACTIVE_SESSIONS,
        "default_lifetime": lifetime_default,
        "remember_lifetime": lifetime_remember,
        "min_code_length": MIN_CODE_LENGTH,
        "context": {**context.as_dict(), "key_id": context.key_id, "source": context.source,
                    "session_id": context.session_id},
    }


@router.get("/settings/access")
def read_access(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Apakah konsol terkunci, sesi mana yang aktif, dan berapa lama sesi bertahan."""
    _require_admin(context)
    settings = services_from_request(request).settings
    return ok(_access_payload(settings, context))


@router.put("/settings/access")
def set_access_code(
    payload: AccessCodeRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Pasang atau ganti kode akses konsol. Mengganti kode langsung mematikan semua sesi lama."""
    _require_admin(context)
    settings = services_from_request(request).settings
    store = access_store(settings)
    already_set = bool(store.status()["enabled"])

    if already_set and context.source == "session":
        if not payload.current_code or not store.verify(payload.current_code):
            raise AppError(
                "AUTH_FORBIDDEN",
                "Kode akses sekarang salah; masukkan kode lama untuk menggantinya",
                details={"field": "current_code"},
            )
    validate_code(payload.code)
    before = store.generation()
    store.set_code(payload.code, updated_by=f"{context.source}:{context.user_id}")
    revoked = 0
    if before and before != store.generation():
        # Sesi lama terikat versi kode: ganti kode = keluarkan semua sesi.
        revoked = session_store(settings).revoke_all(except_id=context.session_id or "")
    logger.info(
        "kode akses konsol %s oleh=%s; sesi dikeluarkan=%s",
        "diganti" if already_set else "dipasang",
        context.user_id,
        revoked,
    )
    return ok({**_access_payload(settings, context), "updated": True, "sessions_revoked": revoked, "status": store.public_status()})


@router.delete("/settings/access")
def clear_access_code(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Matikan kode akses: konsol kembali hanya bisa dibuka dengan API key."""
    _require_admin(context)
    settings = services_from_request(request).settings
    store = access_store(settings)
    if not store.status()["enabled"]:
        raise AppError("VALIDATION_ERROR", "Kode akses belum diatur")
    store.clear()
    revoked = session_store(settings).revoke_all(except_id=context.session_id or "")
    logger.info("kode akses konsol dihapus oleh=%s; sesi dikeluarkan=%s", context.user_id, revoked)
    return ok({**_access_payload(settings, context), "removed": True, "sessions_revoked": revoked})


@router.delete("/settings/access/sessions")
def revoke_all_sessions(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Keluarkan semua sesi konsol, termasuk yang sekarang (kecuali diminta menyisakan satu)."""
    _require_admin(context)
    settings = services_from_request(request).settings
    keep_current = bool(request.query_params.get("keep_current") in {"1", "true", "yes"})
    except_id = context.session_id if keep_current else ""
    revoked = session_store(settings).revoke_all(except_id=except_id or "")
    logger.info("sesi konsol dikeluarkan=%s oleh=%s", revoked, context.user_id)
    return ok({**_access_payload(settings, context), "sessions_revoked": revoked, "kept_current": keep_current})


@router.delete("/settings/access/sessions/{session_id}")
def revoke_one_session(
    session_id: str,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Keluarkan satu sesi tertentu (mis. perangkat yang hilang)."""
    _require_admin(context)
    settings = services_from_request(request).settings
    if not str(session_id or "").startswith("ses_"):
        raise AppError(
            "VALIDATION_ERROR",
            "Hanya sesi konsol yang bisa dikeluarkan di sini",
            details={"session_id": session_id},
        )
    entry = session_store(settings).revoke(session_id)
    logger.info("sesi %s dikeluarkan oleh=%s", session_id, context.user_id)
    return ok({"entry": entry, **_access_payload(settings, context)})
