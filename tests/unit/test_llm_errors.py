"""Klasifikasi kegagalan LLM: operator harus melihat SEBAB, bukan kata 'error' polos."""

from app.rag.llm_errors import classify, rejects_param, summarize, wants_max_completion_tokens


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else str(body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("bukan JSON")


class HTTPStatusError(Exception):
    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


class ConnectError(Exception):
    pass


class ReadTimeout(Exception):
    pass


URL = "https://gateway.example/v1"


def _kind(exc):
    return classify(exc, base_url=URL, model="oc/x")["kind"]


def test_status_codes_map_to_readable_kinds():
    assert _kind(HTTPStatusError("x", _Resp(401, {"error": {"message": "bad key"}}))) == "auth"
    assert _kind(HTTPStatusError("x", _Resp(403, "forbidden"))) == "auth"
    assert _kind(HTTPStatusError("x", _Resp(404, "nope"))) == "model_not_found"
    assert _kind(HTTPStatusError("x", _Resp(429, "slow down"))) == "rate_limit"
    assert _kind(HTTPStatusError("x", _Resp(400, {"error": {"message": "Unsupported parameter"}}))) == "bad_request"
    assert _kind(HTTPStatusError("x", _Resp(502, "bad gateway"))) == "upstream"


def test_misleading_credit_message_is_rate_limit_even_on_400():
    # Beberapa gateway membalas 400 "insufficient credits" untuk parameter yang ditolak / kuota habis.
    err = HTTPStatusError("x", _Resp(400, {"error": {"message": "Insufficient credits"}}))
    assert _kind(err) == "rate_limit"


def test_model_missing_detected_from_400_body():
    err = HTTPStatusError("x", _Resp(400, {"error": {"message": "The model `zzz` does not exist"}}))
    assert _kind(err) == "model_not_found"


def test_network_errors():
    assert _kind(ConnectError("dns")) == "unreachable"
    assert _kind(ReadTimeout("slow")) == "timeout"
    assert _kind(KeyError("choices")) == "bad_response"


def test_upstream_message_is_kept_and_secrets_are_redacted():
    err = HTTPStatusError("x", _Resp(401, {"error": {"message": "Invalid key sk-abcdef1234567890 for Bearer abcdef123456"}}))
    info = classify(err, base_url=URL, model="oc/x")
    assert "sk-abcdef1234567890" not in info["upstream"]
    assert "abcdef123456" not in info["upstream"]
    assert info["host"] == "gateway.example"
    assert info["status"] == 401
    line = summarize(info)
    assert "HTTP 401" in line and "gateway.example" in line and "oc/x" in line


def test_parameter_hints_drive_adaptive_retry():
    err = HTTPStatusError(
        "x", _Resp(400, {"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
    )
    info = classify(err, base_url=URL, model="m")
    assert wants_max_completion_tokens(info)
    temp = classify(
        HTTPStatusError("x", _Resp(400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.1"}})),
        base_url=URL,
        model="m",
    )
    assert rejects_param(temp, "temperature") and not wants_max_completion_tokens(temp)
