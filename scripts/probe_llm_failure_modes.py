"""Selidiki persis seperti yang dilakukan layanan: httpx, header, dan pola galat 429/400.

Tujuan: memisahkan tiga hal yang terlihat sama dari luar ("RAG tidak bisa menjawab"):
1. rate limit 429 - model gratis dibatasi;
2. 400 - parameter atau bentuk permintaan ditolak;
3. model reasoning yang mengembalikan content KOSONG (jawaban ada di reasoning_content).
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

print("model:", MODEL, "| url:", URL)

# Header persis seperti yang dikirim layanan sekarang (tanpa User-Agent khusus).
LAYANAN_HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}
print("\n== 1. apakah httpx default diterima? ==")
r = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20},
               headers=LAYANAN_HEADERS, timeout=60)
print("  status:", r.status_code, "| UA yang terkirim: python-httpx (default)")
if r.status_code != 200:
    print("  isi:", r.text[:200])

print("\n== 2. apakah parameter sampling ditolak (400)? ==")
for label, extra in (
    ("dasar", {}),
    ("+ temperature", {"temperature": 0.1}),
    ("+ top_p", {"top_p": 0.9}),
    ("+ frequency_penalty", {"frequency_penalty": 0.2}),
    ("+ keduanya", {"temperature": 0.1, "top_p": 0.9, "frequency_penalty": 0.2}),
):
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Balas: ok"}], "max_tokens": 30, **extra}
    try:
        resp = httpx.post(URL, json=body, headers=LAYANAN_HEADERS, timeout=90)
        note = resp.text[:110].replace("\n", " ")
        print(f"  {label:22s} -> HTTP {resp.status_code} | {note}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:22s} -> {type(exc).__name__}: {str(exc)[:80]}")
    time.sleep(1)

print("\n== 3. berapa cepat kena 429 (rate limit)? ==")
ok = bad = 0
for index in range(12):
    try:
        resp = httpx.post(URL, json={"model": MODEL, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 10},
                          headers=LAYANAN_HEADERS, timeout=60)
        if resp.status_code == 200:
            ok += 1
        else:
            bad += 1
            print(f"  permintaan #{index + 1}: HTTP {resp.status_code} | {resp.text[:90]}")
    except Exception as exc:  # noqa: BLE001
        bad += 1
        print(f"  permintaan #{index + 1}: {type(exc).__name__}")
print(f"  hasil: {ok} sukses, {bad} gagal dari 12 permintaan cepat")

print("\n== 4. model reasoning: content kosong? ==")
prompt = (
    "You are an organizational knowledge assistant. Answer in Markdown.\n"
    "Cite sources with the bracketed context number.\n\n"
    "RETRIEVED_CONTEXT:\n[1] Laporan tahunan bank bjb 2025: total aset 100 triliun rupiah, "
    "laba bersih 2 triliun rupiah.\n\nUser query:\nBerapa total aset dan laba bersih bank bjb?"
)
for max_tokens in (256, 1024, 4096):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    try:
        resp = httpx.post(URL, json=body, headers=LAYANAN_HEADERS, timeout=200)
        if resp.status_code != 200:
            print(f"  max_tokens={max_tokens:5d} -> HTTP {resp.status_code} | {resp.text[:90]}")
            continue
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
        usage = data.get("usage") or {}
        print(f"  max_tokens={max_tokens:5d} finish={choice.get('finish_reason')!r} "
              f"content={len(content)} reasoning={len(reasoning)} "
              f"out_tokens={usage.get('completion_tokens')}")
        print(f"     content   : {content[:150]!r}")
        if not content and reasoning:
            print(f"     reasoning : {reasoning[:150]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  max_tokens={max_tokens:5d} -> {type(exc).__name__}: {str(exc)[:80]}")
    time.sleep(1)
