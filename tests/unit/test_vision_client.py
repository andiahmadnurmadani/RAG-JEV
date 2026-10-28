"""Visi: kunci LLM tidak bocor ke endpoint lain, dan fallback format JSON saat endpoint menolaknya."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.rag.generator import LLMUsage
from app.rag.vision import VisionImage, VisionService, _VisionSettings


def _settings(**values: Any) -> Settings:
    base = dict(llm_provider="openai_compatible", llm_base_url="https://llm.example.com/v1", llm_api_key="llm-key", llm_model="text-model")
    base.update(values)
    return Settings(**base)


def test_vision_reuses_the_llm_endpoint_key_and_model_when_empty():
    view = _VisionSettings(_settings())
    assert (view.llm_base_url, view.llm_api_key, view.llm_model) == ("https://llm.example.com/v1", "llm-key", "text-model")


def test_llm_key_is_not_sent_to_a_different_vision_endpoint():
    view = _VisionSettings(_settings(vision_base_url="https://other.example.com/v1", vision_model="vl"))
    assert view.llm_api_key == ""
    assert view.llm_model == "vl"
    own = _VisionSettings(_settings(vision_base_url="https://other.example.com/v1", vision_api_key="vision-key"))
    assert own.llm_api_key == "vision-key"


class _Client:
    """Menolak response_format tertentu dengan galat seperti LLMClient."""

    def __init__(self, reject: List[str], reply: str = '{"labels": ["2023"], "values": [12.4]}', upstream: str = "bad response_format") -> None:
        self.reject = reject
        self.reply = reply
        self.upstream = upstream
        self.calls: List[Dict[str, Any]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None, response_format=None, timeout=None):
        self.calls.append({"messages": messages, "response_format": response_format})
        kind = (response_format or {}).get("type", "none")
        if kind in self.reject:
            raise AppError("LLM_FAILED", "ditolak", details={"llm": {"status": 400, "upstream": self.upstream, "model": "m"}})
        return self.reply, LLMUsage(input_tokens=10, output_tokens=5, model="vl-model")


def _run(service: VisionService, client: _Client, response_format):
    service._client = lambda: client  # type: ignore[method-assign]
    image = VisionImage(data=b"\x89PNG\r\n\x1a\n", mime="image/png")
    return service.run(images=[image], query="baca", response_format=response_format)


def test_json_schema_falls_back_to_json_object_then_to_plain_prompt():
    client = _Client(reject=["json_schema", "json_object"])
    result = _run(VisionService(_settings()), client, {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}})
    assert [call["response_format"] and call["response_format"]["type"] for call in client.calls] == ["json_schema", "json_object", None]
    assert result.parsed == {"labels": ["2023"], "values": [12.4]}
    assert result.model == "vl-model"


def test_image_is_sent_as_openai_image_url_part():
    client = _Client(reject=[])
    _run(VisionService(_settings()), client, None)
    parts = client.calls[0]["messages"][1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[-1]["type"] == "text"


def test_prose_around_json_is_repaired_or_rejected():
    client = _Client(reject=[], reply='Berikut hasilnya: {"a": 1} semoga membantu')
    result = _run(VisionService(_settings()), client, {"type": "json_object"})
    assert result.answer == '{"a": 1}'
    broken = _Client(reject=[], reply="tidak ada json di sini")
    with pytest.raises(AppError):
        _run(VisionService(_settings()), broken, {"type": "json_object"})


def test_a_text_only_model_is_reported_as_such():
    client = _Client(reject=["none"], upstream="this model does not support image input")
    with pytest.raises(AppError) as caught:
        _run(VisionService(_settings()), client, None)
    assert "tidak menerima gambar" in caught.value.message
