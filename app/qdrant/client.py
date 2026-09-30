"""Qdrant connection factory (PRD 8.4).

Two deployment modes, one code path:

* ``QDRANT_URL`` set  -> real Qdrant server (docker-compose / production).
* ``QDRANT_URL`` empty -> embedded local mode (``path=``) for dev machines that
  have no container runtime. Same client API, same filters, same isolation.
"""

from __future__ import annotations

import threading
from typing import Optional

from qdrant_client import QdrantClient

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_client = None
_client_signature: Optional[str] = None


def build_client(settings: Settings):
    if settings.qdrant_url:
        return QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=settings.qdrant_timeout,
        )
    return QdrantClient(path=settings.qdrant_local_path)


def get_client(settings: Settings):
    """Process-wide singleton; the signature guard allows tests to swap config."""
    global _client, _client_signature
    signature = f"{settings.qdrant_url}|{settings.qdrant_local_path}|{bool(settings.qdrant_api_key)}"
    with _lock:
        if _client is None or _client_signature != signature:
            if _client is not None:
                try:
                    _client.close()
                except Exception:  # noqa: BLE001
                    pass
            _client = build_client(settings)
            _client_signature = signature
            mode = "server" if settings.qdrant_url else "embedded-local"
            logger.info("connected to qdrant (%s)", mode)
        return _client


def reset_client() -> None:
    global _client, _client_signature
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None
        _client_signature = None


def health(settings: Settings) -> str:
    try:
        get_client(settings).get_collections()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("qdrant health check failed: %s", exc)
        return "error"
