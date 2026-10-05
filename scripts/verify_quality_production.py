"""Verifikasi produksi: reranker aktif dan jawaban bersih dari kata rusak.

Dijalankan DI DALAM container; kunci dibaca dari /data dan tidak pernah dicetak.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"


def any_key() -> str:
    for path in (Path("/data/api_keys.json"), Path("/data/bootstrap_admin_key.json")):
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if isinstance(data, dict) and data.get("key"):
            return data["key"]
        records = data.get("keys") if isinstance(data, dict) else data
        items = records.values() if isinstance(records, dict) else (records or [])
        for record in items:
            if not record.get("revoked_at") and not record.get("revoked") and record.get("key"):
                return record["key"]
    raise SystemExit("tidak ada kunci")


def call(method: str, path: str, payload=None, token: str = "") -> tuple[int, str]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


KATA_RUSAK = re.compile(r"[A-Za-z][a-z]*[A-Z]{2,}[A-Za-z]*|[A-Za-z]{2,}\.[A-Za-z]{2,}|([aeiou])\1[aeiou]")


def main() -> int:
    token = any_key()

    print("== 1. setelan kualitas yang aktif ==")
    status, body = call("GET", "/settings", token=token)
    try:
        sections = json.loads(body)["data"]["sections"]
        llm = sections["llm"]
        retrieval = sections["retrieval"]
        print("  llm.temperature      :", llm.get("temperature"))
        print("  llm.top_p            :", llm.get("top_p"))
        print("  llm.frequency_penalty:", llm.get("frequency_penalty"))
        print("  llm.repair_attempts  :", llm.get("repair_attempts"))
        print("  reranker_provider    :", retrieval.get("reranker_provider"))
        print("  reranker_enabled     :", retrieval.get("reranker_enabled"))
    except Exception as exc:  # noqa: BLE001
        print("  GAGAL membaca setelan:", exc, body[:200])
        return 1

    print("\n== 2. cari dengan reranker ==")
    status, body = call("POST", "/search", {
        "query": "cuti tahunan",
        "knowledge_base_id": "kb_chat",
        "options": {"top_k": 3, "use_reranker": True},
    }, token)
    try:
        data = json.loads(body)["data"]
        print("  reranker dilaporkan:", data.get("reranker"))
        results = data.get("results") or []
        print("  jumlah hasil:", len(results))
        for rank, hit in enumerate(results[:3], 1):
            print(f"    {rank}. [{hit.get('score'):.3f}] {(hit.get('content') or '')[:70]}")
    except Exception:  # noqa: BLE001
        print("  HTTP", status, body[:250])

    print("\n== 3. tanya & periksa kata rusak ==")
    status, body = call("POST", "/query", {
        "query": "Apa isi knowledge base ini? Sebutkan singkat dan rapi.",
        "knowledge_base_id": "kb_chat",
        "options": {"top_k": 4},
    }, token)
    try:
        data = json.loads(body)["data"]
        answer = (data.get("answer") or "").strip()
        usage = data.get("usage") or {}
        print("  HTTP", status, "| finish_reason:", usage.get("finish_reason"), "| panjang:", len(answer))
        for line in answer.splitlines()[:10]:
            print("   |", line[:130])
        # findall dengan grup mengembalikan '' saat alternasi pertama cocok - pakai finditer
        # supaya kata yang benar-benar cocok terlihat, bukan daftar string kosong.
        rusak = [m.group(0) for m in KATA_RUSAK.finditer(answer)]
        print("  cocok regex kasar   :", rusak[:10] or "tidak ada", "(bisa positif palsu: domain/akronim)")
        # Sekaligus pakai detektor sungguhan dari kode produksi.
        try:
            import sys
            # Di image, /srv/app adalah ISI paket `app` (bukan repo root) - jadi /srv yang
            # ditambahkan ke sys.path, bukan /srv/app.
            if "/srv" not in sys.path:
                sys.path.insert(0, "/srv")
            from app.core.config import get_settings
            from app.rag.generator import Generator
            det = Generator(get_settings())._corrupted_words(answer, answer)
            print("  detektor internal    :", sorted(det)[:10] or "tidak ada")
        except Exception as exc:  # noqa: BLE001
            print("  detektor internal    : (gagal)", type(exc).__name__)
    except Exception:  # noqa: BLE001
        print("  HTTP", status, body[:250])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
