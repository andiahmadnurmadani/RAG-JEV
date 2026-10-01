"""Gerbang kode akses: satu kode membuka konsol, sesinya dipakai sebagai kredensial.

Keluhan yang melahirkan fitur ini: menekan "Buat kunci" di layar Pengaturan gagal dengan
"AUTH_INVALID: Missing credentials", sebab konsol di peramban tidak punya API key untuk
dikirim. Test di sini mengunci janji penggantinya:

* kode akses tidak pernah tersimpan apa adanya (berkas hanya memuat digest + potongan);
* token sesi juga hanya hash, dan tidak pernah muncul di respons status mana pun;
* sesi diterima di header yang sama dengan API key, jadi endpoint lain tidak perlu berubah;
* "ingat saya" memperpanjang masa berlaku (12 jam vs 7 hari);
* mengganti kode mematikan semua sesi lama; mencabut sesi dicatat, bukan dihapus;
* digest kode (``generation``) tidak pernah dikirim keluar - nilainya cukup untuk menebak kode.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, auth

GATE = "/api/v1/auth/gate"
LOGIN = "/api/v1/auth/login"
SESSION = "/api/v1/auth/session"
ACCESS = "/api/v1/settings/access"
KEYS = "/api/v1/settings/api-keys"
CODE = "kode-akses-uji-123"


def _read(path: str) -> str:
    """Isi berkas, atau string kosong bila berkasnya memang belum dibuat."""
    return Path(path).read_text(encoding="utf-8") if path and Path(path).exists() else ""


def _days_ahead(stamp: str) -> float:
    return (datetime.fromisoformat(stamp) - datetime.now(timezone.utc)).total_seconds() / 86400


def _set_code(client, code: str = CODE, key: str = TENANT_A_KEY):
    return client.put(ACCESS, json={"code": code}, headers=auth(key))


def _login(client, code: str = CODE, remember: bool = False):
    return client.post(LOGIN, json={"code": code, "remember": remember})


def _token(client, code: str = CODE, remember: bool = False) -> str:
    response = _login(client, code, remember)
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _status(client, key: str = TENANT_A_KEY) -> dict:
    response = client.get(ACCESS, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --------------------------------------------------------------------------- #
# Keadaan gerbang
# --------------------------------------------------------------------------- #
def test_the_gate_reports_whether_the_console_is_locked(client):
    body = client.get(GATE).json()["data"]
    assert body["enabled"] is False
    assert body["remember_lifetime"]["days"] >= 1
    assert body["min_code_length"] >= 6


def test_an_admin_key_sets_the_code_and_the_gate_locks(client):
    response = _set_code(client)
    assert response.status_code == 200, response.text
    assert CODE not in response.text
    assert response.json()["data"]["access"]["enabled"] is True
    assert client.get(GATE).json()["data"]["enabled"] is True


def test_the_code_is_stored_as_a_digest_only(client, settings):
    _set_code(client)
    raw = open(settings.access_path, encoding="utf-8").read()
    assert CODE not in raw, "kode akses tidak boleh tersimpan apa adanya"
    assert "digest" in raw
    assert json.loads(raw)["code"]["hint"] != CODE


def test_the_generation_digest_never_leaves_the_server(client):
    _set_code(client)
    body = client.get(ACCESS, headers=auth(TENANT_A_KEY))
    assert "generation" not in body.json()["data"]["access"]
    assert "generation" not in body.text, "penanda versi kode cukup untuk menebak kode"
    assert "digest" not in body.text


def test_a_code_shorter_than_the_minimum_is_refused(client):
    response = client.put(ACCESS, json={"code": "123"}, headers=auth(TENANT_A_KEY))
    assert response.status_code >= 400
    assert client.get(GATE).json()["data"]["enabled"] is False


def test_login_needs_the_gate_to_be_on(client):
    response = _login(client)
    assert response.status_code == 401
    assert response.json()["error"]["details"]["gate_enabled"] is False


# --------------------------------------------------------------------------- #
# Masuk: kode -> sesi
# --------------------------------------------------------------------------- #
def test_a_wrong_code_is_refused_and_the_leftover_attempts_are_told(client, settings):
    _set_code(client)
    response = _login(client, "kode-salah")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_INVALID"
    assert isinstance(response.json()["error"]["details"]["attempts_left"], int)
    assert "sess_" not in _read(settings.sessions_path), "kode salah tidak boleh membuat sesi"


def test_the_session_token_opens_the_api_like_an_api_key(client):
    _set_code(client)
    token = _token(client)
    assert token.startswith("sess_")
    assert client.get("/api/v1/knowledge", headers=auth(token)).status_code == 200
    who = client.get(SESSION, headers=auth(token)).json()["data"]
    assert who["source"] == "session"
    assert who["session"]["session_id"].startswith("ses_")


def test_remember_me_extends_the_session(client):
    _set_code(client)
    short = _login(client, remember=False).json()["data"]
    long = _login(client, remember=True).json()["data"]
    assert short["remember"] is False and long["remember"] is True
    assert long["expires_at"] > short["expires_at"]
    assert _days_ahead(short["expires_at"]) < 1, "tanpa ingat saya: hitungan jam"
    assert _days_ahead(long["expires_at"]) > 6, "ingat saya: sekitar seminggu"


def test_the_session_token_is_never_written_or_listed_in_the_clear(client, settings):
    _set_code(client)
    token = _token(client, remember=True)
    assert token not in client.get(ACCESS, headers=auth(token)).text
    assert token not in open(settings.sessions_path, encoding="utf-8").read()


# --------------------------------------------------------------------------- #
# Inti keluhan: konsol tanpa API key tetap bisa membuat kunci
# --------------------------------------------------------------------------- #
def test_the_console_creates_an_api_key_from_a_session(client):
    _set_code(client)
    token = _token(client)
    response = client.post(
        KEYS, json={"label": "dari-konsol", "permissions": ["read"]}, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    created = response.json()["data"]
    new_key = created.get("key") or created.get("api_key")
    assert new_key.startswith("rag_")
    assert client.get("/api/v1/knowledge", headers=auth(new_key)).status_code == 200


def test_a_session_carries_the_tenant_configured_for_the_console(client):
    """Tenant sesi berasal dari konfigurasi server (UI_SESSION_ORGANIZATION_ID), bukan klien."""
    _set_code(client)
    token = _token(client)
    created = client.post(
        KEYS, json={"label": "sesi", "permissions": ["read"]}, headers=auth(token)
    ).json()["data"]
    assert created["entry"]["organization_id"] == "default"
    assert created["entry"]["source"] == "registry", "kunci baru selalu lahir di registry"


# --------------------------------------------------------------------------- #
# Masa berlaku dan pencabutan
# --------------------------------------------------------------------------- #
def test_changing_the_code_revokes_older_sessions(client):
    """Diganti dari kunci admin: semua sesi lama (yang tidak dikecualikan) dikeluarkan."""
    _set_code(client)
    old = _token(client)
    response = client.put(
        ACCESS, json={"code": "kode-akses-baru-456"}, headers=auth(TENANT_A_KEY)
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["sessions_revoked"] >= 1
    assert client.get("/api/v1/knowledge", headers=auth(old)).status_code == 401
    assert _login(client, "kode-akses-baru-456").status_code == 200


def test_the_caller_session_also_dies_when_the_code_changes(client):
    """Sesi terikat versi kode: sesi pemanggil pun tidak berlaku setelah kode berganti."""
    _set_code(client)
    token = _token(client)
    response = client.put(
        ACCESS, json={"code": "kode-akses-baru-456", "current_code": CODE}, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/knowledge", headers=auth(token)).status_code == 401


def test_replacing_the_code_needs_the_current_one_when_using_a_session(client):
    _set_code(client)
    token = _token(client)
    response = client.put(ACCESS, json={"code": "kode-akses-baru-456"}, headers=auth(token))
    assert response.status_code == 403
    assert response.json()["error"]["details"]["field"] == "current_code"


def test_revoking_a_session_is_recorded_not_deleted(client):
    _set_code(client)
    first = _token(client)
    second = _token(client, remember=True)

    status = _status(client, second)
    assert status["active_sessions"] == 2, "sesi hidup tidak boleh dianggap kode sudah diganti"
    mine = status["context"]["session_id"]
    victim = [row for row in status["sessions"] if row["session_id"] != mine][0]
    assert victim["stale_code"] is False and victim["state"] == "active"

    response = client.delete(f"{ACCESS}/sessions/{victim['session_id']}", headers=auth(second))
    assert response.status_code == 200, response.text
    after = response.json()["data"]
    assert after["active_sessions"] == 1
    assert any(row["state"] == "revoked" for row in after["sessions"])
    assert client.get("/api/v1/knowledge", headers=auth(first)).status_code == 401


def test_revoking_every_session_ends_even_the_caller(client):
    _set_code(client)
    token = _token(client)
    response = client.delete(f"{ACCESS}/sessions", headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["active_sessions"] == 0
    assert client.get("/api/v1/knowledge", headers=auth(token)).status_code == 401


def test_logging_out_ends_only_this_session(client):
    _set_code(client)
    token = _token(client)
    assert client.delete(SESSION, headers=auth(token)).status_code == 200
    assert client.get("/api/v1/knowledge", headers=auth(token)).status_code == 401
    assert client.get("/api/v1/knowledge", headers=auth(TENANT_A_KEY)).status_code == 200


def test_clearing_the_code_returns_the_console_to_api_keys(client):
    _set_code(client)
    token = _token(client)
    response = client.delete(ACCESS, headers=auth(TENANT_A_KEY))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["access"]["enabled"] is False
    assert response.json()["data"]["sessions_revoked"] >= 1
    assert client.get("/api/v1/knowledge", headers=auth(token)).status_code == 401
    assert client.get(GATE).json()["data"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Batas izin: hanya admin
# --------------------------------------------------------------------------- #
def test_only_an_admin_may_read_or_change_the_code(client):
    _set_code(client)
    assert client.get(ACCESS, headers=auth(READ_ONLY_KEY)).status_code == 403
    assert (
        client.put(ACCESS, json={"code": "kode-lain-789"}, headers=auth(READ_ONLY_KEY)).status_code
        == 403
    )
    assert client.delete(ACCESS, headers=auth(READ_ONLY_KEY)).status_code == 403
    assert client.get(ACCESS).status_code == 401


def test_a_limited_key_cannot_revoke_sessions(client):
    _set_code(client)
    _token(client)
    token = _token(client, remember=True)
    created = client.post(
        KEYS, json={"label": "terbatas", "permissions": ["read"]}, headers=auth(token)
    ).json()["data"]
    limited = auth(created.get("key") or created.get("api_key"))
    assert client.delete(f"{ACCESS}/sessions", headers=limited).status_code == 403


def test_a_foreign_session_id_is_refused(client):
    _set_code(client)
    response = client.delete(f"{ACCESS}/sessions/bukan-sesi", headers=auth(TENANT_A_KEY))
    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_gate_is_readable_without_any_credential(client):
    """Layar masuk harus bisa dibaca sebelum ada kredensial apa pun."""
    assert client.get(GATE).status_code == 200
