"""Kunci admin pertama: supaya konsol baru bisa dipakai tanpa menempel rahasia ke mana pun.

Masalah yang dijawab berkas ini: gerbang kode akses hanya bisa dipasang oleh kredensial
berizin ``admin``. Pada layanan yang baru dipasang, ``API_KEYS_JSON`` masih kosong dan
registry masih kosong, jadi tidak ada seorang pun yang bisa memasang kode - padahal justru
itu langkah pertama yang diinginkan operator. Jalan keluarnya bukan membuka endpoint set
kode untuk umum (pemanggil pertama akan mendapat izin penuh), melainkan menerbitkan satu
kunci admin *bootstrap*:

* dibuat sekali saat layanan dijalankan, hanya bila belum ada kunci dari env/registry;
* nilainya ditulis ke berkas mode 0600 (``BOOTSTRAP_ADMIN_KEY_PATH``), bukan ke log;
* di log hanya jalur berkas dan potongan tersamar yang muncul;
* kunci tampil di panel Kunci API seperti kunci lain, jadi operator bisa mencabutnya
  setelah kode akses dipasang dan kunci sendiri dibuat.

Setelah kunci itu dicabut, berkasnya boleh dihapus: pemanggilan berikutnya tidak membuat
ulang selama sudah ada kunci admin lain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from app.core.api_keys import ALLOWED_PERMISSIONS, registry_for
from app.core.jsonfile import read_json, write_json_atomic
from app.core.logging import get_logger

logger = get_logger(__name__)

BOOTSTRAP_LABEL = "kunci bootstrap (hapus setelah kode akses dipasang)"
ADMIN_PERMISSION = "*"
BOOTSTRAP_CREATOR = "bootstrap"


def _stored_key(path: Path) -> str:
    """Kunci yang sudah tersimpan di berkas bootstrap, atau string kosong."""
    data = read_json(path)
    if isinstance(data, dict):
        return str(data.get("key") or "").strip()
    return ""


def _admin_already_exists(settings: Any) -> bool:
    """Benar bila sudah ada kredensial admin dari env atau registry yang masih hidup."""
    wanted = {ADMIN_PERMISSION, "admin"}
    for _key, context in (settings.api_keys or {}).items():
        if wanted & set(context.get("permissions") or []):
            return True
    registry = registry_for(settings)
    for entry in registry.public():
        if entry.get("state") != "active":
            continue
        if wanted & set(entry.get("permissions") or []):
            return True
    return False


def _bootstrapped_before(settings: Any) -> bool:
    """True bila kunci bootstrap pernah diterbitkan di registry ini.

    Dipakai untuk menghormati pencabutan: setelah operator mencabut kunci bootstrap,
    menjalankan ulang layanan tidak menghidupkannya kembali. Pemasangan benar-benar baru
    (registry kosong) tetap mendapat kuncinya.
    """
    registry = registry_for(settings)
    return any(
        str(record.get("created_by") or "") == BOOTSTRAP_CREATOR for record in registry.records()
    )


def bootstrap_path(settings: Any) -> Path:
    """Lokasi berkas kunci bootstrap.

    Bila ``BOOTSTRAP_ADMIN_KEY_PATH`` kosong, berkas ditaruh **di direktori yang sama dengan
    registry kunci** (``API_KEYS_PATH``). Di container itu penting: berkas di dalam image
    hilang setiap redeploy, sedangkan direktori registry biasanya volume tetap.
    """
    raw = str(getattr(settings, "bootstrap_admin_key_path", "") or "").strip()
    if raw:
        return Path(raw)
    registry_file = str(getattr(settings, "api_keys_path", "") or "").strip()
    if registry_file:
        return Path(registry_file).with_name("bootstrap_admin_key.json")
    return Path("data/bootstrap_admin_key.json")


def ensure_bootstrap_admin(settings: Any) -> Optional[Dict[str, Any]]:
    """Terbitkan kunci admin bootstrap bila memang belum ada jalan masuk lain.

    Mengembalikan entri publik kunci baru, atau ``None`` bila tidak diperlukan.
    """
    if not bool(getattr(settings, "bootstrap_admin_key", True)):
        return None
    if _admin_already_exists(settings) or _bootstrapped_before(settings):
        return None

    path = bootstrap_path(settings)
    existing = _stored_key(path)
    registry = registry_for(settings)
    if existing and registry.resolve(existing) is not None:
        # Berkas lama masih berlaku: jangan terbitkan kunci kedua.
        return None

    permissions = [ADMIN_PERMISSION] if ADMIN_PERMISSION in ALLOWED_PERMISSIONS else ["admin"]
    key, entry = registry.create(
        label=BOOTSTRAP_LABEL,
        permissions=permissions,
        organization_id=str(getattr(settings, "ui_session_organization_id", "default") or "default"),
        user_id=str(getattr(settings, "ui_session_user_id", "operator") or "operator"),
        application_id=str(getattr(settings, "ui_session_application_id", "rag-console") or "rag-console"),
        created_by="bootstrap",
    )
    write_json_atomic(path, {"key": key, "key_id": entry["key_id"], "created_at": entry["created_at"]})
    logger.warning(
        "belum ada kunci admin: kunci bootstrap diterbitkan (%s) dan disimpan di %s. "
        "Pakai sekali untuk memasang kode akses di panel Akses & Sesi, lalu cabut kuncinya.",
        entry.get("hint"),
        path,
    )
    return entry
