"""Jev router — query intent -> capability (PRD 18, 19).

The route decision never carries tenant information: the trusted context is taken
from authentication and is passed to Jev read-only, purely as classification
signal, and any tenant field echoed back by Jev is stripped (see ``policies``).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.jev.policies import (
    RouteDecision,
    coerce_capability,
    heuristic_route,
    sanitize_route_payload,
)
from app.jev.systemone import SystemOneClient
from app.jev.tools import JevClient

logger = get_logger(__name__)


class JevRouter:
    """Two transports, one contract: today's route decision comes from either the
    jevai.org MCP tools or a native System One decision endpoint. Both are optional -
    every failure path degrades to the deterministic heuristic.
    """

    def __init__(
        self,
        settings: Settings,
        client: Optional[JevClient] = None,
        systemone: Optional[SystemOneClient] = None,
    ) -> None:
        self._settings = settings
        self._client = client or JevClient(settings)
        self._systemone = systemone or SystemOneClient(settings)
        self._last_status = "unknown"
        self._last_latency_ms: Optional[float] = None
        self._health_cache: Optional[tuple[float, str]] = None

    @property
    def client(self) -> JevClient:
        return self._client

    @property
    def systemone(self) -> SystemOneClient:
        return self._systemone

    @property
    def provider(self) -> str:
        return "systemone" if self._settings.jev_provider == "systemone" else "mcp"

    def reset_health_cache(self) -> None:
        """Called after a runtime settings change: the cached verdict may be stale."""
        self._health_cache = None
        self._last_status = "unknown"

    @property
    def status(self) -> str:
        return self._last_status

    # ------------------------------------------------------------------ #
    def route(self, query: str, *, tenant_context: Dict[str, Any]) -> RouteDecision:
        """Classify the query. Never blocks the request path on Jev being healthy."""
        if self.provider == "systemone":
            return self._route_systemone(query, tenant_context=tenant_context)
        if not self._client.enabled:
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_disabled"
            self._last_status = "disabled"
            return decision
        if not self._settings.jev_enabled:
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_off"
            self._last_status = "disabled"
            return decision

        # Only non-identifying, non-secret signals are exposed to Jev.
        arguments = {
            "task": query,
            "available_capabilities": list(
                [
                    "knowledge_query",
                    "knowledge_search",
                    "knowledge_extract",
                    "knowledge_summary",
                ]
            ),
            "context": {
                "application": tenant_context.get("application_id"),
                "surface": "rag_service",
                "has_tenant_context": True,
            },
        }
        started = time.perf_counter()
        try:
            result = self._client.call_tool(self._settings.jev_route_tool, arguments)
        except Exception as exc:  # noqa: BLE001 — degradation, not failure
            self._last_status = "error"
            self._last_latency_ms = round((time.perf_counter() - started) * 1000, 2)
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_error: {exc}"
            logger.warning("jev routing failed, using heuristic: %s", exc)
            return decision

        self._last_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        payload = self._client.structured_content(result)
        cleaned, stripped = sanitize_route_payload(payload)

        capability = (
            coerce_capability(cleaned.get("capability"))
            or coerce_capability(cleaned.get("route"))
            or coerce_capability(cleaned.get("intent"))
            or coerce_capability(cleaned.get("task_type"))
            or coerce_capability(cleaned.get("decision"))
        )
        if capability is None:
            self._last_status = "unusable"
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = "jev returned no usable capability"
            decision.raw = cleaned
            decision.stripped_fields = stripped
            return decision

        self._last_status = "ok"
        confidence = _as_float(cleaned.get("confidence"), default=0.7)
        reason = str(cleaned.get("reason") or cleaned.get("rationale") or "jev")[:280]
        return RouteDecision(
            capability=capability,
            source="jev",
            confidence=confidence,
            reason=reason,
            raw=cleaned,
            stripped_fields=stripped,
        )

    # ------------------------------------------------------------------ #
    def _route_systemone(self, query: str, *, tenant_context: Dict[str, Any]) -> RouteDecision:
        """One ``choice`` question about the request -> capability + calibrated confidence."""
        if not self._settings.jev_enabled:
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_off"
            self._last_status = "disabled"
            return decision
        if not self._systemone.enabled:
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_systemone_unconfigured"
            self._last_status = "disabled"
            return decision

        state = {
            "request": {"query": query, "surface": "rag_service"},
            "context": {
                "application": tenant_context.get("application_id"),
                "has_tenant_context": True,
            },
            "note": "Classify the request for a multi-tenant retrieval service.",
        }
        criteria = {
            "knowledge_query": "Answer a question using the tenant's indexed documents",
            "knowledge_search": "Return raw retrieval hits, no generated answer",
            "knowledge_summary": "Summarize a document or a knowledge base",
            "knowledge_extract": "Extract structured fields from documents",
        }
        try:
            answer = self._systemone.ask_choice(
                state,
                instructions="Which capability should serve this request?",
                criteria=criteria,
            )
        except Exception as exc:  # noqa: BLE001 - degradation, not failure
            self._last_status = "error"
            self._last_latency_ms = self._systemone.last_latency_ms
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"{decision.reason}; jev_error: {exc}"
            logger.warning("jev systemone routing failed, using heuristic: %s", exc)
            return decision

        self._last_latency_ms = self._systemone.last_latency_ms
        capability = coerce_capability(answer.choice)
        if capability is None:
            self._last_status = "unusable"
            decision = heuristic_route(query)
            decision.source = "fallback"
            decision.reason = f"jev returned no usable capability ({answer.choice!r})"
            decision.raw = answer.raw
            return decision

        self._last_status = "ok"
        runner_up = ""
        others = sorted(answer.probabilities.items(), key=lambda kv: kv[1], reverse=True)
        others = [item for item in others if item[0] != capability]
        if others:
            runner_up = f", next {others[0][0]}={others[0][1]:.2f}"
        return RouteDecision(
            capability=capability,
            source="jev",
            confidence=answer.confidence or (answer.probabilities.get(capability) or 0.7),
            reason=f"systemone choice, confidence={answer.confidence:.2f}{runner_up}",
            raw=answer.raw,
            stripped_fields=[],
        )

    # ------------------------------------------------------------------ #
    def health(self) -> str:
        if self.provider == "systemone":
            return self._health_systemone()
        if not self._client.enabled:
            return "disabled"
        try:
            self._client.call_tool(self._settings.jev_health_tool, {"probe": True})
            self._last_status = "ok"
            return "ok"
        except Exception as exc:  # noqa: BLE001
            self._last_status = "error"
            logger.warning("jev health check failed: %s", exc)
            return "error"

    # ------------------------------------------------------------------ #
    def _health_systemone(self) -> str:
        """Cached liveness probe: /ready is polled, and each probe costs tokens."""
        if not self._settings.jev_enabled:
            self._last_status = "disabled"
            return "disabled"
        if not self._systemone.enabled:
            self._last_status = "disabled"
            return "disabled"
        ttl = max(0, int(self._settings.jev_health_cache_seconds))
        now = time.time()
        if self._health_cache and now - self._health_cache[0] < ttl:
            self._last_status = self._health_cache[1]
            return self._health_cache[1]
        try:
            self._systemone.probe()
            status = "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("jev systemone health check failed: %s", exc)
            status = "error"
        self._last_status = status
        self._health_cache = (now, status)
        return status


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
