"""Error taxonomy + the single error envelope used by every route (PRD 29)."""

from __future__ import annotations

from typing import Any, Dict, Optional

# PRD section 29 — minimum error code set.
ERROR_CODES = {
    "AUTH_INVALID": 401,
    "AUTH_FORBIDDEN": 403,
    "TENANT_CONTEXT_MISSING": 401,
    "KNOWLEDGE_NOT_FOUND": 404,
    "DOCUMENT_NOT_FOUND": 404,
    "INDEXING_FAILED": 500,
    "EMBEDDING_FAILED": 500,
    "RETRIEVAL_FAILED": 500,
    "RERANK_FAILED": 500,
    "LLM_FAILED": 502,
    "JEV_FAILED": 502,
    "VALIDATION_ERROR": 422,
    "PAYLOAD_TOO_LARGE": 413,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
}


class AppError(Exception):
    """Domain error carrying a stable code from PRD 29."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        if code not in ERROR_CODES:
            code = "INTERNAL_ERROR"
        self.code = code
        self.message = message
        self.status_code = status_code or ERROR_CODES[code]
        self.details = details or {}

    def envelope(self, request_id: str) -> Dict[str, Any]:
        error: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
        }
        if self.details:
            error["details"] = self.details
        return {"success": False, "error": error}


def ok(data: Dict[str, Any]) -> Dict[str, Any]:
    """Success envelope (PRD 22-28)."""
    return {"success": True, "data": data}
