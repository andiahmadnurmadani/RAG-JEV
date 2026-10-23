"""Batas permintaan yang bisa diatur dari layar Setelan (bagian ``limits``).

Yang diuji: batas dibaca saat permintaan masuk (ubah setelan = langsung berlaku), dihitung
per API key (bukan satu ember bersama), 0 = tanpa batas, dan nilai ngawur ditolak.
"""

from __future__ import annotations

import pytest

from app.api.middleware.ratelimit import ConcurrencyGate, SlidingWindowLimiter
from app.core.errors import AppError
from tests.conftest import READ_ONLY_KEY, TENANT_A_KEY, TENANT_B_KEY, auth


def _put_limits(client, **limits):
    return client.put("/api/v1/settings", headers=auth(TENANT_A_KEY), json={"limits": limits})


def test_limiter_reads_its_limit_on_every_check():
    box = {"limit": 2}
    limiter = SlidingWindowLimiter(lambda: box["limit"])
    limiter.check("a")
    limiter.check("a")
    with pytest.raises(AppError) as err:
        limiter.check("a")
    assert err.value.code == "RATE_LIMITED"
    assert err.value.details["retry_after_seconds"] >= 1
    box["limit"] = 0  # 0 = tanpa batas
    for _ in range(50):
        limiter.check("a")
    box["limit"] = 3
    limiter.check("b")  # identitas lain punya ember sendiri


def test_concurrency_gate_rejects_beyond_limit_and_frees_slots():
    gate = ConcurrencyGate(lambda: 1)
    with gate.slot():
        with pytest.raises(AppError) as err:
            with gate.slot():
                pass
        assert err.value.code == "RATE_LIMITED"
    assert gate.running == 0
    with gate.slot():
        assert gate.running == 1


def test_limits_are_admin_settings_and_validated(client, settings):
    body = client.get("/api/v1/settings", headers=auth(TENANT_A_KEY)).json()["data"]
    assert body["sections"]["limits"] == {
        "per_minute": settings.rate_limit_per_minute,
        "read_per_minute": settings.rate_limit_read_per_minute,
        "global_per_minute": settings.rate_limit_global_per_minute,
        "max_concurrent_queries": settings.max_concurrent_queries,
    }
    bad = _put_limits(client, read_per_minute=-1)
    assert bad.status_code == 422 or bad.status_code == 400
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _put_limits(client, max_concurrent_queries=999).json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.put("/api/v1/settings", headers=auth(READ_ONLY_KEY), json={"limits": {"per_minute": 1}}).status_code == 403


def test_read_limit_applies_per_key_and_changes_without_restart(client):
    assert _put_limits(client, read_per_minute=3, global_per_minute=0).status_code == 200
    for _ in range(3):
        assert client.get("/api/v1/knowledge", headers=auth(TENANT_B_KEY)).status_code == 200
    blocked = client.get("/api/v1/knowledge", headers=auth(TENANT_B_KEY))
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    # kunci lain tidak ikut terkena (dulu semua kunci berbagi ember "anon")
    assert client.get("/api/v1/knowledge", headers=auth(TENANT_A_KEY)).status_code == 200
    assert _put_limits(client, read_per_minute=0).status_code == 200
    assert client.get("/api/v1/knowledge", headers=auth(TENANT_B_KEY)).status_code == 200


def test_registry_keys_of_the_same_application_have_their_own_quota(client):
    # Kunci dari konsol mewarisi application_id yang sama; integrasi yang membanjiri tidak boleh
    # menghabiskan jatah kunci lain (dulu konsol operator ikut kena 429 karena KMS).
    made = [
        client.post("/api/v1/settings/api-keys", headers=auth(TENANT_A_KEY), json={"label": label, "permissions": ["read"]})
        for label in ("integrasi", "operator")
    ]
    assert all(response.status_code == 200 for response in made), made[0].text
    flood, other = (response.json()["data"]["key"] for response in made)
    assert _put_limits(client, read_per_minute=2, global_per_minute=0).status_code == 200
    codes = [client.get("/api/v1/knowledge", headers=auth(flood)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get("/api/v1/knowledge", headers=auth(other)).status_code == 200
    assert _put_limits(client, read_per_minute=0).status_code == 200


def test_global_limit_counts_all_keys_together(client):
    assert _put_limits(client, global_per_minute=4).status_code == 200  # PUT ini sendiri tidak dihitung
    codes = [client.get("/api/v1/knowledge", headers=auth(key)).status_code for key in (TENANT_A_KEY, TENANT_B_KEY) * 3]
    assert codes[:4] == [200, 200, 200, 200]
    assert codes[4:] == [429, 429]
    assert _put_limits(client, global_per_minute=0).status_code == 200


def test_concurrent_query_limit_returns_429_while_busy(client):
    assert _put_limits(client, max_concurrent_queries=1).status_code == 200
    from app.api.deps import services_from_request  # noqa: F401  (memastikan modul ada)
    from app.api.middleware.ratelimit import limits_for

    gate = limits_for(client.app.state.services.settings).queries
    with gate.slot():  # satu pertanyaan "sedang berjalan"
        response = client.post("/api/v1/search", headers=auth(TENANT_A_KEY), json={"query": "apa saja", "knowledge_base_id": "kb_x"})
    assert response.status_code == 429
    assert "busy" in response.json()["error"]["message"]
    assert gate.running == 0
