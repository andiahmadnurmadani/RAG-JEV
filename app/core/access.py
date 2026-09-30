"""Kode akses konsol + sesi browser.

Konsol web sebelumnya hanya bisa dibuka dengan menempel API key. Modul ini menambahkan satu
kode akses (passcode) yang dibuka operator dari layar masuk, lalu ditukar dengan *sesi* yang
berlaku terbatas: 12 jam, atau 7 hari bila "ingat saya" dicentang.

Aturan yang dipegang:

* **Yang disimpan adalah hash.** ``data/access.json`` hanya memuat ``sha256`` kode + potongan
  tersamar untuk dikenali; ``data/sessions.json`` menyimpan hash token sesi, bukan tokennya.
* **Sesi terikat versi kode.** Mengganti kode akses langsung mematikan seluruh sesi lama.
* **Dicabut, bukan dihapus.** Sesi yang sudah berakhir/kecabut tetap tercatat untuk audit.
* **Penolakan sedini mungkin.** Panjang kode, kode salah, dan percobaan berulang ditolak sebelum
  ada sesi/tulisan yang dibuat.
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

SESSION_PREFIX = "sess_"

MIN_CODE_LENGTH = 6
MAX_CODE_LENGTH = 128
MAX_ACTIVE_SESSIONS = 200
# Sesi mencatat pemakaian terakhir paling sering sekali semenit: cukup untuk audit, tanpa
# menulis berkas pada setiap permintaan.
_TOUCH_INTERVAL_SECONDS = 60.0

# Pembatasan percobaan masuk: percobaan gagal beruntun per alamat klien.
MAX_LOGIN_FAILURES = 8
LOGIN_FAILURE_WINDOW_SECONDS = 300.0


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


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mask_code(code: str) -> str:
    """Potongan aman untuk dikenali operator: cukup untuk memastikan kode mana yang aktif."""
    if len(code) <= 4:
        return "*" * len(code)
    if len(code) <= 10:
        return code[0] + "*" * (len(code) - 2) + code[-1]
    return f"{code[:3]}{'*' * 6}{code[-2:]}"


def normalize_code(code: str) -> str:
    """Kode akses = kode yang diketik apa adanya, kecuali spasi di ujung."""
    return str(code or "").strip()


def validate_code(code: str) -> str:
    clean = normalize_code(code)
    if not clean:
        raise AppError("VALIDATION_ERROR", "Kode akses tidak boleh kosong")
    if len(clean) < MIN_CODE_LENGTH:
        raise AppError(
            "VALIDATION_ERROR",
            f"Kode akses minimal {MIN_CODE_LENGTH} karakter",
            details={"min_length": MIN_CODE_LENGTH},
        )
    if len(clean) > MAX_CODE_LENGTH:
        raise AppError(
            "VALIDATION_ERROR",
            f"Kode akses maksimal {MAX_CODE_LENGTH} karakter",
            details={"max_length": MAX_CODE_LENGTH},
        )
    if any(character.isspace() for character in clean):
        raise AppError("VALIDATION_ERROR", "Kode akses tidak boleh memuat spasi")
    return clean


class AccessCodeStore:
    """Satu kode akses untuk seluruh konsol, tersimpan sebagai hash di satu berkas."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: Tuple[Optional[int], Optional[int]] = (None, None)
        self._record: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------------- berkas
    def _load(self, force: bool = False) -> Optional[Dict[str, Any]]:
        stamp = file_stamp(self.path)
        if force or stamp != self._stamp:
            data = read_json(self.path)
            raw = data.get("code") if isinstance(data, dict) else None
            self._record = dict(raw) if isinstance(raw, dict) else None
            self._stamp = stamp
        return self._record

    def _save(self, record: Optional[Dict[str, Any]]) -> None:
        write_json_atomic(self.path, {"version": 1, "code": record})
        self._stamp = file_stamp(self.path)

    # ------------------------------------------------------------------ baca
    def status(self) -> Dict[str, Any]:
        """Bentuk internal: apakah kode sudah ada, kapan diubah, dan penanda versinya.

        ``generation`` adalah digest penuh dan **tidak boleh dikirim ke klien**: nilainya
        cukup untuk menebak kode dari luar. Pakai :meth:`public_status` untuk respons HTTP.
        """
        with self._lock:
            record = self._load() or {}
        digest = str(record.get("digest") or "")
        return {
            "enabled": bool(digest),
            "hint": record.get("hint"),
            "set_at": record.get("set_at"),
            "updated_by": record.get("updated_by"),
            "generation": digest,
        }

    def public_status(self) -> Dict[str, Any]:
        """Versi untuk UI: sama tanpa ``generation`` (penanda versi tetap di server)."""
        data = self.status()
        data.pop("generation", None)
        return data

    def generation(self) -> str:
        """Penanda versi kode; sesi yang dibuat pada versi lain tidak berlaku lagi."""
        with self._lock:
            record = self._load() or {}
        return str(record.get("digest") or "")

    def verify(self, presented: str) -> bool:
        """Cocokkan kode yang diketik (constant-time) tanpa pernah membocorkan kodenya."""
        clean = normalize_code(presented)
        if not clean:
            return False
        with self._lock:
            record = self._load() or {}
        digest = str(record.get("digest") or "")
        if not digest:
            return False
        return hmac.compare_digest(digest, fingerprint(clean))

    # ----------------------------------------------------------------- tulis
    def set_code(self, code: str, updated_by: str = "") -> Dict[str, Any]:
        clean = validate_code(code)
        record = {
            "digest": fingerprint(clean),
            "hint": mask_code(clean),
            "length": len(clean),
            "set_at": _iso(),
            "updated_by": str(updated_by or ""),
        }
        with self._lock:
            previous = self._load(force=True)
            self._record = record
            try:
                self._save(record)
            except OSError as exc:
                self._record = previous
                raise AppError("INTERNAL_ERROR", f"Gagal menyimpan kode akses: {exc}") from exc
        return self.status()

    def clear(self) -> None:
        """Test hook: hapus kode (konsol kembali memakai API key saja)."""
        with self._lock:
            self._record = None
            self._save(None)


class SessionStore:
    """Sesi browser hasil penukaran kode akses. Isi berkas = hash token, bukan tokennya."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: Tuple[Optional[int], Optional[int]] = (None, None)
        self._records: List[Dict[str, Any]] = []
        self._touched_at: Dict[str, float] = {}

    # ---------------------------------------------------------------- berkas
    def _load(self, force: bool = False) -> List[Dict[str, Any]]:
        stamp = file_stamp(self.path)
        if force or stamp != self._stamp:
            data = read_json(self.path)
            raw = data.get("sessions") if isinstance(data, dict) else None
            self._records = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            self._stamp = stamp
        return self._records

    def _save(self) -> None:
        write_json_atomic(self.path, {"version": 1, "sessions": self._records})
        self._stamp = file_stamp(self.path)

    # ------------------------------------------------------------------ baca
    @staticmethod
    def _expired(record: Dict[str, Any]) -> bool:
        expires = _parse_iso(record.get("expires_at"))
        return bool(expires and expires <= datetime.now(timezone.utc))

    @staticmethod
    def _state(record: Dict[str, Any]) -> str:
        if record.get("revoked_at"):
            return "revoked"
        expires = _parse_iso(record.get("expires_at"))
        if expires and expires <= datetime.now(timezone.utc):
            return "expired"
        return "active"

    def _public(self, record: Dict[str, Any], generation: str) -> Dict[str, Any]:
        return {
            "session_id": record.get("session_id"),
            "label": record.get("label"),
            "hint": record.get("hint"),
            "remember": bool(record.get("remember")),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
            "last_used_at": record.get("last_used_at"),
            "revoked_at": record.get("revoked_at"),
            "client": record.get("client"),
            "state": self._state(record),
            "stale_code": bool(generation) and str(record.get("generation") or "") != generation,
        }

    def public(self, generation: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            return [self._public(item, generation) for item in self._load()]

    def active_count(self, generation: str = "") -> int:
        return len([item for item in self.public(generation) if item["state"] == "active" and not item["stale_code"]])

    # ------------------------------------------------------------- pencocokan
    def resolve(self, presented: str, generation: str = "") -> Optional[Dict[str, Any]]:
        """Sesi untuk token ini, atau ``None`` bila tidak ada, sudah berakhir, atau kode sudah diganti."""
        token = str(presented or "").strip()
        if not token.startswith(SESSION_PREFIX):
            return None
        wanted = fingerprint(token)
        with self._lock:
            for record in self._load():
                digest = str(record.get("digest") or "")
                if not digest or record.get("revoked_at") or self._expired(record):
                    continue
                if generation and str(record.get("generation") or "") != generation:
                    continue
                if hmac.compare_digest(digest, wanted):
                    self._touch(record)
                    return {
                        "session_id": record.get("session_id"),
                        "label": record.get("label"),
                        "remember": bool(record.get("remember")),
                        "created_at": record.get("created_at"),
                        "expires_at": record.get("expires_at"),
                        "client": record.get("client"),
                    }
        return None

    def _touch(self, record: Dict[str, Any]) -> None:
        session_id = str(record.get("session_id") or "")
        if not session_id:
            return
        now = _now()
        if now - self._touched_at.get(session_id, 0.0) < _TOUCH_INTERVAL_SECONDS:
            return
        self._touched_at[session_id] = now
        record["last_used_at"] = _iso(now)
        try:
            self._save()
        except OSError as exc:  # pragma: no cover - izin/disk penuh
            logger.warning("gagal menyimpan last_used_at sesi %s: %s", session_id, exc)

    # ----------------------------------------------------------------- tulis
    def create(
        self,
        *,
        remember: bool,
        generation: str,
        label: str = "",
        client: str = "",
        hours: int = 12,
        days: int = 7,
    ) -> Tuple[str, Dict[str, Any]]:
        """Buat sesi baru. Mengembalikan (token penuh, entri publik) - token tampil sekali saja."""
        if not generation:
            raise AppError("AUTH_INVALID", "Kode akses belum diatur untuk konsol ini")
        lifetime = timedelta(days=max(1, int(days))) if remember else timedelta(hours=max(1, int(hours)))
        with self._lock:
            records = self._load(force=True)
            active = [
                item
                for item in records
                if not item.get("revoked_at") and not self._expired(item)
            ]
            if len(active) >= MAX_ACTIVE_SESSIONS:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"Sudah ada {MAX_ACTIVE_SESSIONS} sesi aktif; keluarkan sesi lain dulu",
                    details={"max_active_sessions": MAX_ACTIVE_SESSIONS},
                )
            token = SESSION_PREFIX + secrets.token_urlsafe(32)
            record: Dict[str, Any] = {
                "session_id": "ses_" + secrets.token_hex(4),
                "label": str(label or "").strip() or (client or "peramban"),
                "hint": f"{token[:11]}...{token[-4:]}",
                "digest": fingerprint(token),
                "generation": str(generation),
                "remember": bool(remember),
                "client": str(client or ""),
                "created_at": _iso(),
                "expires_at": _iso((datetime.now(timezone.utc) + lifetime).timestamp()),
                "last_used_at": None,
                "revoked_at": None,
            }
            records.append(record)
            self._records = records
            try:
                self._save()
            except OSError as exc:
                records.pop()
                raise AppError("INTERNAL_ERROR", f"Gagal menyimpan sesi: {exc}") from exc
            return token, self._public(record, generation)

    def revoke(self, session_id: str) -> Dict[str, Any]:
        wanted = str(session_id or "").strip()
        with self._lock:
            for record in self._load(force=True):
                if record.get("session_id") != wanted:
                    continue
                if not record.get("revoked_at"):
                    record["revoked_at"] = _iso()
                    try:
                        self._save()
                    except OSError as exc:
                        record["revoked_at"] = None
                        raise AppError("INTERNAL_ERROR", f"Gagal menyimpan sesi: {exc}") from exc
                return self._public(record, "")
        raise AppError("VALIDATION_ERROR", f"Sesi '{wanted}' tidak ada", details={"session_id": wanted})

    def revoke_all(self, *, except_id: str = "") -> int:
        """Keluarkan semua sesi aktif (kecuali satu, bila ditentukan)."""
        revoked = 0
        with self._lock:
            for record in self._load(force=True):
                if record.get("revoked_at") or self._expired(record):
                    continue
                if except_id and record.get("session_id") == except_id:
                    continue
                record["revoked_at"] = _iso()
                revoked += 1
            if revoked:
                try:
                    self._save()
                except OSError as exc:
                    raise AppError("INTERNAL_ERROR", f"Gagal menyimpan sesi: {exc}") from exc
        return revoked


class LoginThrottle:
    """Batas percobaan masuk per alamat klien, di memori proses.

    Tujuannya menahan percobaan berulang atas kode akses, bukan menggantikan rate limit HTTP:
    hitungannya hanya untuk percobaan yang **gagal**, dan direset begitu satu percobaan berhasil.
    """

    def __init__(self, max_failures: int = MAX_LOGIN_FAILURES, window: float = LOGIN_FAILURE_WINDOW_SECONDS) -> None:
        self.max_failures = int(max_failures)
        self.window = float(window)
        self._failures: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> List[float]:
        return [moment for moment in self._failures.get(client, []) if now - moment < self.window]

    def blocked_for(self, client: str) -> float:
        """Sisa detik sampai percobaan berikutnya diizinkan (0 = boleh mencoba)."""
        with self._lock:
            now = _now()
            recent = self._recent(client, now)
            self._failures[client] = recent
            if len(recent) < self.max_failures:
                return 0.0
            return max(0.0, self.window - (now - min(recent)))

    def register_failure(self, client: str) -> None:
        with self._lock:
            now = _now()
            recent = self._recent(client, now)
            recent.append(now)
            self._failures[client] = recent

    def remaining_attempts(self, client: str) -> int:
        """Sisa percobaan sebelum alamat ini diblokir sementara."""
        with self._lock:
            recent = self._recent(client, _now())
            self._failures[client] = recent
        return max(0, self.max_failures - len(recent))

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def clear(self) -> None:
        """Test hook."""
        with self._lock:
            self._failures.clear()


_STORES: Dict[str, Any] = {}
_STORE_LOCK = threading.Lock()
THROTTLE = LoginThrottle()


def _path_for(settings: Any, attribute: str, filename: str) -> Path:
    path = str(getattr(settings, attribute, "") or "").strip()
    if not path:
        override = str(getattr(settings, "settings_override_path", "") or "").strip()
        path = str(Path(override).with_name(filename)) if override else f"data/{filename}"
    return Path(path)


def access_store(settings: Any) -> AccessCodeStore:
    """Store kode akses untuk proses ini (satu objek per path, cache mengikuti mtime)."""
    key = str(_path_for(settings, "access_path", "access.json"))
    with _STORE_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = AccessCodeStore(Path(key))
            _STORES[key] = store
        return store


def session_store(settings: Any) -> SessionStore:
    """Store sesi untuk proses ini (satu objek per path, cache mengikuti mtime)."""
    key = str(_path_for(settings, "sessions_path", "sessions.json"))
    with _STORE_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = SessionStore(Path(key))
            _STORES[key] = store
        return store


def reset_stores() -> None:
    """Test hook: buang cache store supaya berkas baru dibaca ulang."""
    with _STORE_LOCK:
        _STORES.clear()
    THROTTLE.clear()


def session_lifetime(settings: Any, remember: bool) -> Dict[str, Any]:
    """Batas usia sesi yang akan dibuat, dalam bentuk yang bisa ditampilkan UI."""
    days = max(1, int(getattr(settings, "ui_remember_days", 7) or 7))
    hours = max(1, int(getattr(settings, "ui_session_hours", 12) or 12))
    if remember:
        return {"remember": True, "days": days, "hours": 0, "label": f"{days} hari"}
    return {"remember": False, "days": 0, "hours": hours, "label": f"{hours} jam"}


def session_context(settings: Any, session: Dict[str, Any]) -> Dict[str, Any]:
    """Konteks tepercaya untuk sebuah sesi - dibentuk di server, bukan dari peramban."""
    return {
        "user_id": str(getattr(settings, "ui_session_user_id", "operator") or "operator"),
        "organization_id": str(getattr(settings, "ui_session_organization_id", "default") or "default"),
        "application_id": str(getattr(settings, "ui_session_application_id", "rag-console") or "rag-console"),
        "permissions": list(settings.ui_session_permission_list),
        "key_id": None,
        "session_id": session.get("session_id"),
        "session_expires_at": session.get("expires_at"),
        "source": "session",
    }
