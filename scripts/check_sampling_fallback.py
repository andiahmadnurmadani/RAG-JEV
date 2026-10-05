"""Buktikan parameter sampling adaptif: bila gateway menolak top_p, permintaan tetap berhasil.

Kasus nyata: gateway yang dipakai membalas HTTP 400 saat menerima `top_p`. Tanpa penanganan ini,
seluruh jawaban gagal (502) hanya karena satu parameter opsional.
"""

from __future__ import annotations

import os
import tempfile

os.environ.update({
    "APP_ENV": "test",
    "LLM_PROVIDER": "openai_compatible",
    "LLM_BASE_URL": "http://contoh.invalid/v1",
    "LLM_API_KEY": "kunci-uji",
    "EMBEDDING_PROVIDER": "hash",
    "QDRANT_URL": "",
    "JEV_ENABLED": "false",
    "QDRANT_LOCAL_PATH": os.path.join(tempfile.mkdtemp(), "q"),
})

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.rag import generator as G  # noqa: E402

settings = get_settings()
sent: list[dict] = []


class _Ok:
    status_code = 200

    @staticmethod
    def json():
        return {
            "choices": [{"message": {"content": "jawaban [1]"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "model": "uji",
        }

    @staticmethod
    def raise_for_status():
        return None


def fake_post(url, json=None, headers=None, timeout=None):
    sent.append(dict(json or {}))
    if "top_p" in (json or {}):
        request = httpx.Request("POST", url)
        response = httpx.Response(400, request=request, text="insufficient credits")
        raise httpx.HTTPStatusError("Client error '400 Bad Request'", request=request, response=response)
    return _Ok()


httpx.post = fake_post  # type: ignore[assignment]

client = G.LLMClient(settings)
text, usage = client.chat([{"role": "user", "content": "halo"}])

print("jumlah percobaan :", len(sent))
print("percobaan 1 punya top_p :", "top_p" in sent[0])
print("percobaan terakhir      :", sorted(sent[-1].keys()))
print("jawaban tetap keluar    :", repr(text))
print("frequency_penalty tetap dikirim:", "frequency_penalty" in sent[-1])

gagal = 0
if len(sent) < 2:
    print("GAGAL: tidak ada percobaan ulang tanpa parameter yang ditolak")
    gagal += 1
if "top_p" in sent[-1]:
    print("GAGAL: top_p masih dikirim di percobaan terakhir")
    gagal += 1
if text != "jawaban [1]":
    print("GAGAL: jawaban tidak keluar")
    gagal += 1
print("\nSEMUA LULUS" if gagal == 0 else f"\n{gagal} GAGAL")
raise SystemExit(0 if gagal == 0 else 1)
