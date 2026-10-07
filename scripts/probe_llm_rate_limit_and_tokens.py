"""Buktikan dua akar kegagalan yang membuat "RAG tidak bisa menjawab" setelah ganti model:

1. **max_tokens terlalu kecil pada health check.** Model reasoning (seri fledge) menghabiskan
   token untuk berpikir; `max_tokens=4` membuat endpoint membalas 400 dan layar /ready
   melaporkan LLM bermasalah walau modelnya sehat.
2. **429 rate limit tanpa retry.** Model gratis membatasi permintaan; satu panggilan yang kena
   429 langsung menjadi 502 bagi pengguna, padahal percobaan ulang beberapa detik kemudian
   berhasil.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

llm = json.loads(Path("/data/settings.json").read_text()).get("llm") or {}
BASE = str(llm.get("base_url") or "").rstrip("/")
MODEL = str(llm.get("model") or "")
KEY = str(llm.get("api_key") or "")
URL = BASE + "/chat/completions"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}

print("model:", MODEL)

print("\n== 1. max_tokens kecil (persis seperti health check: 4) ==")
for mt in (4, 16, 64, 256):
    try:
        resp = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ping"}],
                                     "max_tokens": mt, "temperature": 0.0},
                          headers=HEADERS, timeout=60)
        detail = resp.text[:150].replace("\n", " ") if resp.status_code != 200 else "OK"
        print(f"  max_tokens={mt:4d} -> HTTP {resp.status_code} | {detail}")
    except Exception as exc:  # noqa: BLE001
        print(f"  max_tokens={mt:4d} -> {type(exc).__name__}: {str(exc)[:70]}")
    time.sleep(1)

print("\n== 2. jawaban sangat pendek dengan max_tokens kecil ==")
resp = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ping"}],
                             "max_tokens": 16, "temperature": 0.0}, headers=HEADERS, timeout=60)
if resp.status_code == 200:
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    print("  finish_reason:", choice.get("finish_reason"))
    print("  content      :", repr(str(message.get("content") or "")))
    print("  reasoning    :", repr(str(message.get("reasoning_content") or "")[:150]))

print("\n== 3. apakah 429 muncul pada burst? (meniru pengguna bertanya beruntun) ==")
codes: list[int] = []
for index in range(20):
    try:
        r = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ok"}],
                                  "max_tokens": 30}, headers=HEADERS, timeout=60)
        codes.append(r.status_code)
        if r.status_code != 200:
            print(f"  #{index + 1}: HTTP {r.status_code} | {r.text[:100]}")
    except Exception as exc:  # noqa: BLE001
        codes.append(-1)
        print(f"  #{index + 1}: {type(exc).__name__}")
print("  ringkasan kode:", {code: codes.count(code) for code in sorted(set(codes))})

print("\n== 4. apakah percobaan ulang menolong setelah 429? ==")
got_429 = False
for index in range(30):
    try:
        r = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ok"}],
                                  "max_tokens": 30}, headers=HEADERS, timeout=60)
        if r.status_code == 429:
            got_429 = True
            print(f"  kena 429 di percobaan #{index + 1}; menunggu 5 detik lalu coba lagi...")
            retry_after = r.headers.get("retry-after")
            print("  header Retry-After:", retry_after)
            time.sleep(5)
            r2 = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ok"}],
                                       "max_tokens": 30}, headers=HEADERS, timeout=60)
            print(f"  setelah menunggu: HTTP {r2.status_code} -> "
                  f"{'BERHASIL (retry menolong)' if r2.status_code == 200 else r2.text[:80]}")
            break
    except Exception:  # noqa: BLE001
        pass
if not got_429:
    print("  tidak kena 429 pada 30 permintaan cepat (batasnya mungkin per menit/kuota)")
