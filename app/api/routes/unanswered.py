"""Pertanyaan yang tidak terjawab: lihat, tandai selesai, beri catatan, hapus."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.core.errors import AppError, ok

router = APIRouter(tags=["unanswered"])

KNOWN_REASONS = (
    "no_candidates",
    "below_threshold",
    "strict_grounding",
    "context_empty",
    "answer_truncated",
    "table_plan_invalid",
)


class UnansweredUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: List[int] = Field(min_length=1, max_length=500)
    status: Optional[Literal["open", "resolved"]] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class UnansweredDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: List[int] = Field(min_length=1, max_length=500)


@router.get("/unanswered")
def list_unanswered(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    knowledge_base_id: Optional[str] = Query(default=None, max_length=200),
    status: Optional[Literal["open", "resolved"]] = None,
    q: str = Query(default="", max_length=200),
    sort: Literal["recent", "frequent"] = "recent",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> Dict[str, Any]:
    """Pertanyaan yang berakhir "tidak ditemukan", terbaru atau tersering lebih dulu."""
    context.require("read")
    if knowledge_base_id:
        context.require_knowledge_base(knowledge_base_id)
    services = services_from_request(request)
    data = services.unanswered.list(
        organization_id=context.organization_id,
        knowledge_base_id=knowledge_base_id,
        allowed=context.knowledge_base_ids,
        status=status,
        search=q,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    data["recording"] = {
        "enabled": bool(services.settings.unanswered_enabled),
        "reasons": [item for item in str(services.settings.unanswered_reasons or "").split(",") if item],
        "retention_days": int(services.settings.unanswered_retention_days or 0),
        "max_entries": int(services.settings.unanswered_max_entries or 0),
    }
    return ok(data)


@router.patch("/unanswered")
def update_unanswered(
    payload: UnansweredUpdate,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    context.require("write")
    if payload.status is None and payload.note is None:
        raise AppError("VALIDATION_ERROR", "isi status atau note")
    services = services_from_request(request)
    updated = services.unanswered.update(
        organization_id=context.organization_id,
        ids=payload.ids,
        allowed=context.knowledge_base_ids,
        status=payload.status,
        note=payload.note,
    )
    return ok({"updated": updated})


@router.post("/unanswered/delete")
def delete_unanswered(
    payload: UnansweredDelete,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
) -> Dict[str, Any]:
    context.require("write")
    services = services_from_request(request)
    deleted = services.unanswered.delete(
        organization_id=context.organization_id, ids=payload.ids, allowed=context.knowledge_base_ids
    )
    return ok({"deleted": deleted})


@router.delete("/unanswered")
def clear_unanswered(
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    confirm: str = Query(default="", max_length=20),
    knowledge_base_id: Optional[str] = Query(default=None, max_length=200),
    status: Optional[Literal["open", "resolved"]] = None,
) -> Dict[str, Any]:
    """Hapus semua yang cocok dengan filter. Wajib ``?confirm=hapus``."""
    context.require("write")
    if confirm != "hapus":
        raise AppError("VALIDATION_ERROR", "kirim ?confirm=hapus untuk menghapus semua yang cocok dengan filter")
    if knowledge_base_id:
        context.require_knowledge_base(knowledge_base_id)
    services = services_from_request(request)
    deleted = services.unanswered.delete_matching(
        organization_id=context.organization_id,
        knowledge_base_id=knowledge_base_id,
        allowed=context.knowledge_base_ids,
        status=status,
    )
    return ok({"deleted": deleted})
