"""The bundled test console: served same-origin, no secrets inside, no CORS by default."""

from __future__ import annotations

import re


def test_ui_is_served_without_credentials(client):
    """The console itself is static; the API key is typed by the operator in the browser."""
    response = client.get("/ui/")
    assert response.status_code == 200
    body = response.text
    assert "RAG Console" in body
    # Assets are referenced relatively so the console also works behind a proxy that
    # mounts it somewhere other than /ui/.
    assert re.search(r'src="app\.js\?v=[^"]+"', body), "app.js harus punya cap versi"
    assert re.search(r'href="style\.css\?v=[^"]+"', body), "style.css harus punya cap versi"


def test_static_assets_are_never_cached_hard(client):
    """Berkas statis tanpa hash nama: peramban harus selalu boleh menanyakan versi baru.

    Tanpa ini, setelah redeploy HTML baru dijalankan bersama app.js lama yang tersimpan
    di cache, dan elemen yang sudah tidak ada membuat halaman melempar TypeError.
    """
    for path in ("/ui/", "/ui/app.js", "/ui/style.css"):
        header = client.get(path).headers.get("cache-control", "")
        assert "no-cache" in header, f"{path}: Cache-Control={header!r}"
        assert "max-age" not in header, f"{path}: Cache-Control={header!r}"


def test_every_element_the_script_touches_exists_in_the_page(client):
    """Penjaga null: setiap id yang dicari app.js harus ada di HTML yang dikirim.

    Bug nyata yang pernah terjadi: app.js masih menyentuh #view-settings/#btn-back
    sementara HTML-nya sudah memakai dialog — halaman mati dengan TypeError.
    """
    page = client.get("/ui/").text
    script = client.get("/ui/app.js").text
    ids = set(re.findall(r'\$\("([^"]+)"\)', script))
    ids |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
    assert ids, "tidak ada id yang terbaca dari app.js"
    missing = sorted(node for node in ids if f'id="{node}"' not in page)
    assert not missing, f"id ini dipakai app.js tapi tidak ada di index.html: {missing}"


def test_ui_assets_are_reachable(client):
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_root_redirects_to_the_console(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/ui/"


def test_console_html_carries_no_credential(client):
    """Nothing in the shipped HTML may look like a key or a tenant identifier."""
    body = client.get("/ui/").text
    for forbidden in ("live-key", "org_a", "org_b", "Bearer "):
        assert forbidden not in body, forbidden


def test_cors_is_closed_by_default(client):
    response = client.get("/api/v1/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}


def test_console_uses_the_documented_json_contract_not_multipart(client):
    """Uploads go as content_base64 JSON: /knowledge/index has no multipart variant, so a
    FormData upload fails with 422 (which it did, until this test existed)."""
    script = client.get("/ui/app.js").text
    assert "content_base64" in script
    assert "FormData" not in script


def test_console_refuses_an_oversized_file_without_sending_it(client):
    """The browser checks the published limit itself; the server check stays the backstop."""
    script = client.get("/ui/app.js").text
    assert "max_upload_mb" in script
    assert "file.size > limit" in script
    assert "melebihi batas" in script


def test_upload_is_separate_from_the_prompt_composer(client):
    """Knowledge upload has its own controls; the chat input only sends prompts."""
    body = client.get("/ui/").text
    for node in ("dropzone", "file-input", "btn-paste", "btn-url"):
        assert 'id="' + node + '"' in body, node
    for node in ("prompt", "btn-send", "thread-inner", "scope-chip"):
        assert 'id="' + node + '"' in body, node
    # the composer must not contain any file input
    composer = body[body.index('class="composer"') :]
    assert "file-input" not in composer


def test_settings_expose_the_jev_route_and_retrieval_controls(client):
    body = client.get("/ui/").text
    assert 'id="r-route"' in body
    assert "otomatis (dari Jev)" in body
    assert 'id="r-strict"' in body and 'id="r-hybrid"' in body and 'id="r-reranker"' in body
    script = client.get("/ui/app.js").text
    assert "options.route" in script


def test_the_main_page_stays_clean_and_configuration_lives_in_settings(client):
    """The chat page holds upload, the document list and the prompt - nothing else.

    Model/Jev configuration must not be reachable from the chat surface, otherwise the
    operator sees two places that promise the same thing. The boundary is the settings page.
    """
    body = client.get("/ui/").text
    chat = body[body.index('id="view-chat"') : body.index('id="view-eval"')]
    for leaked in ("llm-model", "llm-base", "llm-key", "jev-url", "jev-model", "set-key", "key-label"):
        assert 'id="' + leaked + '"' not in chat, leaked

    settings = body[body.index('id="view-settings"') : body.index('id="dlg-newkb"')]
    for node in ("set-key", "set-kb", "llm-base", "llm-model", "llm-provider", "jev-url", "jev-model", "jev-provider"):
        assert 'id="' + node + '"' in settings, node
    assert 'id="dropzone"' not in settings and 'id="prompt"' not in settings


def test_settings_can_fetch_the_model_list_and_test_jev(client):
    """A URL + key + model can be tried before it is saved: that is the whole ask."""
    body = client.get("/ui/").text
    assert 'id="btn-llm-models"' in body and "Muat daftar model" in body
    assert 'id="llm-models"' in body and 'id="llm-model-filter"' in body
    assert 'id="btn-jev-probe"' in body and "Uji Jev" in body

    script = client.get("/ui/app.js").text
    assert 'api("POST", "/settings/llm/models"' in script
    assert 'api("POST", "/settings/jev/probe"' in script
    assert 'api("PUT", "/settings"' in script
    assert 'api("GET", "/settings"' in script


def test_every_input_in_the_settings_screen_has_a_label(client):
    """A settings screen nobody can navigate by keyboard is not finished."""
    body = client.get("/ui/").text
    settings = body[body.index('id="view-settings"') : body.index('id="dlg-newkb"')]
    labelled = set(re.findall(r'<label[^>]*for="([^"]+)"', settings))
    tags = re.findall(r"<(?:input|select)\b[^>]*>", settings)
    unlabelled = []
    for tag in tags:
        has_own_label = "aria-label" in tag or 'type="checkbox"' in tag  # switches carry their own text
        node = re.search(r'id="([^"]+)"', tag)
        if node and not has_own_label and node.group(1) not in labelled:
            unlabelled.append(node.group(1))
    assert not unlabelled, unlabelled


def test_console_lists_documents_and_scopes_questions(client):
    """The two controls the simple flow needs: a document list and a scoped question."""
    script = client.get("/ui/app.js").text
    assert '"/knowledge?limit=' in script          # list endpoint is called with a limit
    assert "document_ids" in script                # scope is sent when documents are picked
    assert "data-pick" in script                   # selection is per document row


def test_console_never_sends_a_client_chosen_tenant(client):
    """organization_id is the one field the UI must never put on the wire for data calls.

    One deliberate exception: the API-key panel, where an admin chooses the tenant of the
    *new* key via POST /settings/api-keys (the server refuses that unless the calling key
    carries "*"). So the field may appear inside ``createKey`` and nowhere else.
    """
    script = client.get("/ui/app.js").text
    head, _, tail = script.partition("async function createKey")
    inside, _, rest = tail.partition("async function revokeKey")
    assert inside, "createKey tidak ditemukan di app.js"
    # di dalam panel kunci, tenant kunci baru memang ditentukan admin
    assert '"organization_id"' in inside
    # di luar itu, field ini hanya boleh dibaca dari respons (entry.organization_id),
    # tidak pernah ditulis sebagai nama field di sebuah body.
    for outside in (head, rest):
        assert "organization_id:" not in outside
        assert '"organization_id"' not in outside


def test_console_calls_the_api_prefix_it_advertises(client):
    """Every path in app.js is relative to /api/v1; dropping the prefix answered 404 for
    every button (this actually happened: /health and /knowledge/index both 404'd)."""
    script = client.get("/ui/app.js").text
    assert 'location.origin + "/api/v1"' in script
    paths = re.findall(r"api\(\"[A-Z]+\",\s*\"([^\"]+)\"", script)
    assert paths, "no api() calls found - did the console stop calling the API?"
    # A path that already carries the prefix would be requested as /api/v1/api/v1/...
    assert all(not path.startswith("/api/") for path in paths), paths


def test_the_main_page_and_the_format_control_never_overlap(client):
    """Format berkas adalah kebijakan layanan: kontrolnya di Pengaturan, bukan di chat."""
    body = client.get("/ui/").text
    chat = body[body.index('id="view-chat"') : body.index('id="view-eval"')]
    for leaked in ("fmt-groups", "fmt-max", "btn-save-fmt"):
        assert 'id="' + leaked + '"' not in chat, leaked

    settings = body[body.index('id="view-settings"') : body.index('id="dlg-newkb"')]
    for node in ("fmt-groups", "fmt-max", "btn-save-fmt", "btn-fmt-all", "fmt-note"):
        assert 'id="' + node + '"' in settings, node
    assert "Format berkas" in settings


# --------------------------------------------------------------------------- #
# Pengaturan sebagai halaman + panel Kunci API
# --------------------------------------------------------------------------- #
def test_settings_is_a_scrollable_page_with_panels(client):
    """Pengaturan adalah halaman (bisa digulir sampai bawah), bukan dialog modal.

    Dialog modal lama memotong panel yang panjang: isinya tidak bisa digulir di layar kecil.
    """
    body = client.get("/ui/").text
    assert 'id="view-settings"' in body
    assert 'id="dlg-settings"' not in body, "dialog Pengaturan lama harus sudah tidak ada"
    for panel in ("conn", "keys", "llm", "jev", "fmt", "retr"):
        assert 'data-panel="' + panel + '"' in body, panel
    assert body.count('data-panel="conn" hidden') == 0

    script = client.get("/ui/app.js").text
    assert "selectPanel" in script and '"#/settings"' in script
    for panel in ("conn", "keys", "llm", "jev", "fmt", "retr"):
        assert panel in script


def test_every_knowledge_base_is_listed_and_can_be_opened(client):
    """Halaman Knowledge base: semua KB organisasi, bisa dicari dan dibuka satu per satu."""
    body = client.get("/ui/").text
    for node in ("view-kbs", "kb-grid", "kb-search", "btn-kb-new", "dlg-newkb", "kb-current"):
        assert 'id="' + node + '"' in body, node
    script = client.get("/ui/app.js").text
    assert 'api("GET", "/knowledge-bases")' in script
    assert '"#/kb/"' in script and "openKnowledgeBase" in script


def test_the_settings_page_manages_api_keys(client):
    """Panel Kunci API: buat, lihat, cabut - dan nilainya tidak bisa dibaca ulang."""
    body = client.get("/ui/").text
    settings = body[body.index('id="view-settings"') : body.index('id="dlg-newkb"')]
    for node in ("key-label", "key-expiry", "key-perm-read", "key-perm-write", "key-perm-admin",
                 "btn-create-key", "keys-rows", "key-value", "btn-copy-key", "btn-keys-refresh",
                 "keys-note", "keys-status"):
        assert 'id="' + node + '"' in settings, node
    # kunci penuh hanya ditampilkan sekali, sebagai nilai yang tidak bisa diketik ulang
    assert 'id="key-value" readonly' in settings
    # "read" selalu ikut dan dikunci; admin harus dipilih sadar
    assert 'id="key-perm-read" checked disabled' in settings
    assert 'id="key-perm-admin"' in settings and "checked" not in settings.partition('id="key-perm-admin"')[2][:60]

    script = client.get("/ui/app.js").text
    assert 'api("GET", "/settings/api-keys")' in script
    assert 'api("POST", "/settings/api-keys"' in script
    assert 'api("DELETE", "/settings/api-keys/"' in script
    # pencabutan dikonfirmasi dulu di browser, lalu diterapkan server
    assert "window.confirm" in script


def test_the_format_control_is_wired_to_the_settings_api(client):
    script = client.get("/ui/app.js").text
    assert "renderFormats" in script and "saveFormats" in script
    assert 'api("PUT", "/settings"' in script
    # katalog datang dari server, tidak pernah ditulis ulang di klien
    assert "sections.uploads" in script and "data.catalog" in script
    # berkas berformat yang tidak diizinkan ditolak di browser, sebelum diunggah
    assert "allowed_ext" in script and "tidak termasuk format yang diizinkan" in script
    assert 'setAttribute("accept"' in script


def test_the_format_list_shows_availability_and_never_offers_a_dead_switch(client):
    script = client.get("/ui/app.js").text
    # format tanpa dukungan di mesin ini: kotak dinonaktifkan dan alasannya ditampilkan
    assert "disabled" in script and "row.note" in script
    # batas ukuran ikut dikirim bersama daftar ekstensi
    assert "max_upload_mb" in script

