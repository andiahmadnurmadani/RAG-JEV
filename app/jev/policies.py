"""Jev capability policy (PRD 18, 19).

Two invariants are enforced here, not in the prompt:

* Jev decides **what** to do (capability) — never **whose** data may be read.
  Any ``organization_id``/``tenant``/``user_id`` field arriving from Jev is dropped.
* Unknown or missing capabilities fall back to ``knowledge_query`` (fail closed),
  so a broken/hostile router response can never widen access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "knowledge_query": {
        "description": "Answer a question from organizational knowledge with citations.",
        "endpoint": "/api/v1/query",
        "rerank": True,
        "generate": True,
    },
    "knowledge_search": {
        "description": "Return relevant passages without generating an answer.",
        "endpoint": "/api/v1/search",
        "rerank": True,
        "generate": False,
    },
    "knowledge_extract": {
        "description": "Extract structured data from organizational knowledge.",
        "endpoint": "/api/v1/extract",
        "rerank": True,
        "generate": True,
    },
    "knowledge_summary": {
        "description": "Summarise the retrieved knowledge for the user.",
        "endpoint": "/api/v1/query",
        "rerank": True,
        "generate": True,
    },
}

DEFAULT_CAPABILITY = "knowledge_query"

# Fields Jev is never allowed to influence.
FORBIDDEN_ROUTE_FIELDS = (
    "organization_id",
    "org_id",
    "tenant",
    "tenant_id",
    "user_id",
    "application_id",
    "permissions",
    "api_key",
    "token",
)


@dataclass
class RouteDecision:
    capability: str
    source: str  # jev | heuristic | fallback
    confidence: float = 0.0
    reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    stripped_fields: List[str] = field(default_factory=list)

    @property
    def uses_reranker(self) -> bool:
        return bool(CAPABILITIES.get(self.capability, CAPABILITIES[DEFAULT_CAPABILITY])["rerank"])

    @property
    def generates_answer(self) -> bool:
        return bool(CAPABILITIES.get(self.capability, CAPABILITIES[DEFAULT_CAPABILITY])["generate"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "source": self.source,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
            "stripped_tenant_fields": self.stripped_fields,
        }


def sanitize_route_payload(payload: Optional[Dict[str, Any]]) -> tuple[Dict[str, Any], List[str]]:
    """Remove anything tenant-shaped from a Jev response (PRD 19)."""
    if not isinstance(payload, dict):
        return {}, []
    cleaned: Dict[str, Any] = {}
    stripped: List[str] = []
    for key, value in payload.items():
        if key.lower() in FORBIDDEN_ROUTE_FIELDS:
            stripped.append(key)
            continue
        cleaned[key] = value
    return cleaned, stripped


def coerce_capability(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "query": "knowledge_query",
        "rag": "knowledge_query",
        "ask": "knowledge_query",
        "search": "knowledge_search",
        "retrieve": "knowledge_search",
        "extract": "knowledge_extract",
        "structured": "knowledge_extract",
        "summary": "knowledge_summary",
        "summarize": "knowledge_summary",
        "summarise": "knowledge_summary",
        "knowledge_search": "knowledge_search",
        "knowledge_extract": "knowledge_extract",
        "knowledge_summary": "knowledge_summary",
        "knowledge_query": "knowledge_query",
    }
    canonical = aliases.get(candidate, candidate)
    return canonical if canonical in CAPABILITIES else None


def heuristic_route(query: str) -> RouteDecision:
    """Deterministic fallback used when Jev is unavailable (PRD 18 fallback path)."""
    lowered = (query or "").lower()
    extract_markers = ("ambil data", "ekstrak", "extract", "tabel data", "json", "daftar data", "rekap data")
    summary_markers = ("ringkas", "ringkasan", "rangkum", "summary", "summarize", "ikhtisar")
    search_markers = ("cari", "temukan pasal", "di mana disebut", "sebutkan halaman", "list", "find")

    if any(marker in lowered for marker in extract_markers):
        return RouteDecision(capability="knowledge_extract", source="heuristic", confidence=0.5, reason="keyword:extract")
    if any(marker in lowered for marker in summary_markers):
        return RouteDecision(capability="knowledge_summary", source="heuristic", confidence=0.5, reason="keyword:summary")
    if any(marker in lowered for marker in search_markers):
        return RouteDecision(capability="knowledge_search", source="heuristic", confidence=0.45, reason="keyword:search")
    return RouteDecision(capability=DEFAULT_CAPABILITY, source="heuristic", confidence=0.4, reason="default")
