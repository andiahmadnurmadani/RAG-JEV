"""Setelan sumber web bisa diubah operator tanpa redeploy, dan nilainya mengikat crawl."""

from __future__ import annotations

from tests.conftest import TENANT_A_KEY, auth


def test_web_settings_can_be_read_and_written(client, settings):
    response = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY))
    assert response.status_code == 200, response.text
    web = response.json()["data"]["sections"]["web"]
    assert web["enabled"] is True
    assert web["respect_robots"] is True
    assert web["allow_private_urls"] is False, "bawaan harus menolak alamat privat"

    updated = client.put(
        "/api/v1/settings",
        json={"web": {"max_pages": 7, "max_depth": 1, "same_host": False}},
        headers=auth(TENANT_A_KEY),
    )
    assert updated.status_code == 200, updated.text
    assert settings.web_crawl_max_pages == 7
    assert settings.web_crawl_max_depth == 1
    assert settings.web_crawl_same_host is False
    assert "web.max_pages" in updated.json()["data"]["applied"]


def test_web_settings_reject_absurd_values(client, settings):
    before = settings.web_crawl_max_pages
    response = client.put(
        "/api/v1/settings",
        json={"web": {"max_pages": 10_000}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert settings.web_crawl_max_pages == before


def test_web_settings_can_turn_the_feature_off(client, settings):
    response = client.put(
        "/api/v1/settings",
        json={"web": {"enabled": False}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200
    assert settings.web_crawl_enabled is False
    assert response.json()["data"]["sections"]["web"]["enabled"] is False


def test_ready_publishes_the_web_policy(client):
    detail = client.get("/api/v1/ready").json()["data"]["detail"]
    assert "web" in detail
    assert set(detail["web"]) >= {"enabled", "max_pages", "max_depth", "respect_robots", "allow_private_urls"}


def test_the_console_exposes_the_web_panel_and_the_url_source(client):
    page = client.get("/ui/").text
    script = client.get("/ui/app.js").text
    for field in ("s-web-enabled", "s-web-pages", "s-web-depth", "btn-save-web-svc", "url-crawl"):
        assert field in page, f"{field} tidak ada di halaman"
    for name in ("renderWebService", "saveWebService"):
        assert name in script, f"{name} tidak ada di skrip"
