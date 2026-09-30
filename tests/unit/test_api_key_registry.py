"""Registry kunci: yang disimpan, yang dihasilkan, dan yang berhenti berlaku.

Diuji di level modul (tanpa HTTP) karena di sinilah janjinya berada: berkas hanya memuat
hash, kunci yang dicabut/kedaluwarsa tidak pernah cocok, dan batas jumlah tetap ditegakkan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core import api_keys
from app.core.api_keys import ApiKeyRegistry, fingerprint, generate_key, hint_for
from app.core.errors import AppError


@pytest.fixture()
def registry(tmp_path: Path) -> ApiKeyRegistry:
    return ApiKeyRegistry(tmp_path / "api_keys.json")


def _issue(registry: ApiKeyRegistry, label: str = "uji", **kwargs):
    base = {
        "label": label,
        "permissions": ["read"],
        "organization_id": "org_a",
        "user_id": "user_a",
        "application_id": "app_a",
        "created_by": "user_a@org_a",
    }
    base.update(kwargs)
    return registry.create(**base)


# --------------------------------------------------------------------------- #
# Bentuk kunci
# --------------------------------------------------------------------------- #
def test_a_generated_key_is_prefixed_random_and_unique():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(key.startswith(api_keys.KEY_PREFIX) for key in keys)
    assert all(len(key) > 30 for key in keys)


def test_the_hint_never_shows_the_middle_of_the_key():
    key = "rag_" + "A" * 40
    hint = hint_for(key)
    assert hint.startswith("rag_") and hint.endswith(key[-4:])
    assert key[10:30] not in hint


def test_short_values_are_masked_entirely():
    assert hint_for("rag_1234") == "*" * 8


# --------------------------------------------------------------------------- #
# Yang tersimpan di berkas
# --------------------------------------------------------------------------- #
def test_the_file_holds_a_hash_and_never_the_key(registry: ApiKeyRegistry):
    key, entry = _issue(registry)
    stored = registry.path.read_text(encoding="utf-8")

    assert key not in stored
    assert fingerprint(key) in stored
    assert entry["hint"] in stored
    assert entry["key_id"].startswith("key_")


def test_public_view_hides_the_digest(registry: ApiKeyRegistry):
    _issue(registry, label="Bot CS")
    entry = registry.public()[0]
    assert "digest" not in entry
    assert entry["label"] == "Bot CS"
    assert entry["state"] == "active"
    assert entry["revocable"] is True


def test_the_file_is_only_readable_by_its_owner(registry: ApiKeyRegistry):
    import os
    import stat

    _issue(registry)
    if os.name == "nt":  # chmod tidak berlaku sama di Windows
        pytest.skip("mode berkas POSIX tidak berlaku di Windows")
    mode = stat.S_IMODE(registry.path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_the_registry_is_reread_when_the_file_changes(registry: ApiKeyRegistry):
    """Proses lain (atau deploy baru) bisa menambah kunci; cache ikut mtime berkas."""
    other = ApiKeyRegistry(registry.path)
    key, _ = _issue(other, label="dari proses lain")
    assert registry.resolve(key) is not None


# --------------------------------------------------------------------------- #
# Pencocokan
# --------------------------------------------------------------------------- #
def test_resolve_returns_the_trusted_context(registry: ApiKeyRegistry):
    key, entry = _issue(registry, permissions=["read", "write"])
    context = registry.resolve(key)
    assert context == {
        "user_id": "user_a",
        "organization_id": "org_a",
        "application_id": "app_a",
        "permissions": ["read", "write"],
        "key_id": entry["key_id"],
        "source": "registry",
    }


@pytest.mark.parametrize("candidate", ["", "rag_salah", "test-key-org-a"])
def test_resolve_refuses_anything_it_did_not_issue(registry: ApiKeyRegistry, candidate: str):
    _issue(registry)
    assert registry.resolve(candidate) is None


def test_a_revoked_key_never_resolves(registry: ApiKeyRegistry):
    key, entry = _issue(registry)
    registry.revoke(entry["key_id"])
    assert registry.resolve(key) is None


def test_an_expired_key_never_resolves(registry: ApiKeyRegistry):
    key, entry = _issue(registry, expires_in_days=1)
    records = registry.records()
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    registry.path.write_text(
        __import__("json").dumps({"version": 1, "keys": [{**records[0], "expires_at": past.isoformat()}]}),
        encoding="utf-8",
    )
    assert registry.resolve(key) is None
    assert registry.public()[0]["state"] == "expired"


def test_last_used_at_is_recorded_once_per_interval(registry: ApiKeyRegistry, monkeypatch):
    key, entry = _issue(registry)
    assert registry.records()[0]["last_used_at"] is None

    registry.resolve(key)
    first = registry.records()[0]["last_used_at"]
    assert first is not None

    # pemakaian berikutnya dalam interval yang sama tidak menulis ulang
    registry.resolve(key)
    assert registry.records()[0]["last_used_at"] == first


# --------------------------------------------------------------------------- #
# Penolakan sedini mungkin
# --------------------------------------------------------------------------- #
def test_create_refuses_an_empty_label(registry: ApiKeyRegistry):
    with pytest.raises(AppError) as excinfo:
        _issue(registry, label="   ")
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_create_refuses_a_missing_tenant_field(registry: ApiKeyRegistry):
    with pytest.raises(AppError) as excinfo:
        _issue(registry, organization_id="")
    assert "organization_id" in excinfo.value.message


def test_create_refuses_an_unknown_permission_before_writing(registry: ApiKeyRegistry):
    with pytest.raises(AppError):
        _issue(registry, permissions=["read", "root"])
    assert not registry.path.exists(), "berkas tidak boleh dibuat saat permintaan ditolak"


def test_create_refuses_an_empty_permission_list(registry: ApiKeyRegistry):
    with pytest.raises(AppError) as excinfo:
        _issue(registry, permissions=[])
    assert "izin" in excinfo.value.message.lower()


def test_the_number_of_active_keys_is_capped(registry: ApiKeyRegistry, monkeypatch):
    monkeypatch.setattr(api_keys, "MAX_ACTIVE_KEYS", 2)
    _issue(registry, label="satu")
    _issue(registry, label="dua")
    with pytest.raises(AppError) as excinfo:
        _issue(registry, label="tiga")
    assert "kunci aktif" in excinfo.value.message

    # mencabut satu kunci membebaskan slotnya
    registry.revoke(registry.public()[0]["key_id"])
    assert _issue(registry, label="tiga")[0].startswith("rag_")


def test_revoke_of_an_unknown_key_is_refused(registry: ApiKeyRegistry):
    with pytest.raises(AppError) as excinfo:
        registry.revoke("key_abcdef01")
    assert excinfo.value.code == "VALIDATION_ERROR"
