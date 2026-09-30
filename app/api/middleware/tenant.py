"""Tenant guard (PRD 6, 13, 19, 34).

Every entry point funnels through here, so "whose data is this?" has exactly one
answer per request.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.api.middleware.auth import TrustedContext
from app.core.errors import AppError
from app.core.security import reject_client_tenant_fields

# Retrieval/query bodies must not carry tenant fields at all.
QUERY_ENDPOINTS = ("/query", "/search", "/extract")


def guard_query_body(body: Any, where: str) -> None:
    """PRD 6: ``organization_id`` can never be supplied by the client prompt."""
    reject_client_tenant_fields(body, where)


def assert_tenant_match(context: TrustedContext, claimed: Optional[str], *, where: str) -> str:
    """KMS may echo its organization_id; it must equal the trusted context."""
    if not claimed:
        return context.organization_id
    if str(claimed) != context.organization_id:
        raise AppError(
            "AUTH_FORBIDDEN",
            "organization_id does not match the trusted tenant context",
            details={"location": where},
        )
    return context.organization_id


def retrieval_scope(context: TrustedContext, knowledge_base_id: Optional[str]) -> Dict[str, Any]:
    """The only filter set any retrieval call may use."""
    if not context.organization_id:
        raise AppError("TENANT_CONTEXT_MISSING", "organization_id missing from the trusted context")
    scope: Dict[str, Any] = {"organization_id": context.organization_id}
    if knowledge_base_id:
        scope["knowledge_base_id"] = knowledge_base_id
    return scope
