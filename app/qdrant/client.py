"""Qdrant connection factory (PRD 8.4).

Two deployment modes, one code path:

* ``QDRANT_URL`` set  -> real Qdrant server (docker-compose / production).
* ``QDRANT_URL`` empty -> embedded local mode (``path=``) for dev machines that
  have no container runtime. Same client API, same filters, same isolation.

Thread-safety
-------------
The embedded client (``path=``) keeps its collection in memory and is **not** thread-safe:
two threads writing at once corrupt its internal arrays, and the next read fails with e.g.
``operands could not be broadcast together with shapes (5432,) (5431,)`` - a permanently
broken collection, not a transient error. That is exactly what happened once summaries
started running on a second worker pool while indexing kept writing.

So in local mode every call is serialised through a re-entrant lock (``SerialisedClient``).
The server client is already thread-safe and is returned as-is. Local mode is a
development/single-user convenience; for real concurrency point ``QDRANT_URL`` at a server.
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


class SerialisedClient:
    """Delegate to a Qdrant client, running every call under one re-entrant lock.

    Only used for the embedded client, which is not thread-safe. The lock is re-entrant
    because a client call may re-enter through a helper that performs two calls.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.RLock()

    def __getattr__(self, name: str):
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def serialised(*args, **kwargs):
            with self._lock:
                return attribute(*args, **kwargs)

        return serialised

    # Explicit, so close() never deadlocks during shutdown.
    def close(self, *args, **kwargs):
        with self._lock:
            return self._client.close(*args, **kwargs)


def build_client(settings: Settings):
    if settings.qdrant_url:
        return QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=settings.qdrant_timeout,
        )
    # Embedded local mode: serialise access (see module docstring).
    return SerialisedClient(QdrantClient(path=settings.qdrant_local_path))


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
