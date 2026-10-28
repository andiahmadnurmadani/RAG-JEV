"""POST /vision - membaca gambar (grafik, tabel, diagram, halaman pindaian) dengan model visi."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from app.api.deps import services_from_request
from app.api.middleware.auth import TrustedContext, trusted_context
from app.api.middleware.ratelimit import QuerySlot, limits_for, vision_slot
from app.api.schemas import VisionDataOut, VisionRequest, VisionUsageOut
from app.core.errors import ok
from app.core.logging import get_logger
from app.core.request_log import annotate
from app.rag.vision import VisionService, decode_images

logger = get_logger(__name__)
router = APIRouter(tags=["vision"])


@router.post("/vision")
def vision(
    payload: VisionRequest,
    request: Request,
    context: TrustedContext = Depends(trusted_context),
    slot: QuerySlot = Depends(vision_slot),
) -> Dict[str, Any]:
    """Gambar + instruksi -> teks, atau JSON yang dijamin sah bila ``response_format`` meminta JSON.

    Tidak membaca knowledge dan tidak menyimpan gambar: cukup izin ``read`` pada kunci API.
    """
    settings = services_from_request(request).settings
    annotate(request, detail=payload.query)
    limits_for(settings).check(context.limit_identity)
    context.require("read")
    images = decode_images([image.model_dump() for image in payload.images], settings)
    slot.acquire(context.limit_identity)

    result = VisionService(settings).run(
        images=images,
        query=payload.query,
        response_format=payload.response_format.model_dump(exclude_none=True) if payload.response_format else None,
        max_tokens=payload.max_tokens,
    )
    data = VisionDataOut(
        answer=result.answer,
        json_data=result.parsed,
        model=result.model,
        usage=VisionUsageOut(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            latency_ms=result.usage.latency_ms,
            finish_reason=result.usage.finish_reason,
        ),
        images=result.images,
    )
    logger.info(
        "vision served org=%s images=%d model=%s tokens=%d/%d",
        context.organization_id, len(images), result.model, result.usage.input_tokens, result.usage.output_tokens,
    )
    return ok(data.model_dump(by_alias=True))
