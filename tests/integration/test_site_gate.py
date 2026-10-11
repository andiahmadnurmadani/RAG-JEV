"""Password situs: halaman web dikunci, API tetap memakai API key seperti biasa."""

from __future__ import annotations

import pytest

from app.core import site_gate
from tests.conftest import TENANT_A_KEY, auth

PASSWORD = "uji-gerbang-2026!"


@pytest.fixture()
def gated(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("SITE_PASSWORD_HASH", site_gate.hash_password(PASSWORD, iterations=1000))
    reset_settings_cache()
    site_gate.THROTTLE.clear()
    site_gate.GLOBAL_THROTTLE.clear()
    with TestClient(create_app(), follow_redirects=False) as client:
        yield client
    site_gate.THROTTLE.clear()
    site_gate.GLOBAL_THROTTLE.clear()


def _login(client, password=PASSWORD, next_path="/ui/"):
    return client.post(site_gate.LOGIN_PATH, data={"password": password, "next": next_path})


def test_halaman_web_dialihkan_ke_login(gated):
    for path in ("/", "/ui/", "/ui/app.js", "/docs", "/openapi.json", "/guide/"):
        response = gated.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith(site_gate.LOGIN_PATH), path
    page = gated.get(site_gate.LOGIN_PATH)
    assert page.status_code == 200 and 'type="password"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-frame-options"] == "DENY"


def test_api_tidak_terpengaruh(gated):
    assert gated.get("/api/v1/health").status_code == 200
    assert gated.get("/api/v1/ready").status_code in (200, 503)
    # tanpa API key tetap 401 dari API (bukan dialihkan ke halaman login)
    assert gated.post("/api/v1/query", json={"query": "halo"}).status_code == 401
    assert gated.get("/api/v1/knowledge-bases", headers=auth(TENANT_A_KEY)).status_code == 200


def test_password_salah_ditolak_dan_dibatasi(gated):
    wrong = _login(gated, "salah")
    assert wrong.status_code == 401 and "Password salah" in wrong.text
    assert site_gate.COOKIE_NAME not in wrong.cookies
    for _ in range(10):
        _login(gated, "salah")
    blocked = _login(gated)  # password benar pun ditahan selama diblokir
    assert blocked.status_code == 429


def test_password_benar_membuka_halaman(gated):
    response = _login(gated, next_path="/ui/app.js")
    assert response.status_code == 303 and response.headers["location"] == "/ui/app.js"
    cookie = response.cookies.get(site_gate.COOKIE_NAME)
    assert cookie
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    gated.cookies.set(site_gate.COOKIE_NAME, cookie)
    assert gated.get("/ui/").status_code == 200
    assert gated.get("/openapi.json").status_code == 200
    # keluar menghapus cookie
    out = gated.get(site_gate.LOGOUT_PATH)
    assert out.status_code == 303 and out.headers["location"] == site_gate.LOGIN_PATH


def test_cookie_palsu_atau_diubah_ditolak(gated):
    good = _login(gated).cookies.get(site_gate.COOKIE_NAME)
    expires, nonce, signature = good.split(".")
    for forged in ("abc", f"{int(expires) + 999999}.{nonce}.{signature}", f"{expires}.{nonce}.{'0' * 64}"):
        gated.cookies.set(site_gate.COOKIE_NAME, forged)
        assert gated.get("/ui/").status_code == 303, forged


def test_tidak_bisa_dipakai_untuk_open_redirect(gated):
    for evil in ("//evil.example", "https://evil.example", "/\\evil.example", "/site-login"):
        response = _login(gated, next_path=evil)
        assert response.headers["location"] == "/ui/", evil


def test_post_tanpa_cookie_ke_halaman_web_401(gated):
    assert gated.post("/ui/").status_code == 401


def test_hash_dan_verifikasi():
    encoded = site_gate.hash_password("rahasia", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert site_gate.verify_password("rahasia", encoded)
    assert not site_gate.verify_password("Rahasia", encoded)
    assert not site_gate.verify_password("rahasia", "bukan-hash")


def test_bawaan_konfigurasi_berupa_hash_yang_valid():
    from app.core.config import Settings

    default = Settings.model_fields["site_password_hash"].default
    assert default.startswith("pbkdf2_sha256$600000$")
    assert len(default.split("$")) == 4


def test_gerbang_bisa_dimatikan(settings):
    gate = site_gate.SiteGate(settings)  # conftest: SITE_PASSWORD_HASH=off
    assert not gate.enabled
