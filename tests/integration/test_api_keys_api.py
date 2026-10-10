"""Kunci API buatan layar Pengaturan: batas izin, kerahasiaan, dan pencabutan.

Yang diuji di sini adalah *batas*, bukan jalur bahagia: siapa yang boleh membuat, apa
yang boleh dilihat kembali, dan apa yang terjadi setelah kunci dicabut.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.conftest import READ_ONLY_KEY, SUPER_KEY, TENANT_A_KEY, TENANT_B_KEY, auth

CREATE = "/api/v1/settings/api-keys"
LIST = "/api/v1/settings/api-keys"


def _create(client, key: str = TENANT_A_KEY, **payload):
    body = {"label": "Bot CS", "permissions": ["read", "write"]}
    body.update(payload)
    return client.post(CREATE, json=body, headers=auth(key))


def _list(client, key: str = TENANT_A_KEY):
    return client.get(LIST, headers=auth(key))


def _entry_for(client, key_id: str) -> dict:
    for entry in _list(client).json()["data"]["keys"]:
        if entry.get("key_id") == key_id:
            return entry
    raise AssertionError(f"kunci {key_id} tidak ada di daftar")


# --------------------------------------------------------------------------- #
# Siapa yang boleh menyentuh kunci
# --------------------------------------------------------------------------- #
def test_operator_org_keys_may_list_keys_in_api_key_only_mode(client, settings):
    """Mode satu operator: kunci TULIS organisasi operator mengelola kunci; kunci hanya-baca
    (dipasang di aplikasi chat) dan kunci tenant lain tidak."""
    settings.ui_session_organization_id = "org_a"
    assert _list(client, TENANT_A_KEY).status_code == 200
    assert _list(client, READ_ONLY_KEY).status_code == 403
    assert _list(client, TENANT_B_KEY).status_code == 403


def test_keys_may_be_created_and_revoked_within_the_operator_org(client, settings):
    settings.ui_session_organization_id = "org_b"
    created = _create(client, TENANT_B_KEY)
    assert created.status_code == 200, created.text
    key_id = created.json()["data"]["entry"]["key_id"]
    assert client.delete(f"{CREATE}/{key_id}", headers=auth(TENANT_B_KEY)).status_code == 200


def test_a_key_of_another_org_cannot_be_revoked(client):
    """Kunci admin org_a tidak boleh mencabut kunci milik org_b."""
    created = _create(client, SUPER_KEY, organization_id="org_b", user_id="u", application_id="a")
    key_id = created.json()["data"]["entry"]["key_id"]
    refused = client.delete(f"{CREATE}/{key_id}", headers=auth(TENANT_A_KEY))
    assert refused.status_code == 403
    assert client.delete(f"{CREATE}/{key_id}", headers=auth(SUPER_KEY)).status_code == 200


def test_a_key_cannot_grant_more_than_it_has(client, settings):
    """K1: kunci tanpa admin tidak boleh membuat kunci '*' / admin (eskalasi dua langkah)."""
    settings.ui_session_organization_id = "org_b"
    for permissions in (["*"], ["admin"]):
        response = _create(client, TENANT_B_KEY, permissions=permissions)
        assert response.status_code == 403, (permissions, response.text)
    assert _create(client, TENANT_B_KEY, permissions=["read", "write"]).status_code == 200
    # Kunci hanya-baca tidak mengelola kunci sama sekali; kunci admin pun tidak bisa membuat '*'.
    settings.ui_session_organization_id = "org_a"
    assert _create(client, READ_ONLY_KEY, permissions=["read"]).status_code == 403
    assert _create(client, TENANT_A_KEY, permissions=["*"]).status_code == 403


def test_a_key_can_be_bound_to_knowledge_bases(client):
    created = _create(client, label="Proyek A", knowledge_base_ids=["kb_proyek_a"])
    assert created.status_code == 200, created.text
    assert created.json()["data"]["entry"]["knowledge_base_ids"] == ["kb_proyek_a"]
    key = created.json()["data"]["key"]
    other = client.post(
        "/api/v1/search",
        json={"query": "cuti", "knowledge_base_id": "kb_proyek_b"},
        headers=auth(key),
    )
    assert other.status_code == 403
    own = client.post(
        "/api/v1/search",
        json={"query": "cuti", "knowledge_base_id": "kb_proyek_a"},
        headers=auth(key),
    )
    assert own.status_code == 200
    escape = client.post(CREATE, json={"label": "lepas", "permissions": ["read"]}, headers=auth(key))
    assert escape.status_code == 403, "kunci terikat tidak boleh membuat kunci tanpa ikatan"


def test_only_admin_may_manage_keys_when_api_key_only_is_off(client, settings):
    """CONSOLE_API_KEY_ONLY=false mengembalikan aturan lama: hanya izin admin."""
    settings.console_api_key_only = False
    for key in (TENANT_B_KEY, READ_ONLY_KEY):
        response = _list(client, key)
        assert response.status_code == 403
        assert response.json()["error"]["details"]["permission"] == "admin"
    assert _create(client, TENANT_B_KEY).status_code == 403
    assert client.delete(f"{CREATE}/key_deadbeef", headers=auth(READ_ONLY_KEY)).status_code == 403


def test_missing_credentials_cannot_list_keys(client):
    assert client.get(LIST).status_code == 401


# --------------------------------------------------------------------------- #
# Membuat kunci: hasil, konteks, dan langsung terpakai
# --------------------------------------------------------------------------- #
def test_created_key_is_returned_once_and_works_immediately(client):
    response = _create(client, label="Bot CS")
    assert response.status_code == 200
    data = response.json()["data"]
    key = data["key"]
    assert key.startswith("rag_") and len(key) > 20
    assert data["entry"]["state"] == "active"
    assert data["entry"]["source"] == "registry"
    assert data["entry"]["organization_id"] == "org_a"
    assert data["entry"]["created_by"] == "user_a@org_a"

    # kunci baru langsung dipakai ke endpoint data
    assert client.get("/api/v1/knowledge", headers=auth(key)).status_code == 200


def test_key_hash_is_not_the_key_and_stays_out_of_every_response(client, settings):
    key = _create(client, label="Bot CS").json()["data"]["key"]
    blob = json.dumps(_list(client).json())

    assert key not in blob
    import hashlib

    assert hashlib.sha256(key.encode()).hexdigest() not in blob
    stored = Path(settings.api_keys_path).read_text(encoding="utf-8")
    assert key not in stored
    assert hashlib.sha256(key.encode()).hexdigest() in stored


def test_default_permissions_are_read_and_write_not_admin(client):
    """Izin yang DIBERIKAN tetap read+write (bukan admin) - hanya akses konsol yang longgar.

    Bedakan dua hal: kunci baru tidak diberi izin ``admin`` (itu tidak berubah), tetapi pada
    mode "cukup API key" kunci itu tetap boleh membuka konsol. Dengan mode dimatikan, izinnya
    kembali menentukan: tanpa admin, Pengaturan ditolak.
    """
    key = _create(client, label="Bot CS").json()["data"]["key"]
    assert client.get("/api/v1/knowledge", headers=auth(key)).status_code == 200
    entry = [item for item in _list(client).json()["data"]["keys"] if item.get("label") == "Bot CS"]
    assert entry and sorted(entry[0]["permissions"]) == ["read", "write"], entry

    settings = client.app.state.services.settings
    settings.console_api_key_only = False
    assert client.get("/api/v1/settings", headers=auth(key)).status_code == 403


def test_entry_reports_the_permissions_that_were_granted(client):
    entry = _create(client, permissions=["read", "admin"]).json()["data"]["entry"]
    assert sorted(entry["permissions"]) == ["admin", "read"]


def test_env_keys_are_visible_but_not_revocable(client):
    # Kunci '*' melihat semua; kunci tenant hanya kunci organisasinya sendiri.
    keys = _list(client, SUPER_KEY).json()["data"]["keys"]
    env = [item for item in keys if item["source"] == "env"]
    assert len(env) == 4
    own = [item for item in _list(client).json()["data"]["keys"] if item["source"] == "env"]
    assert {item["organization_id"] for item in own} == {"org_a"}
    assert all(item["key_id"] is None for item in env)
    assert all(item["revocable"] is False for item in env)
    # potongan kunci env cukup untuk mengenali, tidak cukup untuk dipakai
    assert not any(TENANT_A_KEY in json.dumps(item) for item in env)

    refused = client.delete(f"{CREATE}/{TENANT_A_KEY}", headers=auth(TENANT_A_KEY))
    assert refused.status_code == 422
    assert "API_KEYS_JSON" in refused.json()["error"]["message"]


def test_list_reports_the_calling_context(client):
    data = _list(client).json()["data"]
    assert data["context"]["organization_id"] == "org_a"
    assert data["context"]["user_id"] == "user_a"
    assert data["context"]["source"] == "env"
    assert data["context"]["key_id"] is None
    assert sorted(data["allowed_permissions"]) == ["*", "admin", "read", "write"]


def test_last_used_at_is_filled_after_the_key_is_used(client):
    key_id = _create(client, label="Bot CS").json()["data"]["entry"]["key_id"]
    assert _entry_for(client, key_id)["last_used_at"] is None

    key = _create(client, label="Bot kedua").json()["data"]["key"]
    client.get("/api/v1/knowledge", headers=auth(key))

    used = [item for item in _list(client).json()["data"]["keys"] if item["label"] == "Bot kedua"][0]
    assert used["last_used_at"] is not None


# --------------------------------------------------------------------------- #
# Konteks tenant kunci baru
# --------------------------------------------------------------------------- #
def test_tenant_fields_default_to_the_calling_key(client):
    entry = _create(client, label="Bot CS").json()["data"]["entry"]
    assert (entry["organization_id"], entry["user_id"], entry["application_id"]) == ("org_a", "user_a", "app_a")


def test_choosing_another_tenant_without_star_permission_is_refused(client):
    response = _create(client, label="Lintas tenant", organization_id="org_b")
    assert response.status_code == 403
    assert response.json()["error"]["details"]["fields"] == ["organization_id"]
    # penolakan terjadi sebelum kunci apa pun dibuat
    assert [item for item in _list(client).json()["data"]["keys"] if item["source"] == "registry"] == []


def test_a_super_key_may_mint_a_key_for_another_tenant(client):
    response = _create(client, SUPER_KEY, label="Bot tenant B", organization_id="org_b", user_id="user_b", application_id="app_b")
    assert response.status_code == 200
    key = response.json()["data"]["key"]
    assert response.json()["data"]["entry"]["organization_id"] == "org_b"
    # konteks itu benar-benar berlaku: kunci org_b tidak melihat dokumen org_a
    assert client.get("/api/v1/knowledge", headers=auth(key)).status_code == 200


# --------------------------------------------------------------------------- #
# Validasi ditolak sedini mungkin
# --------------------------------------------------------------------------- #
def test_an_empty_label_is_refused(client):
    response = _create(client, label="   ")
    assert response.status_code == 422
    assert "label" in response.json()["error"]["message"].lower()


def test_an_overlong_label_is_refused(client):
    assert _create(client, label="x" * 65).status_code == 422


def test_an_unknown_permission_is_refused(client):
    response = _create(client, permissions=["read", "delete-everything"])
    assert response.status_code == 422
    assert response.json()["error"]["details"]["unknown"] == ["delete-everything"]


def test_an_empty_permission_list_is_refused_not_silently_defaulted(client):
    response = _create(client, permissions=[])
    assert response.status_code == 422
    assert "izin" in response.json()["error"]["message"].lower()


def test_expiry_must_be_within_range(client):
    assert _create(client, expires_in_days=0).status_code == 422
    assert _create(client, expires_in_days=5000).status_code == 422
    assert _create(client, expires_in_days="besok").status_code == 422

    entry = _create(client, expires_in_days=30).json()["data"]["entry"]
    assert entry["expires_at"] is not None


def test_unknown_body_fields_are_refused(client):
    response = _create(client, permissions=["read"], tenant_id="org_b")
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Pencabutan
# --------------------------------------------------------------------------- #
def test_revoked_key_stops_working(client):
    created = _create(client, label="Sementara").json()["data"]
    key, key_id = created["key"], created["entry"]["key_id"]
    assert client.get("/api/v1/knowledge", headers=auth(key)).status_code == 200

    revoked = client.delete(f"{CREATE}/{key_id}", headers=auth(TENANT_A_KEY))
    assert revoked.status_code == 200
    assert revoked.json()["data"]["entry"]["state"] == "revoked"

    after = client.get("/api/v1/knowledge", headers=auth(key))
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "AUTH_INVALID"
    assert _entry_for(client, key_id)["state"] == "revoked"


def test_a_key_cannot_revoke_itself(client):
    created = _create(client, permissions=["read", "admin"]).json()["data"]
    key, key_id = created["key"], created["entry"]["key_id"]

    response = client.delete(f"{CREATE}/{key_id}", headers=auth(key))
    assert response.status_code == 403
    assert "dirinya sendiri" in response.json()["error"]["message"]
    # kuncinya masih hidup
    assert client.get("/api/v1/knowledge", headers=auth(key)).status_code == 200


def test_revoking_an_unknown_key_is_a_clear_refusal(client):
    response = client.delete(f"{CREATE}/key_00000000", headers=auth(TENANT_A_KEY))
    assert response.status_code == 422
    assert "tidak ada" in response.json()["error"]["message"]


def test_revoking_twice_is_idempotent(client):
    key_id = _create(client, label="Sementara").json()["data"]["entry"]["key_id"]
    first = client.delete(f"{CREATE}/{key_id}", headers=auth(TENANT_A_KEY))
    second = client.delete(f"{CREATE}/{key_id}", headers=auth(TENANT_A_KEY))
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["data"]["entry"]["revoked_at"] == second.json()["data"]["entry"]["revoked_at"]


def test_a_key_created_by_an_admin_can_manage_settings_when_granted_admin(client):
    key = _create(client, permissions=["read", "admin"]).json()["data"]["key"]
    assert client.get("/api/v1/settings", headers=auth(key)).status_code == 200
