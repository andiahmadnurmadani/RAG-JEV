"""Masuk ke konsol dengan satu kode akses, lalu bertukar jadi sesi.

Alur yang dilayani di sini:

1. ``GET /auth/gate`` — layar masuk perlu tahu apakah konsol ini memang terkunci (tanpa kredensial);
2. ``POST /auth/login`` — kode akses ditukar dengan token sesi; "ingat saya" memperpanjang jadi
   ``UI_REMEMBER_DAYS`` hari (bawaan 7);
3. ``GET/DELETE /auth/session`` — siapa yang sedang masuk, dan keluar (mengakhiri sesi ini).

Token sesi diterima di header yang sama dengan API key (``Authorization: Bearer ...``), jadi
seluruh layanan tidak perlu tahu apakah pemanggilnya memakai kunci atau sesi.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.core import access
from app.core.errors import AppError, ok

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    """Kode akses + pilihan "ingat saya". Tidak ada field tenant: konteks dibentuk server."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=256)
    remember: bool = False


def _client(request: Request) -> str:
    """Alamat klien untuk audit dan pembatasan percobaan (di belakang proxy: X-Forwarded-For)."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


@router.get("/auth/gate")
def read_gate(request: Request) -> Dict[str, Any]:
    """Apakah konsol terkunci dan berapa lama sesi berlaku. Tidak membocorkan kode."""
    settings = services_from_request(request).settings
    status = access.access_store(settings).status()
    default = access.session_lifetime(settings, False)
    remember = access.session_lifetime(settings, True)
    # ``api_key_only``: konsol tidak memakai gerbang kode akses - cukup tempel API key, dan
    # kunci itu membuka seluruh layar Pengaturan. Gerbang yang tidak perlu membuat operator
    # mengira konsol terkunci padahal tidak.
    return ok(
        {
            "enabled": bool(status["enabled"]) and not settings.console_api_key_only,
            "code_set": bool(status["enabled"]),
            "api_key_only": bool(settings.console_api_key_only),
            "hint": status["hint"] if status["enabled"] else None,
            "set_at": status["set_at"] if status["enabled"] else None,
            "default_lifetime": default,
            "remember_lifetime": remember,
            "min_code_length": access.MIN_CODE_LENGTH,
            "active_sessions": access.session_store(settings).active_count(status["generation"] or ""),
        }
    )


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request) -> Dict[str, Any]:
    """Tukar kode akses dengan sesi. Kode salah ditolak sebelum sesi apa pun dibuat."""
    settings = services_from_request(request).settings
    store = access.access_store(settings)
    status = store.status()
    if not status["enabled"]:
        raise AppError(
            "AUTH_INVALID",
            "Konsol ini tidak memakai kode akses; isi API key di panel Koneksi",
            details={"gate_enabled": False},
        )

    client = _client(request)
    blocked = access.THROTTLE.blocked_for(client)
    if blocked > 0:
        raise AppError(
            "RATE_LIMITED",
            f"Terlalu banyak percobaan masuk. Coba lagi dalam {int(blocked) + 1} detik",
            details={"retry_after_seconds": int(blocked) + 1},
        )

    if not store.verify(payload.code):
        access.THROTTLE.register_failure(client)
        raise AppError(
            "AUTH_INVALID",
            "Kode akses salah",
            details={"attempts_left": access.THROTTLE.remaining_attempts(client)},
        )

    access.THROTTLE.reset(client)
    token, entry = access.session_store(settings).create(
        remember=payload.remember,
        generation=store.generation(),
        label=_client(request),
        client=client,
        hours=getattr(settings, "ui_session_hours", 12),
        days=getattr(settings, "ui_remember_days", 7),
    )
    return ok(
        {
            "token": token,
            "session": entry,
            "expires_at": entry.get("expires_at"),
            "remember": bool(payload.remember),
            "note": "Simpan token ini di peramban; kode akses tetap tidak pernah dikirim ulang.",
        }
    )


@router.get("/auth/session")
def read_session(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Siapa yang sedang masuk: kunci API, sesi konsol, atau token KMS."""
    settings = services_from_request(request).settings
    payload: Dict[str, Any] = {
        "context": {**context.as_dict(), "key_id": context.key_id, "source": context.source},
        "source": context.source,
        "session": None,
        "gate": access.access_store(settings).public_status(),
    }
    if context.source == "session" and context.session_id:
        for item in access.session_store(settings).public():
            if item.get("session_id") == context.session_id:
                payload["session"] = item
                break
    return ok(payload)


@router.delete("/auth/session")
def logout(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Keluar: sesi yang sedang dipakai dicabut. Kunci API tidak bisa 'keluar' di sini."""
    settings = services_from_request(request).settings
    if context.source != "session" or not context.session_id:
        raise AppError(
            "VALIDATION_ERROR",
            "Kredensial ini bukan sesi konsol, jadi tidak ada yang bisa dikeluarkan",
            details={"source": context.source},
        )
    entry = access.session_store(settings).revoke(context.session_id)
    return ok({"session": entry, "revoked": True})


@router.post("/auth/logout")
def logout_post(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    """Sama seperti ``DELETE /auth/session``; disediakan untuk klien yang hanya bisa POST."""
    return logout(request, context)
