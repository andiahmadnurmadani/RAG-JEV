"""Registry kunci API yang dibuat dari layar Pengaturan.

``API_KEYS_JSON`` (env) tetap sumber kebenaran untuk kunci yang ditanam saat deploy; berkas
ini menambahkan kunci yang bisa dibuat dan dicabut operator tanpa redeploy. Keduanya
bertemu di :func:`app.core.security.resolve_api_key`, jadi seluruh layanan tidak perlu tahu
dari mana sebuah kunci berasal.

Aturan yang dipegang:

* **Yang disimpan adalah hash.** Berkas hanya memuat ``sha256`` kunci + awalan pendek untuk
  ditampilkan. Bocornya berkas tidak langsung memberi kunci yang bisa dipakai.
* **Konteks tenant diwarisi dari pembuatnya** kecuali pembuat punya izin ``*`` (lihat rute).
* **Dicabut, bukan dihapus.** Jejak siapa membuat dan kapan dipakai terakhir tetap ada.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.errors import AppError
from app.core.jsonfile import file_stamp, read_json, write_json_atomic
from app.core.logging import get_logger

logger = get_logger(__name__)

KEY_PREFIX = "rag_"
DEFAULT_PERMISSIONS: Tuple[str, ...] = ("read", "write")
ALLOWED_PERMISSIONS: Tuple[str, ...] = ("read", "write", "admin", "*")
MAX_ACTIVE_KEYS = 200
MAX_EXPIRY_DAYS = 3650
# last_used_at ditulis paling sering sekali semenit per kunci: cukup untuk audit, tanpa
# menulis berkas pada setiap permintaan.
_TOUCH_INTERVAL_SECONDS = 60.0

_VISIBLE_FIELDS = (
    "key_id",
    "label",
    "hint",
    "organization_id",
    "user_id",
    "application_id",
    "permissions",
    "created_at",
    "created_by",
    "expires_at",
    "last_used_at",
    "revoked_at",
)


def generate_key() -> str:
    """Kunci baru: awalan yang bisa dikenali mata + 32 byte acak (CSPRNG)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def hint_for(key: str) -> str:
    """Potongan aman untuk daftar: cukup untuk mengenali, tidak untuk dipakai."""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:12]}...{key[-4:]}"


def _now() -> float:
    return time.time()


def _iso(timestamp: Optional[float] = None) -> str:
    moment = datetime.fromtimestamp(_now() if timestamp is None else timestamp, tz=timezone.utc)
    return moment.replace(microsecond=0).isoformat()


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ApiKeyRegistry:
    """Daftar kunci dinamis di satu berkas JSON (isi = hash, bukan kunci)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: Tuple[Optional[int], Optional[int]] = (None, None)
        self._records: List[Dict[str, Any]] = []
        self._touched_at: Dict[str, float] = {}

    # ---------------------------------------------------------------- berkas
    def _load(self, force: bool = False) -> List[Dict[str, Any]]:
        """Muat ulang bila berkas berubah (mtime/ukuran), supaya proses lain juga terbaca."""
        stamp = file_stamp(self.path)
        if force or stamp != self._stamp:
            data = read_json(self.path)
            raw = data.get("keys") if isinstance(data, dict) else None
            self._records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            self._stamp = stamp
        return self._records

    def _save(self) -> None:
        write_json_atomic(self.path, {"version": 1, "keys": self._records})
        self._stamp = file_stamp(self.path)

    # ----------------------------------------------------------------- baca
    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._load()]

    def public(self) -> List[Dict[str, Any]]:
        """Bentuk aman untuk UI: tanpa ``digest``, tanpa kunci penuh."""
        with self._lock:
            return [self._public(record) for record in self._load()]

    def _public(self, record: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {field: record.get(field) for field in _VISIBLE_FIELDS}
        out["permissions"] = [str(p) for p in (record.get("permissions") or [])]
        out["knowledge_base_ids"] = [str(k) for k in (record.get("knowledge_base_ids") or [])]
        out["source"] = "registry"
        out["revocable"] = not bool(record.get("revoked_at"))
        out["state"] = self._state(record)
        return out

    def _state(self, record: Dict[str, Any]) -> str:
        if record.get("revoked_at"):
            return "revoked"
        if self._is_expired(record):
            return "expired"
        return "active"

    @staticmethod
    def _is_expired(record: Dict[str, Any]) -> bool:
        expires = _parse_iso(record.get("expires_at"))
        return bool(expires and expires <= datetime.now(timezone.utc))

    # ------------------------------------------------------------- pencocokan
    def resolve(self, presented: str) -> Optional[Dict[str, Any]]:
        """Konteks tepercaya untuk kunci ini, atau ``None`` bila tidak ada/kedaluwarsa/dicabut."""
        if not presented:
            return None
        wanted = fingerprint(presented)
        with self._lock:
            records = self._load()
            for record in records:
                digest = str(record.get("digest") or "")
                if not digest or record.get("revoked_at") or self._is_expired(record):
                    continue
                if hmac.compare_digest(digest, wanted):
                    self._touch(record)
                    return {
                        "user_id": str(record.get("user_id") or ""),
                        "organization_id": str(record.get("organization_id") or ""),
                        "application_id": str(record.get("application_id") or ""),
                        "permissions": [str(p) for p in (record.get("permissions") or [])],
                        "knowledge_base_ids": [str(k) for k in (record.get("knowledge_base_ids") or [])],
                        "key_id": record.get("key_id"),
                        "source": "registry",
                    }
        return None

    def _touch(self, record: Dict[str, Any]) -> None:
        """Catat pemakaian terakhir, tanpa menulis berkas tiap permintaan (dipanggil di dalam lock)."""
        key_id = str(record.get("key_id") or "")
        if not key_id:
            return
        now = _now()
        if now - self._touched_at.get(key_id, 0.0) < _TOUCH_INTERVAL_SECONDS:
            return
        self._touched_at[key_id] = now
        record["last_used_at"] = _iso(now)
        try:
            self._save()
        except OSError as exc:  # pragma: no cover - izin/disk penuh
            logger.warning("gagal menyimpan last_used_at kunci %s: %s", key_id, exc)

    # ----------------------------------------------------------------- tulis
    def create(
        self,
        *,
        label: str,
        permissions: Optional[List[str]],
        organization_id: str,
        user_id: str,
        application_id: str,
        created_by: str,
        expires_in_days: Optional[int] = None,
        knowledge_base_ids: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Buat kunci baru. Mengembalikan (kunci penuh, entri publik) - kunci tampil sekali saja."""
        clean_label = (label or "").strip()
        if not clean_label:
            raise AppError("VALIDATION_ERROR", "Label kunci wajib diisi")
        if len(clean_label) > 64:
            raise AppError("VALIDATION_ERROR", "Label kunci maksimal 64 karakter", details={"length": len(clean_label)})

        granted = [str(p).strip() for p in (list(DEFAULT_PERMISSIONS) if permissions is None else permissions) if str(p).strip()]
        if not granted:
            raise AppError("VALIDATION_ERROR", "Pilih minimal satu izin", details={"allowed": list(ALLOWED_PERMISSIONS)})
        unknown = sorted({p for p in granted if p not in ALLOWED_PERMISSIONS})
        if unknown:
            raise AppError(
                "VALIDATION_ERROR",
                "Izin tidak dikenal: " + ", ".join(unknown),
                details={"unknown": unknown, "allowed": list(ALLOWED_PERMISSIONS)},
            )
        for field, value in (("organization_id", organization_id), ("user_id", user_id), ("application_id", application_id)):
            if not str(value or "").strip():
                raise AppError("VALIDATION_ERROR", f"'{field}' tidak boleh kosong", details={"field": field})

        bound = sorted({str(item).strip() for item in (knowledge_base_ids or []) if str(item).strip()})
        if len(bound) > 50 or any(len(item) > 200 for item in bound):
            raise AppError(
                "VALIDATION_ERROR",
                "knowledge_base_ids maksimal 50 item, masing-masing maksimal 200 karakter",
            )

        expires_at = None
        if expires_in_days is not None:
            try:
                days = int(expires_in_days)
            except (TypeError, ValueError) as exc:
                raise AppError("VALIDATION_ERROR", "Masa berlaku harus berupa angka hari") from exc
            if not 1 <= days <= MAX_EXPIRY_DAYS:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"Masa berlaku harus antara 1 dan {MAX_EXPIRY_DAYS} hari",
                    details={"expires_in_days": days},
                )
            expires_at = _iso((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())

        with self._lock:
            records = self._load(force=True)
            active = [item for item in records if not item.get("revoked_at")]
            if len(active) >= MAX_ACTIVE_KEYS:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"Sudah ada {MAX_ACTIVE_KEYS} kunci aktif; cabut yang tidak dipakai dulu",
                    details={"max_active_keys": MAX_ACTIVE_KEYS},
                )
            key = generate_key()
            record: Dict[str, Any] = {
                "key_id": "key_" + secrets.token_hex(4),
                "label": clean_label,
                "hint": hint_for(key),
                "digest": fingerprint(key),
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "application_id": str(application_id),
                "permissions": granted,
                "knowledge_base_ids": bound,
                "created_at": _iso(),
                "created_by": str(created_by or ""),
                "expires_at": expires_at,
                "last_used_at": None,
                "revoked_at": None,
            }
            records.append(record)
            self._records = records
            try:
                self._save()
            except OSError as exc:
                records.pop()
                raise AppError("INTERNAL_ERROR", f"Gagal menyimpan registry kunci: {exc}") from exc
            return key, self._public(record)

    def revoke(self, key_id: str) -> Dict[str, Any]:
        wanted = str(key_id or "").strip()
        with self._lock:
            for record in self._load(force=True):
                if record.get("key_id") != wanted:
                    continue
                if not record.get("revoked_at"):
                    record["revoked_at"] = _iso()
                    try:
                        self._save()
                    except OSError as exc:
                        record["revoked_at"] = None
                        raise AppError("INTERNAL_ERROR", f"Gagal menyimpan registry kunci: {exc}") from exc
                return self._public(record)
        raise AppError("VALIDATION_ERROR", f"Kunci '{wanted}' tidak ada di registry", details={"key_id": wanted})


_REGISTRIES: Dict[str, ApiKeyRegistry] = {}
_REGISTRY_LOCK = threading.Lock()


def registry_for(settings: Any) -> ApiKeyRegistry:
    """Registry untuk proses ini (satu objek per path, cache file mengikuti mtime)."""
    path = str(getattr(settings, "api_keys_path", "") or "").strip()
    if not path:
        override = str(getattr(settings, "settings_override_path", "") or "").strip()
        path = str(Path(override).with_name("api_keys.json")) if override else "data/api_keys.json"
    with _REGISTRY_LOCK:
        registry = _REGISTRIES.get(path)
        if registry is None:
            registry = ApiKeyRegistry(Path(path))
            _REGISTRIES[path] = registry
        return registry


def reset_registries() -> None:
    """Test hook: buang cache registry supaya berkas baru dibaca ulang."""
    with _REGISTRY_LOCK:
        _REGISTRIES.clear()
