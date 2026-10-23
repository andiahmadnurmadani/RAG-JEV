"""Log permintaan untuk operator: daftar, ringkasan asal permintaan, dan hapus."""

from __future__ import annotations

import time
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.routes.settings import _require_admin
from app.core.api_keys import registry_for
from app.core.errors import AppError, ok

router = APIRouter(tags=["logs"])

StatusClass = Literal["2xx", "3xx", "4xx", "5xx", "429", "errors"]

# Lebar batang grafik per rentang waktu: kira-kira 60 batang di setiap rentang.
_BUCKETS = ((15, 15), (60, 60), (360, 300), (1440, 1800), (10080, 3600 * 3))


def _bucket_for(minutes: int) -> int:
    for limit, seconds in _BUCKETS:
        if minutes <= limit:
            return seconds
    return 3600 * 6


def _key_labels(settings: Any) -> Dict[str, str]:
    """ID kunci -> label buatan operator ("KMS BUMD"), supaya asal permintaan mudah dikenali."""
    try:
        return {
            str(item.get("key_id")): str(item.get("label") or "")
            for item in registry_for(settings).public()
            if item.get("key_id")
        }
    except Exception:  # noqa: BLE001 - label hanya pelengkap tampilan
        return {}


def _store(request: Request):
    services = services_from_request(request)
    store = getattr(services, "request_log", None)
    if store is None:
        raise AppError("INTERNAL_ERROR", "log permintaan tidak tersedia")
    return services, store


@router.get("/logs/requests")
def list_requests(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    minutes: int = Query(default=60, ge=1, le=60 * 24 * 31),
    status: Optional[StatusClass] = None,
    q: str = Query(default="", max_length=200),
    client_ip: Optional[str] = Query(default=None, max_length=100),
    key_id: Optional[str] = Query(default=None, max_length=100),
    application_id: Optional[str] = Query(default=None, max_length=200),
    path: Optional[str] = Query(default=None, max_length=300),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    summary: bool = True,
) -> Dict[str, Any]:
    """Permintaan terbaru lebih dulu, plus ringkasan (asal teratas, jalur teratas, per waktu)."""
    services, store = _store(request)
    _require_admin(context, services.settings)
    store.flush(timeout=1.0)
    since = time.time() - minutes * 60
    filters = dict(
        status=status,
        search=q,
        client_ip=client_ip or None,
        key_id=key_id or None,
        application_id=application_id or None,
        path=path or None,
    )
    data = store.list(since=since, limit=limit, offset=offset, **filters)
    labels = _key_labels(services.settings)
    for item in data["items"]:
        item["key_label"] = labels.get(item.get("key_id") or "", "")
    if summary:
        overview = store.summary(since=since, bucket_seconds=_bucket_for(minutes), **filters)
        for source in overview["sources"]:
            source["key_label"] = labels.get(source.get("key_id") or "", "")
        data["summary"] = overview
    settings = services.settings
    data["recording"] = {
        "enabled": bool(settings.request_log_enabled),
        "retention_days": int(settings.request_log_retention_days or 0),
        "max_entries": int(settings.request_log_max_entries or 0),
        "skip_paths": [item.strip() for item in str(settings.request_log_skip_paths or "").split(",") if item.strip()],
    }
    data["minutes"] = minutes
    data["now"] = time.time()
    return ok(data)


@router.delete("/logs/requests")
def clear_requests(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    confirm: str = Query(default="", max_length=20),
) -> Dict[str, Any]:
    """Kosongkan log permintaan. Wajib ``?confirm=hapus``."""
    services, store = _store(request)
    _require_admin(context, services.settings)
    if confirm != "hapus":
        raise AppError("VALIDATION_ERROR", "kirim ?confirm=hapus untuk mengosongkan log permintaan")
    store.flush(timeout=1.0)
    return ok({"deleted": store.clear()})
