"""Setelan baru (sampling, perbaikan teks, reranker) harus bisa diubah lewat API dan berlaku."""

from __future__ import annotations

from tests.conftest import TENANT_A_KEY, auth


def test_llm_sampling_settings_are_editable_and_bounded(client):
    # Baca: nilai tampil sebagai angka.
    sections = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]["sections"]
    llm = sections["llm"]
    for field in ("temperature", "top_p", "frequency_penalty", "repair_attempts"):
        assert field in llm, f"{field} tidak muncul di /settings"

    # Tulis: nilai baru diterima.
    response = client.put(
        "/api/v1/settings",
        json={"llm": {"top_p": 0.7, "frequency_penalty": 0.3, "repair_attempts": 2, "temperature": 0.2}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    applied = response.json()["data"]["applied"]
    assert "llm.top_p" in applied and "llm.repair_attempts" in applied

    # Nilai di luar batas ditolak, bukan diterima apa adanya.
    for payload, field in (
        ({"top_p": 5}, "llm.top_p"),
        ({"temperature": 9}, "llm.temperature"),
        ({"repair_attempts": 99}, "llm.repair_attempts"),
    ):
        bad = client.put("/api/v1/settings", json={"llm": payload}, headers=auth(TENANT_A_KEY))
        assert bad.status_code == 422, f"{field}: {bad.status_code}"
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_new_sampling_values_reach_the_model(client, settings):
    """Nilai yang disimpan harus benar-benar dipakai panggilan LLM berikutnya."""
    client.put(
        "/api/v1/settings",
        json={"llm": {"top_p": 0.55, "frequency_penalty": 0.44}},
        headers=auth(TENANT_A_KEY),
    )
    assert settings.llm_top_p == 0.55
    assert settings.llm_frequency_penalty == 0.44


def test_reranker_provider_is_selectable_and_validated(client, settings):
    sections = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]["sections"]
    # Nilainya bisa dibaca (di lingkungan uji, fixture menyetel provider dari env).
    assert sections["retrieval"].get("reranker_provider") in {
        "lexical", "none", "fastembed", "sentence_transformers"
    }

    ok = client.put(
        "/api/v1/settings",
        json={"retrieval": {"reranker_provider": "lexical", "reranker_enabled": True}},
        headers=auth(TENANT_A_KEY),
    )
    assert ok.status_code == 200, ok.text
    assert settings.reranker_provider == "lexical"

    bad = client.put(
        "/api/v1/settings",
        json={"retrieval": {"reranker_provider": "bukan-mesin"}},
        headers=auth(TENANT_A_KEY),
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_console_exposes_the_new_fields():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    page = (repo / "app" / "ui" / "index.html").read_text(encoding="utf-8")
    script = (repo / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    for element in ("llm-temperature", "llm-top-p", "llm-frequency-penalty", "llm-repair-attempts",
                    "s-reranker", "s-reranker-provider"):
        assert f'id="{element}"' in page, f"{element} tidak ada di index.html"
        assert f'$("{element}")' in script, f"{element} tidak dipakai app.js"


def test_foreign_script_switch_is_editable(client, settings):
    """Layanan yang knowledge-nya beraksara lain harus bisa mematikan filter ini."""
    sections = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]["sections"]
    assert "strip_foreign_scripts" in sections["llm"]

    response = client.put(
        "/api/v1/settings",
        json={"llm": {"strip_foreign_scripts": False}},
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 200, response.text
    assert settings.text_strip_foreign is False
