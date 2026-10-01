"""Baca/tulis JSON secara aman.

Dua hal yang diulang di beberapa berkas keadaan (settings, registry kunci):
tulis atomik supaya pembaca tidak pernah melihat berkas separuh, dan mode 0600
supaya berkas berisi rahasia tidak terbuka untuk pengguna lain.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()


def read_json(path: Path) -> Optional[Any]:
    """Isi berkas JSON, atau ``None`` bila tidak ada / tidak terbaca (bukan pengecualian)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover - berkas tidak terbaca memang masalah mesin
        logger.warning("berkas JSON tidak bisa dibaca (%s): %s", path, exc)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("berkas JSON tidak valid (%s): %s", path, exc)
        return None


def write_json_atomic(path: Path, data: Any, mode: int = 0o600) -> None:
    """Tulis JSON lewat tempfile di direktori yang sama, fsync, lalu ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with _LOCK:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", delete=False
        )
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, path)
        try:
            os.chmod(path, mode)
        except OSError:  # pragma: no cover - windows/acl edge
            pass


def file_stamp(path: Path) -> Tuple[Optional[int], Optional[int]]:
    """(mtime_ns, ukuran) untuk cache sederhana; ``(None, None)`` bila berkas tidak ada."""
    try:
        stat = path.stat()
    except OSError:
        return (None, None)
    return (stat.st_mtime_ns, stat.st_size)
