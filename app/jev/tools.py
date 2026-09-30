"""Minimal JSON-RPC client for the Jev MCP endpoint (PRD 18).

Only ``tools/call`` is needed at runtime; the transport is plain HTTPS + bearer
token so the RAG service carries no MCP SDK dependency.

Failure policy: a Jev error is *never* fatal for retrieval. The router degrades to
the deterministic heuristic and reports ``source="heuristic"`` together with the
Jev status, so the omission is visible in the response and in ``/ready``.
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Dict, Optional

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

_request_counter = itertools.count(1)


class JevClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_error: Optional[str] = None

    # Read per call: the settings screen can repoint Jev on a running process.
    @property
    def url(self) -> str:
        return self._settings.jev_mcp_url

    @property
    def timeout(self) -> float:
        return self._settings.jev_timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url and self._settings.jev_enabled)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ------------------------------------------------------------------ #
    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.enabled:
            raise AppError("JEV_FAILED", "Jev integration is disabled")
        if not self._settings.jev_api_key:
            raise AppError("JEV_FAILED", "JEV_API_KEY is not configured")

        import httpx

        payload = {
            "jsonrpc": "2.0",
            "id": next(_request_counter),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._settings.jev_api_key}",
        }
        try:
            response = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"transport: {exc}"
            raise AppError("JEV_FAILED", f"Jev transport failed: {exc}") from exc

        if response.status_code >= 400:
            self._last_error = f"http_{response.status_code}"
            raise AppError("JEV_FAILED", f"Jev returned HTTP {response.status_code}")

        body = _decode(response)
        if body.get("error"):
            self._last_error = str(body["error"].get("message", "error"))
            raise AppError("JEV_FAILED", f"Jev error: {self._last_error}")

        result = body.get("result") or {}
        if result.get("isError"):
            message = _tool_error_text(result) or "unknown Jev error"
            self._last_error = message
            raise AppError("JEV_FAILED", f"Jev tool error: {message}")

        self._last_error = None
        return result

    # ------------------------------------------------------------------ #
    def structured_content(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(result.get("structuredContent"), dict):
            return dict(result["structuredContent"])
        text = _first_text(result)
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {"text": text}
        return {}


def _decode(response) -> Dict[str, Any]:
    """Accept ``application/json`` and ``text/event-stream`` responses alike."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                candidate = line[5:].strip()
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        return {}
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return {}


def _tool_error_text(result: Dict[str, Any]) -> str:
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]))
    return " ".join(parts)[:500]


def _first_text(result: Dict[str, Any]) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
    return ""
