"""Setelan ringkasan dokumen bisa diubah operator, dan UI-nya ada."""

from __future__ import annotations

from tests.conftest import TENANT_A_KEY, auth


def test_summary_settings_are_readable_and_writable(client, settings):
    response = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY))
    assert response.status_code == 200, response.text
    summary = response.json()["data"]["sections"]["summary"]
    assert summary["enabled"] is True
    assert summary["window_tokens"] == 12000
    assert summary["max_documents"] == 3

    updated = client.put(
        "/api/v1/settings",
        json={"summary": {"enabled": False, "max_tokens": 4096, "max_documents": 5}},
        headers=auth(TENANT_A_KEY),
    )
    assert updated.status_code == 200, updated.text
    assert settings.document_summary_enabled is False
    assert settings.summary_max_tokens == 4096
    assert settings.summary_max_documents == 5
    assert "summary.max_tokens" in updated.json()["data"]["applied"]


def test_summary_settings_reject_absurd_values(client, settings):
    before = settings.summary_max_documents
    response = client.put(
        "/api/v1/settings",
        json={"summary": {"max_documents": 500}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert settings.summary_max_documents == before


def test_the_console_exposes_the_summary_panel_and_viewer(client):
    page = client.get("/ui/").text
    script = client.get("/ui/app.js").text
    for field in (
        "s-summary-enabled",
        "s-summary-window",
        "s-summary-maxtok",
        "s-summary-maxdocs",
        "btn-save-summary-svc",
        "dlg-summary",
        "summary-body",
    ):
        assert field in page, f"{field} tidak ada di halaman"
    for name in ("renderSummaryService", "saveSummaryService", "showSummary"):
        assert name in script, f"{name} tidak ada di skrip"
