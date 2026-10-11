"""Satu password untuk membuka halaman web (konsol /ui, panduan /guide, /docs).

Lapisan ini hanya menjaga *halaman* yang dibuka lewat peramban. Semua jalur API
(``/api/v1/...``) sengaja tidak tersentuh: aplikasi yang memanggil API tetap memakai API
key seperti biasa, dan healthcheck ``/api/v1/health`` tetap terbuka untuk orkestrator.

Password tidak pernah disimpan sebagai teks: yang ada di konfigurasi adalah hash
PBKDF2-SHA256. Setelah password benar, peramban menerima cookie bertanda tangan HMAC
(berlaku ``site_gate_days`` hari). Kunci tanda tangannya diturunkan dari rahasia acak di
berkas data *dan* hash password, jadi mengganti password otomatis mengeluarkan semua
peramban yang sudah masuk.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote

from app.core.access import LoginThrottle, _path_for

COOKIE_NAME = "rag_site"
LOGIN_PATH = "/site-login"
LOGOUT_PATH = "/site-logout"
DEFAULT_NEXT = "/ui/"
PBKDF2_ITERATIONS = 600_000
MAX_FORM_BYTES = 8 * 1024
DISABLED_VALUES = {"", "off", "false", "disabled", "none", "0"}

# Percobaan password per alamat klien, ditambah batas global agar tebakan dari banyak
# alamat sekaligus tetap lambat.
THROTTLE = LoginThrottle(max_failures=8, window=300.0)
GLOBAL_THROTTLE = LoginThrottle(max_failures=40, window=300.0)
GLOBAL_KEY = "__all_clients__"


def hash_password(password: str, *, salt: Optional[str] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


def safe_next(raw: Optional[str]) -> str:
    """Hanya jalur lokal: cegah open redirect (``//evil.com``, ``/\\evil.com``, ``https:``)."""
    value = (raw or "").strip()
    if not value.startswith("/") or value.startswith("//") or value.startswith("/\\") or "\r" in value or "\n" in value:
        return DEFAULT_NEXT
    if value.startswith(LOGIN_PATH) or value.startswith(LOGOUT_PATH):
        return DEFAULT_NEXT
    return value


class SiteGate:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        api_prefix = str(getattr(settings, "api_prefix", "/api/v1") or "/api/v1").rstrip("/")
        # Seluruh /api tidak dijaga: dipakai aplikasi lain dengan API key, dan healthcheck.
        self._exempt_prefix = "/" + api_prefix.strip("/").split("/")[0] if api_prefix else "/api"
        plain = str(getattr(settings, "site_password", "") or "")
        configured = str(getattr(settings, "site_password_hash", "") or "").strip()
        if plain:
            self.password_hash: Optional[str] = hash_password(plain)
        elif configured.lower() in DISABLED_VALUES:
            self.password_hash = None
        else:
            self.password_hash = configured
        self.max_age = max(1, int(getattr(settings, "site_gate_days", 30) or 30)) * 86400
        self._key: Optional[bytes] = None

    @property
    def enabled(self) -> bool:
        return bool(self.password_hash)

    def is_exempt(self, path: str) -> bool:
        return path == self._exempt_prefix or path.startswith(self._exempt_prefix + "/")

    # ------------------------------------------------------------------ cookie
    def _secret(self) -> bytes:
        """Rahasia acak per pemasangan (berkas 0600 di samping access.json)."""
        path = _path_for(self._settings, "site_gate_secret_path", "site_gate_secret")
        try:
            return bytes.fromhex(path.read_text().strip())
        except (OSError, ValueError):
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(32)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(value)
        except FileExistsError:
            return bytes.fromhex(path.read_text().strip())
        return bytes.fromhex(value)

    def _signing_key(self) -> bytes:
        if self._key is None:
            self._key = hmac.new(self._secret(), (self.password_hash or "").encode(), hashlib.sha256).digest()
        return self._key

    def issue(self) -> str:
        expires = int(time.time()) + self.max_age
        body = f"{expires}.{secrets.token_hex(8)}"
        signature = hmac.new(self._signing_key(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        try:
            expires, nonce, signature = token.split(".")
            if int(expires) < time.time():
                return False
        except ValueError:
            return False
        expected = hmac.new(self._signing_key(), f"{expires}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


def parse_form(body: bytes) -> dict:
    data = parse_qs(body[:MAX_FORM_BYTES].decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in data.items() if values}


def login_page(next_path: str, error: str = "") -> str:
    message = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Masuk · RAG</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --fg:#1d2330; --muted:#667085; --line:#d9dde5; --accent:#2f6fed; --err:#c4320a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1218; --card:#171b23; --fg:#e6e9ef; --muted:#98a2b3; --line:#2a303c; --accent:#5b8cff; --err:#ff8a65; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; padding:16px; }}
main {{ width:100%; max-width:360px; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:28px; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
p.sub {{ margin:0 0 20px; color:var(--muted); font-size:13px; }}
label {{ display:block; font-size:13px; color:var(--muted); margin-bottom:6px; }}
input {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:9px; background:transparent; color:var(--fg); font-size:15px; }}
input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
button {{ margin-top:16px; width:100%; padding:10px; border:0; border-radius:9px; background:var(--accent); color:#fff; font-size:15px; font-weight:600; cursor:pointer; }}
.err {{ color:var(--err); font-size:13px; margin:12px 0 0; }}
</style>
</head>
<body>
<main>
  <h1>RAG Service</h1>
  <p class="sub">Masukkan password untuk membuka halaman ini.</p>
  <form method="post" action="{LOGIN_PATH}">
    <input type="hidden" name="next" value="{html.escape(next_path)}">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
    {message}
    <button type="submit">Masuk</button>
  </form>
</main>
</body>
</html>"""


def login_redirect_target(path: str, query: str) -> str:
    target = path + (f"?{query}" if query else "")
    return f"{LOGIN_PATH}?next={quote(target, safe='/')}"
