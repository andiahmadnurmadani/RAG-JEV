"""Jev over the native System One decision endpoint (POST /v1/systemone).

Wire format here is recorded from the live endpoint, so these tests pin the shape a
provider may change; the recorded bodies below are trimmed copies of real answers.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.jev.router import JevRouter
from app.jev.systemone import SystemOneClient

RECORDED_CHOICE = {
    "model": "jev-1.13-free",
    "answers": {
        "decision": {
            "type": "choice",
            "choice": "knowledge_query",
            "confidence": 0.93,
            "probabilities": {
                "knowledge_search": 0.03,
                "knowledge_summary": 0.0,
                "knowledge_query": 0.95,
                "knowledge_extract": 0.02,
            },
        }
    },
    "usage": {"input_tokens": 293, "output_tokens": 22},
    "cost": "0",
}

RECORDED_NOUL = {
    "model": "jev-1.13-free",
    "answers": {"reachable": {"type": "noul", "noul": 0.05}},
    "usage": {"input_tokens": 293, "output_tokens": 22},
    "cost": "0",
}

RECORDED_PROVIDER_ERROR = {
    "error": {
        "message": "[422]: Error from provider (Console): Upstream request failed: Endpoint is unavailable.",
        "type": "invalid_request_error",
        "code": "",
    }
}


class FakeResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class RecordingPost:
    """Stands in for ``httpx.post`` and remembers what the client sent."""

    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = body if body is not None else RECORDED_CHOICE
        self.calls: list[dict] = []

    def __call__(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(self.status_code, self.body)


@pytest.fixture()
def systemone_settings(settings):
    return settings.model_copy(
        update={
            "jev_enabled": True,
            "jev_provider": "systemone",
            "jev_systemone_url": "http://127.0.0.1:20128/v1/systemone",
            "jev_model": "oc/jev-1.13-free",
            "jev_api_key": "test-key-not-a-secret",
        }
    )


def _with_post(monkeypatch, recorder: RecordingPost) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", recorder)


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
def test_choice_answer_is_parsed_with_confidence_and_probabilities(systemone_settings, monkeypatch):
    recorder = RecordingPost()
    _with_post(monkeypatch, recorder)
    client = SystemOneClient(systemone_settings)

    answer = client.ask_choice(
        {"request": {"query": "berapa kuota cuti"}},
        instructions="Which capability should serve this request?",
        criteria={"knowledge_query": "answer", "knowledge_search": "hits"},
    )

    assert answer.choice == "knowledge_query"
    assert answer.confidence == pytest.approx(0.93)
    assert answer.probabilities["knowledge_query"] == pytest.approx(0.95)

    sent = recorder.calls[0]
    assert sent["url"] == "http://127.0.0.1:20128/v1/systemone"
    assert sent["json"]["model"] == "oc/jev-1.13-free"
    assert sent["json"]["state"] == {"request": {"query": "berapa kuota cuti"}}
    assert sent["json"]["questions"]["decision"]["type"] == "choice"
    assert sent["headers"]["Authorization"] == "Bearer test-key-not-a-secret"


def test_the_wire_format_is_not_openai_chat_completions(systemone_settings, monkeypatch):
    recorder = RecordingPost()
    _with_post(monkeypatch, recorder)
    SystemOneClient(systemone_settings).probe()

    payload = recorder.calls[0]["json"]
    assert "messages" not in payload and "temperature" not in payload
    assert set(payload) == {"state", "model", "questions"}
    assert payload["questions"]["reachable"]["type"] == "noul"


def test_score_questions_are_refused_before_the_request_is_sent(systemone_settings, monkeypatch):
    """The live lane answers 422 whenever a `score` question travels with the good ones."""
    recorder = RecordingPost()
    _with_post(monkeypatch, recorder)
    client = SystemOneClient(systemone_settings)

    with pytest.raises(AppError) as excinfo:
        client.decide(
            {"request": {"query": "x"}},
            {
                "decision": {"type": "choice", "instructions": "?", "criteria": {"a": "b"}},
                "confidence": {"type": "score", "instructions": "?", "min": 0, "max": 1},
            },
        )

    assert excinfo.value.code == "JEV_FAILED"
    assert "score" in str(excinfo.value)
    assert recorder.calls == []  # never reached the network


def test_provider_error_is_surfaced_with_its_message(systemone_settings, monkeypatch):
    _with_post(monkeypatch, RecordingPost(status_code=422, body=RECORDED_PROVIDER_ERROR))
    client = SystemOneClient(systemone_settings)

    with pytest.raises(AppError) as excinfo:
        client.probe()

    assert "Endpoint is unavailable" in str(excinfo.value)
    assert client.last_error and "422" in client.last_error


def test_empty_answers_is_an_error_not_a_silent_default(systemone_settings, monkeypatch):
    _with_post(monkeypatch, RecordingPost(body={"model": "x", "answers": {}}))
    client = SystemOneClient(systemone_settings)

    with pytest.raises(AppError):
        client.probe()
    assert client.last_error == "empty_answers"


def test_client_is_disabled_without_url_or_model(systemone_settings, monkeypatch):
    _with_post(monkeypatch, RecordingPost())
    for missing in ("jev_systemone_url", "jev_model"):
        client = SystemOneClient(systemone_settings.model_copy(update={missing: ""}))
        assert client.enabled is False
        with pytest.raises(AppError):
            client.probe()


def test_a_missing_key_fails_the_call_even_though_the_transport_looks_configured(systemone_settings, monkeypatch):
    """URL + model set but no key: same shape as the MCP path - enabled, then a hard error."""
    recorder = RecordingPost()
    _with_post(monkeypatch, recorder)
    client = SystemOneClient(systemone_settings.model_copy(update={"jev_api_key": ""}))

    assert client.enabled is True
    with pytest.raises(AppError) as excinfo:
        client.probe()
    assert "JEV_API_KEY" in str(excinfo.value)
    assert recorder.calls == []


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
class StubSystemOne:
    """Mimics SystemOneClient for the router, including the failure mode."""

    def __init__(
        self,
        choice="knowledge_query",
        confidence=0.93,
        boom: Exception | None = None,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.last_latency_ms = 812.5
        self.last_error = None
        self._choice = choice
        self._confidence = confidence
        self._boom = boom

    def ask_choice(self, state, *, instructions, criteria, question_id="decision"):
        from app.jev.systemone import ChoiceAnswer

        if self._boom:
            raise self._boom
        return ChoiceAnswer(
            choice=self._choice,
            confidence=self._confidence,
            probabilities={self._choice: 0.95, "knowledge_search": 0.03},
            raw={"type": "choice", "choice": self._choice, "confidence": self._confidence},
        )

    def probe(self):
        if self._boom:
            raise self._boom
        return {"type": "noul", "noul": 0.05}


def test_router_takes_the_capability_from_a_systemone_choice(systemone_settings):
    router = JevRouter(systemone_settings, systemone=StubSystemOne())

    decision = router.route("berapa kuota cuti tahunan", tenant_context={"application_id": "app_x"})

    assert decision.capability == "knowledge_query"
    assert decision.source == "jev"
    assert decision.confidence == pytest.approx(0.93)
    assert "confidence=0.93" in decision.reason
    assert router.status == "ok"


def test_router_falls_back_when_systemone_fails(systemone_settings):
    router = JevRouter(systemone_settings, systemone=StubSystemOne(boom=AppError("JEV_FAILED", "Endpoint is unavailable")))

    decision = router.route("cari dokumen cuti", tenant_context={})

    assert decision.source == "fallback"
    assert "jev_error" in decision.reason
    assert router.status == "error"


def test_router_reports_an_unconfigured_systemone_as_disabled(systemone_settings):
    router = JevRouter(systemone_settings, systemone=StubSystemOne(enabled=False))

    decision = router.route("apa isi SOP cuti", tenant_context={})

    assert decision.source == "fallback"
    assert "jev_systemone_unconfigured" in decision.reason
    assert router.status == "disabled"


def test_router_falls_back_when_the_choice_is_not_a_capability(systemone_settings):
    router = JevRouter(systemone_settings, systemone=StubSystemOne(choice="write_poem"))

    decision = router.route("apa isi SOP cuti", tenant_context={})

    assert decision.source == "fallback"
    assert router.status == "unusable"
    assert "no usable capability" in decision.reason


def test_health_probe_is_cached_so_polling_ready_does_not_burn_tokens(systemone_settings):
    class Counting(StubSystemOne):
        def __init__(self):
            super().__init__()
            self.probes = 0

        def probe(self):
            self.probes += 1
            return {"type": "noul", "noul": 0.05}

    counting = Counting()
    router = JevRouter(systemone_settings.model_copy(update={"jev_health_cache_seconds": 300}), systemone=counting)

    assert router.health() == "ok"
    assert router.health() == "ok"
    assert counting.probes == 1


def test_health_reports_error_when_the_probe_fails(systemone_settings):
    router = JevRouter(systemone_settings, systemone=StubSystemOne(boom=AppError("JEV_FAILED", "boom")))

    assert router.health() == "error"
