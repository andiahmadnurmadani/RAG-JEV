"""OpenCode Go: header sesi/UA wajib, model minimax lewat /messages, galat terbaca."""

import pytest

from app.core.errors import AppError
from app.rag import generator as generator_module


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://opencode.ai/zen/go/v1/x")
            raise httpx.HTTPStatusError("err", request=request, response=httpx.Response(self.status_code, request=request, json=self._body))


OPENAI_OK = _Resp(200, {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}], "usage": {}, "model": "m"})
ANTHROPIC_OK = _Resp(
    200,
    {"content": [{"type": "text", "text": "halo"}], "usage": {"input_tokens": 3, "output_tokens": 2}, "stop_reason": "end_turn", "model": "minimax-m3"},
)


@pytest.fixture
def calls(monkeypatch):
    import httpx

    seen = []
    queue = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append((url, dict(json or {}), dict(headers or {})))
        return queue.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen, queue


def _client(settings, model, base="https://opencode.ai/zen/go/v1", style="auto"):
    settings.llm_base_url = base
    settings.llm_model = model
    settings.llm_api_key = "sk-test1234567890"
    settings.llm_api_style = style
    return generator_module.LLMClient(settings)


def test_go_sends_stable_session_and_own_user_agent(settings, calls):
    seen, queue = calls
    queue.extend([OPENAI_OK, OPENAI_OK])
    client = _client(settings, "glm-5.2")
    client.chat([{"role": "user", "content": "a"}])
    client.chat([{"role": "user", "content": "b"}])
    url, _body, headers = seen[0]
    assert url.endswith("/zen/go/v1/chat/completions")
    assert headers["x-opencode-session"] == seen[1][2]["x-opencode-session"]
    assert headers["User-Agent"].startswith("rag-jev")


def test_other_hosts_get_no_session_header(settings, calls):
    seen, queue = calls
    queue.append(OPENAI_OK)
    _client(settings, "m", base="https://gateway.example/v1").chat([{"role": "user", "content": "a"}])
    assert "x-opencode-session" not in seen[0][2]


def test_minimax_uses_messages_endpoint(settings, calls):
    seen, queue = calls
    queue.append(ANTHROPIC_OK)
    text, usage = _client(settings, "minimax-m3").chat(
        [{"role": "system", "content": "aturan"}, {"role": "user", "content": "x"}]
    )
    url, body, headers = seen[0]
    assert url.endswith("/zen/go/v1/messages")
    assert headers["x-api-key"] == "sk-test1234567890" and headers["anthropic-version"]
    assert body["system"] == "aturan" and body["messages"] == [{"role": "user", "content": "x"}]
    assert text == "halo" and usage.input_tokens == 3 and usage.finish_reason == "stop"


def test_forced_style_overrides_auto(settings, calls):
    seen, queue = calls
    queue.append(ANTHROPIC_OK)
    _client(settings, "glm-5.2", style="anthropic").chat([{"role": "user", "content": "x"}])
    assert seen[0][0].endswith("/messages")


def test_missing_session_error_is_readable(settings, calls):
    _seen, queue = calls
    queue.append(_Resp(400, {"error": {"message": "Request is missing x-opencode-session"}}))
    with pytest.raises(AppError) as info:
        _client(settings, "glm-5.2").chat([{"role": "user", "content": "x"}])
    assert "x-opencode-session" in info.value.message
    assert info.value.details["llm"]["kind"] == "bad_request"
