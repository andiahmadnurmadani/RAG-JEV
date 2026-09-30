"""Authentication + trusted tenant context (PRD 6, 19, 21, 34).

Two accepted credential shapes, both resolving to the *same* trusted context:

1. ``Authorization: Bearer <api key>`` registered in ``API_KEYS_JSON``;
2. a KMS-issued HS256 context token (``X-Tenant-Context`` or the bearer token when
   ``REQUIRE_TENANT_CONTEXT_TOKEN=true``).

The context is the only source of ``organization_id``. Request bodies are scanned
for tenant fields and rejected before any work happens.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, Request

from app.api.deps import services_from_request
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import resolve_api_key, verify_context_token
from app.core.tenant import TrustedContext

__all__ = ["TrustedContext", "resolve_context", "trusted_context", "resolve_api_key"]


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


def resolve_context(
    settings: Settings,
    authorization: Optional[str],
    tenant_context_header: Optional[str],
) -> TrustedContext:
    token = _bearer_token(authorization)
    context_token = tenant_context_header or (token if settings.require_tenant_context_token else None)

    if context_token:
        payload = verify_context_token(settings, context_token)
        return TrustedContext(
            user_id=str(payload["user_id"]),
            organization_id=str(payload["organization_id"]),
            application_id=str(payload["application_id"]),
            permissions=[str(p) for p in payload.get("permissions") or []],
            source="kms_token",
        )

    if not token:
        raise AppError("AUTH_INVALID", "Missing credentials: send Authorization: Bearer <API key>")
    context = resolve_api_key(settings, token)
    return TrustedContext(
        user_id=str(context["user_id"]),
        organization_id=str(context["organization_id"]),
        application_id=str(context["application_id"]),
        permissions=[str(p) for p in context.get("permissions") or []],
        source="api_key",
    )


async def trusted_context(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_tenant_context: Optional[str] = Header(default=None, alias="X-Tenant-Context"),
) -> TrustedContext:
    """FastAPI dependency — resolves and caches the trusted tenant context."""
    settings = services_from_request(request).settings
    context = resolve_context(settings, authorization, x_tenant_context)
    request.state.tenant_context = context
    return context
