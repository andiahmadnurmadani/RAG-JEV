"""Terjemahkan kegagalan panggilan LLM menjadi sebab yang bisa dibaca operator.

Sebelumnya semua kegagalan dilaporkan sebagai ``error`` polos: operator tidak tahu apakah kuncinya
salah, modelnya tidak ada, gateway-nya mati, atau parameternya ditolak. Modul ini sengaja tidak
mengimpor httpx (dikenali lewat nama kelas dan atribut ``response``) supaya mudah diuji.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

_SECRET_RE = re.compile(r"(Bearer\s+|sk-|key[=:]\s*)[A-Za-z0-9_.\-]{6,}", re.IGNORECASE)
_SNIPPET_CHARS = 300

# kind -> label sangat pendek untuk lencana status ("Model AI: kunci ditolak")
SHORT: Dict[str, str] = {
    "unreachable": "tidak terjangkau",
    "timeout": "timeout",
    "auth": "kunci ditolak",
    "model_not_found": "model tidak ada",
    "rate_limit": "kuota/batas laju",
    "bad_request": "permintaan ditolak",
    "upstream": "gateway error",
    "bad_response": "balasan salah format",
    "unknown": "gagal",
}

# kind -> (judul singkat untuk UI, saran perbaikan)
KINDS: Dict[str, tuple[str, str]] = {
    "unreachable": (
        "Endpoint LLM tidak terjangkau",
        "Periksa LLM_BASE_URL, apakah gateway hidup, dan apakah server RAG bisa menjangkaunya (DNS/firewall).",
    ),
    "timeout": (
        "Endpoint LLM terlalu lama menjawab",
        "Gateway/model lambat atau antre. Coba model lain yang lebih cepat atau naikkan LLM_TIMEOUT.",
    ),
    "auth": (
        "Kunci API ditolak endpoint LLM",
        "Isi ulang LLM_API_KEY di Pengaturan > Model AI. Kunci kedaluwarsa, salah, atau tidak punya akses ke model ini.",
    ),
    "model_not_found": (
        "Model tidak ditemukan di endpoint",
        "Nama model tidak ada di gateway. Pakai 'Muat daftar model' di Pengaturan lalu pilih salah satu.",
    ),
    "rate_limit": (
        "Kuota atau batas laju model terlampaui",
        "Model gratis/berlangganan sedang dibatasi. Tunggu, atau ganti ke model lain.",
    ),
    "bad_request": (
        "Endpoint menolak isi permintaan",
        "Model/gateway tidak menerima parameter yang dikirim. Lihat pesan dari endpoint di bawah.",
    ),
    "upstream": (
        "Gateway atau penyedia model sedang bermasalah",
        "Galat sisi server (5xx). Biasanya sementara; coba lagi atau ganti model.",
    ),
    "bad_response": (
        "Balasan endpoint bukan format OpenAI",
        "Endpoint menjawab, tetapi bukan JSON chat/completions. Pastikan LLM_BASE_URL berakhir di /v1.",
    ),
    "unknown": (
        "Panggilan LLM gagal",
        "Lihat pesan teknis di bawah.",
    ),
}


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", text or "")


def host_of(url: str) -> str:
    parts = urlsplit(url or "")
    return parts.netloc or (url or "")


def _upstream_message(response: Any) -> str:
    """Pesan galat dari badan balasan endpoint (OpenAI: {"error": {"message": ...}})."""
    if response is None:
        return ""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error.get("error")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
        if data.get("message") or data.get("detail"):
            return str(data.get("message") or data.get("detail"))
    try:
        return str(response.text or "")
    except Exception:  # noqa: BLE001
        return ""


def classify(exc: BaseException, *, base_url: str = "", model: str = "") -> Dict[str, Any]:
    """Kembalikan {kind, title, hint, status, upstream, message, host, model}."""
    response = getattr(exc, "response", None)
    status: Optional[int] = getattr(response, "status_code", None)
    name = type(exc).__name__
    upstream = redact(_upstream_message(response)).strip().replace("\n", " ")[:_SNIPPET_CHARS]
    low = upstream.lower()

    if status is not None:
        if status in (401, 403):
            kind = "auth"
        elif status == 404 or (status in (400, 422) and "model" in low and any(
                word in low for word in ("not found", "does not exist", "unknown", "no such", "tidak ada", "not supported"))):
            kind = "model_not_found"
        elif status == 429 or any(word in low for word in ("quota", "rate limit", "insufficient credit", "too many requests")):
            kind = "rate_limit"
        elif status in (400, 422):
            kind = "bad_request"
        elif status >= 500:
            kind = "upstream"
        else:
            kind = "unknown"
    elif "Timeout" in name:
        kind = "timeout"
    elif any(word in name for word in ("Connect", "Network", "Proxy", "Protocol", "Read", "Remote")) or isinstance(
            exc, (ConnectionError, OSError)):
        kind = "unreachable"
    elif isinstance(exc, (KeyError, IndexError, TypeError, ValueError)) or "JSON" in name:
        kind = "bad_response"
    else:
        kind = "unknown"

    title, hint = KINDS[kind]
    raw = redact(str(exc)).strip().replace("\n", " ")[:_SNIPPET_CHARS]
    return {
        "kind": kind,
        "short": SHORT[kind],
        "title": title,
        "hint": hint,
        "status": status,
        "upstream": upstream or None,
        "message": raw or name,
        "host": host_of(base_url),
        "model": model,
    }


def summarize(info: Dict[str, Any]) -> str:
    """Satu kalimat untuk pesan AppError / log."""
    parts = [info["title"]]
    if info.get("status"):
        parts.append(f"HTTP {info['status']}")
    where = " ".join(p for p in (info.get("host"), info.get("model")) if p)
    if where:
        parts.append(f"({where})")
    if info.get("upstream"):
        parts.append(f"- kata endpoint: {info['upstream']}")
    return " ".join(parts)


def wants_max_completion_tokens(info: Dict[str, Any]) -> bool:
    low = (info.get("upstream") or "").lower()
    return info.get("kind") == "bad_request" and "max_tokens" in low and "max_completion_tokens" in low


def rejects_param(info: Dict[str, Any], name: str) -> bool:
    low = (info.get("upstream") or "").lower()
    return info.get("kind") == "bad_request" and name in low
