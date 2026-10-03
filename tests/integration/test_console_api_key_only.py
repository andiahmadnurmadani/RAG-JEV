"""Konsol mode "cukup API key": gerbang, panel Akses & Sesi, dan tombol Keluar.

Permintaan operator: "tidak perlu kode akses, cukup masukkan API key saja, dan bisa akses
semuanya". Konsekuensinya di konsol:

* ``/auth/gate`` melaporkan ``api_key_only`` sehingga konsol tahu harus meminta kunci API;
* panel **Akses & Sesi** disembunyikan (fitur itu tidak dipakai pada mode ini);
* tombol **Keluar** juga disembunyikan (tidak ada sesi untuk diakhiri);
* dengan ``CONSOLE_API_KEY_ONLY=false``, semuanya kembali: gerbang aktif dan panel itu muncul.

Uji ini menjaga agar bagian-bagian itu tidak "hidup lagi" tanpa sengaja, dan agar keduanya
(API-key-only maupun kode akses) tetap tersedia.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import TENANT_A_KEY, auth

REPO = Path(__file__).resolve().parents[2]


def test_gate_reports_api_key_only_mode(client):
    data = client.get("/api/v1/auth/gate").json()["data"]
    assert data["api_key_only"] is True, "bawaan harus mode cukup API key"
    assert data["enabled"] is False, "gerbang tidak dipakai pada mode ini"
    # ``code_set`` tetap ada supaya operator bisa melihat apakah kode pernah dipasang.
    assert "code_set" in data


def test_gate_reports_the_gate_when_api_key_only_is_off(client, settings):
    settings.console_api_key_only = False
    data = client.get("/api/v1/auth/gate").json()["data"]
    assert data["api_key_only"] is False
    # ``enabled`` = gerbang dipakai DAN kode sudah dipasang. Sebelum kode dipasang, gerbangnya
    # belum mengunci siapa pun - itu sebabnya ``code_set`` dipisahkan.
    assert data["enabled"] is False
    assert data["code_set"] is False
    # Pasang kode: barulah gerbangnya mengunci.
    response = client.put(
        "/api/v1/settings/access", json={"code": "kode-uji-123"}, headers=auth()
    )
    assert response.status_code == 200, response.text
    after = client.get("/api/v1/auth/gate").json()["data"]
    assert after["enabled"] is True, "gerbang harus aktif setelah kode dipasang"
    assert after["code_set"] is True


def test_the_console_hides_access_panel_and_logout_in_api_key_only_mode():
    """Bagian yang hanya berguna dengan kode akses harus disembunyikan, bukan dibiarkan mati."""
    script = (REPO / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "function applyConsoleMode()" in script, "tidak ada pengatur tampilan per mode"
    assert 'state.apiKeyOnly = true;' in script, "mode cukup API key tidak dikenali konsol"
    # Panel Akses & Sesi + tombol Keluar ditangani oleh fungsi itu.
    assert re.search(r"data-panel='access'", script), "panel Akses & Sesi tidak ditangani"
    assert 'const logout = $("btn-logout");' in script, "tombol Keluar tidak ditangani"
    # Panel itu tidak boleh dipilih saat mode ini (kalau dipilih, isinya tetap tampil).
    assert 'if (state.apiKeyOnly && state.panel === "access") state.panel = "conn";' in script


def test_boot_applies_the_console_mode_before_showing_anything():
    """applyConsoleMode() harus dipanggil di boot untuk kedua cabang (mode & gerbang)."""
    script = (REPO / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    boot = script.split("async function boot()", 1)[1]
    assert boot.count("applyConsoleMode()") >= 2, (
        "applyConsoleMode() harus dipanggil baik pada mode cukup API key maupun mode kode akses"
    )
    # Pesan yang memandu operator ke panel Koneksi (bukan layar kode akses).
    assert "Konsol ini tidak memakai kode akses" in script
