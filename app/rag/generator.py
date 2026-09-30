"""Answer generation and extraction (PRD 8.1, 12, 16, 17, 26, 35).

The LLM is an OpenAI-compatible chat endpoint (``LLM_BASE_URL``), which covers the
PRD's local Qwen3 4B server (vLLM/Ollama) as well as a hosted gateway — no code
change between them.

Anti-hallucination is enforced twice:

1. structurally — ``strict_grounding`` refuses to call the LLM at all when the
   retrieval stage produced no candidate above threshold (PRD 16);
2. by prompt — the system prompt forbids outside knowledge and requires citations
   (PRD 17), and states that retrieved documents are untrusted data (PRD 35).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.rag.context import UNTRUSTED_NOTICE, BuiltContext

logger = get_logger(__name__)

NO_ANSWER_ID = "Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia."
NO_ANSWER_EN = "The requested information was not found in the available knowledge base."

NOT_FOUND_MARKERS = (
    "tidak ditemukan",
    "tidak terdapat",
    "informasi tersebut tidak",
    "not found",
    "no information",
    "tidak cukup informasi",
    "tidak ada informasi",
)

SYSTEM_PROMPT = """You are an organizational knowledge assistant.

Answer only using the provided context.

Rules:
1. Do not invent information.
2. If the context does not contain enough information, say that the information was not found.
3. Do not use knowledge from other organizations.
4. Cite the source for factual claims.
5. Preserve numbers, dates, names, and policies accurately.
6. Do not expose internal metadata unless requested and permitted.

{untrusted_notice}
Cite sources with the bracketed context number, for example [1] or [2][3].
Answer in the same language as the user's question.""".format(untrusted_notice=UNTRUSTED_NOTICE)

EXTRACT_SYSTEM_PROMPT = (
    """You extract structured data from organizational documents.

Return ONLY a JSON object that satisfies the requested JSON schema.
Use only the provided context. Never invent values that are not present.
If the context lacks the requested data, return an empty result.

"""
    + UNTRUSTED_NOTICE
)


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""


@dataclass
class GeneratedAnswer:
    answer: str
    grounded: bool
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: str = ""
    citations_used: List[int] = field(default_factory=list)


class MockLLMClient:
    """Deterministic stand-in for tests and offline demos (``LLM_PROVIDER=mock``).

    It answers strictly from the retrieved context: every sentence comes from the
    context block, so grounded/ungrounded behaviour can be tested without a model.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model = "mock-llm"

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> tuple[str, LLMUsage]:
        user = "\n".join(message.get("content", "") for message in messages if message.get("role") == "user")
        started = time.perf_counter()
        if response_format and response_format.get("type") == "json_object" and "Extract the requested data" in user:
            content = json.dumps(self._mock_items(user), ensure_ascii=False)
        else:
            content = self._mock_answer(user)
        usage = LLMUsage(
            input_tokens=max(1, len(user.split())),
            output_tokens=max(1, len(content.split())),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            model=self.model,
        )
        return content, usage

    @staticmethod
    def _context_lines(user: str) -> List[str]:
        inside = False
        lines: List[str] = []
        for line in user.splitlines():
            if line.startswith("<<<RETRIEVED_CONTEXT"):
                inside = True
                continue
            if line.startswith("RETRIEVED_CONTEXT>>>"):
                break
            if inside:
                lines.append(line)
        return lines

    def _mock_answer(self, user: str) -> str:
        lines = self._context_lines(user)
        text = " ".join(line for line in lines if line and not line.startswith(("[", "[DOCUMENT]", "[CONTENT]", "[SECTION]", "</")))
        snippet = text.strip()[:400]
        if not snippet:
            return NO_ANSWER_ID
        return f"Berdasarkan konteks [1]: {snippet}"

    def _mock_items(self, user: str) -> Dict[str, Any]:
        lines = [line for line in self._context_lines(user) if "|" in line or line.strip().startswith("-")]
        items = []
        for line in lines[:5]:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 2:
                items.append({"row": parts[0], "value": parts[1]})
        return {"items": items, "not_found": not items}

    def health(self) -> str:
        return "ok"


class LLMClient:
    """Thin OpenAI-compatible chat client with explicit failure mapping."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        # Read per call: the settings screen can change the model of a running process.
        return self._settings.llm_model

    def chat(
        self,
        messages: Sequence[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> tuple[str, LLMUsage]:
        import httpx

        settings = self._settings
        url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": settings.llm_temperature if temperature is None else temperature,
            "max_tokens": settings.llm_max_tokens if max_tokens is None else max_tokens,
        }
        if response_format:
            body["response_format"] = response_format

        started = time.perf_counter()
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=timeout or settings.llm_timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AppError("LLM_FAILED", f"LLM request failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("LLM_FAILED", "LLM response did not contain a completion") from exc

        usage_block = payload.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(usage_block.get("prompt_tokens") or 0),
            output_tokens=int(usage_block.get("completion_tokens") or 0),
            latency_ms=round(latency_ms, 2),
            model=payload.get("model") or self.model,
        )
        return text.strip(), usage

    def health(self) -> str:
        try:
            self.chat([{"role": "user", "content": "ping"}], max_tokens=4, temperature=0.0, timeout=30)
            return "ok"
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm health check failed: %s", exc)
            return "error"


def build_llm_client(settings: Settings):
    """``LLM_PROVIDER=mock`` keeps the whole pipeline runnable with zero models."""
    if settings.llm_provider == "mock":
        return MockLLMClient(settings)
    return LLMClient(settings)


class Generator:
    def __init__(self, settings: Settings, client: Optional[LLMClient] = None) -> None:
        self._settings = settings
        self._client = client or build_llm_client(settings)

    @property
    def client(self):
        return self._client

    def rebind_llm_client(self, client) -> None:
        """Swap the client after a runtime settings change (mock <-> real provider)."""
        self._client = client

    @property
    def model(self) -> str:
        return self._client.model

    # ------------------------------------------------------------------ #
    def answer(
        self,
        *,
        query: str,
        context: BuiltContext,
        strict_grounding: bool = True,
        no_candidate_reason: Optional[str] = None,
    ) -> GeneratedAnswer:
        if not context.used:
            return GeneratedAnswer(
                answer=NO_ANSWER_ID if self._settings.answer_language == "id" else NO_ANSWER_EN,
                grounded=False,
                usage=LLMUsage(model=self.model),
            )

        user_prompt = f"{context.text}\n\nUser query:\n{query}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        text, usage = self._client.chat(messages)
        grounded = not self._looks_like_refusal(text)
        if strict_grounding and not grounded:
            logger.info("strict grounding: model reported insufficient context (%s)", no_candidate_reason or "low_relevance")
        return GeneratedAnswer(
            answer=text,
            grounded=grounded,
            usage=usage,
            raw=text,
            citations_used=extract_citation_numbers(text),
        )

    # ------------------------------------------------------------------ #
    def extract(
        self,
        *,
        query: str,
        context: BuiltContext,
        output_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not context.used:
            return {"items": [], "not_found": True}

        instruction = (
            "Extract the requested data from the context.\n"
            f"JSON schema to satisfy:\n{json.dumps(output_schema or {'type': 'object'}, ensure_ascii=False)}\n\n"
            f"User request:\n{query}"
        )
        messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": f"{context.text}\n\n{instruction}"},
        ]
        text, usage = self._client.chat(
            messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        payload = _loads_tolerant(text)
        if payload is None:
            # one repair attempt: ask for strict JSON only
            repair, _ = self._client.chat(
                [
                    {"role": "system", "content": "Return valid JSON only. No prose, no markdown fences."},
                    {"role": "user", "content": f"Convert this into valid JSON:\n{text}"},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            payload = _loads_tolerant(repair) or {"items": [], "not_found": True}
        if isinstance(payload, list):
            payload = {"items": payload}
        payload.setdefault("usage", {})
        payload["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "model": usage.model,
        }
        return payload

    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_refusal(text: str) -> bool:
        if not text:
            return True
        lowered = text.lower()
        if any(marker in lowered for marker in NOT_FOUND_MARKERS):
            # a refusal that also answers is still a grounded answer
            return len(lowered) < 400
        return False

    def health(self) -> str:
        return self._client.health()


def extract_citation_numbers(text: str) -> List[int]:
    return sorted({int(match) for match in re.findall(r"\[(\d{1,2})\]", text or "")})


def _loads_tolerant(text: str) -> Optional[Any]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
