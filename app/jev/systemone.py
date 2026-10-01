"""Native System One (Jev) decision client - ``POST /v1/systemone`` (PRD 18).

Jev is not a chat model. It answers *typed questions* about a *state* and returns
calibrated probabilities; there is no ``messages`` array and no ``temperature``.
Wire format, recorded from the live endpoint:

    POST {"state": <str|obj|list>, "model": "<id>",
          "questions": {"<id>": {"type": "choice", "instructions": "...",
                                 "criteria": {"<key>": "<meaning>", ...}}}}
    200  {"model": "jev-1.13-free",
          "answers": {"<id>": {"type": "choice", "choice": "<key>",
                               "confidence": 0.93,
                               "probabilities": {"<key>": 0.95, ...}}},
          "usage": {"input_tokens": 293, "output_tokens": 22}, "cost": "0"}

Question types are ``choice``, ``noul`` (probability that a statement holds) and
``score``. Measured on the ``oc/jev-1.13-free`` lane: ``choice`` and ``noul`` answer
in ~0.8-1.2 s, while *any* request carrying a ``score`` question answers
``422 Endpoint is unavailable`` - so this client never sends ``score`` questions and
sends one question per call (the choice answer already carries its own confidence).

Failure policy matches the MCP path: a Jev error never breaks retrieval. It raises
``AppError("JEV_FAILED", ...)`` and the router degrades to the deterministic heuristic
with ``source="fallback"``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

_SUPPORTED_QUESTION_TYPES = ("choice", "noul")


class ChoiceAnswer:
    """One answered ``choice`` question."""

    __slots__ = ("choice", "confidence", "probabilities", "raw")

    def __init__(self, choice: Optional[str], confidence: float, probabilities: Dict[str, float], raw: Dict[str, Any]) -> None:
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"ChoiceAnswer(choice={self.choice!r}, confidence={self.confidence})"


class SystemOneClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._last_error: Optional[str] = None
        self._last_latency_ms: Optional[float] = None
        self._last_usage: Dict[str, Any] = {}

    # Read per call: the settings screen can repoint Jev on a running process.
    @property
    def url(self) -> str:
        return self._settings.jev_systemone_url

    @property
    def model(self) -> str:
        return self._settings.jev_model

    @property
    def timeout(self) -> float:
        return self._settings.jev_timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.model and self._settings.jev_enabled)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def last_latency_ms(self) -> Optional[float]:
        return self._last_latency_ms

    # ------------------------------------------------------------------ #
    def decide(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Ask Jev a set of typed questions about ``state``; return the ``answers`` map."""
        if not self.enabled:
            raise AppError("JEV_FAILED", "Jev System One is not configured (JEV_SYSTEMONE_URL/JEV_MODEL)")
        if not self._settings.jev_api_key:
            raise AppError("JEV_FAILED", "JEV_API_KEY is not configured")
        unsupported = [
            f"{key}:{q.get('type')}" for key, q in questions.items() if q.get("type") not in _SUPPORTED_QUESTION_TYPES
        ]
        if unsupported:
            # Fail loudly in code, not at runtime: the endpoint rejects whole requests
            # (422) when an unsupported question type travels with the good ones.
            raise AppError(
                "JEV_FAILED",
                f"unsupported System One question types: {unsupported} (only choice/noul answer on this lane)",
            )

        import httpx

        payload = {"state": state, "model": self.model, "questions": questions}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.jev_api_key}",
        }
        started = time.perf_counter()
        try:
            response = httpx.post(self.url, json=payload, headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - transport failure is a degradation
            self._last_error = f"transport: {exc}"
            self._last_latency_ms = round((time.perf_counter() - started) * 1000, 2)
            raise AppError("JEV_FAILED", f"Jev System One transport failed: {exc}") from exc
        self._last_latency_ms = round((time.perf_counter() - started) * 1000, 2)

        body = _safe_json(response)
        if response.status_code >= 400:
            message = _error_message(body) or f"HTTP {response.status_code}"
            self._last_error = f"http_{response.status_code}: {message}"
            raise AppError("JEV_FAILED", f"Jev System One rejected the request: {message}")
        if body.get("error"):
            message = _error_message(body)
            self._last_error = message
            raise AppError("JEV_FAILED", f"Jev System One error: {message}")

        answers = body.get("answers")
        if not isinstance(answers, dict) or not answers:
            self._last_error = "empty_answers"
            raise AppError("JEV_FAILED", "Jev System One returned no answers")

        self._last_error = None
        self._last_usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return answers

    # ------------------------------------------------------------------ #
    def ask_choice(
        self,
        state: Any,
        *,
        instructions: str,
        criteria: Dict[str, str],
        question_id: str = "decision",
    ) -> ChoiceAnswer:
        answers = self.decide(
            state,
            {question_id: {"type": "choice", "instructions": instructions, "criteria": criteria}},
        )
        answer = answers.get(question_id) or {}
        if not isinstance(answer, dict):
            raise AppError("JEV_FAILED", "Jev System One returned a malformed answer")
        probabilities: Dict[str, float] = {}
        raw_probabilities = answer.get("probabilities")
        if isinstance(raw_probabilities, dict):
            for key, value in raw_probabilities.items():
                number = _as_float(value)
                if number is not None:
                    probabilities[str(key)] = number
        choice = answer.get("choice")
        return ChoiceAnswer(
            choice=str(choice) if choice is not None else None,
            confidence=_as_float(answer.get("confidence")) or 0.0,
            probabilities=probabilities,
            raw=dict(answer),
        )

    # ------------------------------------------------------------------ #
    def probe(self, questions: Optional[List[str]] = None) -> Dict[str, Any]:
        """Cheapest liveness check: one ``noul`` question on a tiny state."""
        answers = self.decide(
            {"probe": "liveness", "surface": "rag_service"},
            {"reachable": {"type": "noul", "instructions": "The service is reachable and answering."}},
        )
        return answers.get("reachable") or {}


def _safe_json(response) -> Dict[str, Any]:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _error_message(body: Dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "error")[:300]
    if isinstance(error, str):
        return error[:300]
    return ""


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
