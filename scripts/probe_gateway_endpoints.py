"""Periksa endpoint gateway: apakah ada /rerank dan /embeddings (read-only).

Kunci dibaca dari /data/settings.json di dalam container; tidak pernah dicetak.
"""

from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    settings_path = Path("/data/settings.json")
    if not settings_path.exists():
        print("settings.json tidak ada")
        return 1
    data = json.loads(settings_path.read_text())
    llm = data.get("llm") or {}
    base_url = str(llm.get("base_url") or "").rstrip("/")
    api_key = str(llm.get("api_key") or "")
    model = str(llm.get("model") or "")
    print("base_url :", base_url)
    print("model    :", model)
    print("kunci    :", "ada" if api_key else "KOSONG")

    import httpx

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    probes = [
        ("GET", "/models", None),
        ("POST", "/embeddings", {"model": model, "input": "uji embedding"}),
        ("POST", "/rerank", {"model": model, "query": "cuti tahunan", "documents": ["cuti tahunan 12 hari", "gaji pokok"], "top_n": 2}),
        ("POST", "/reranking", {"model": model, "query": "cuti", "documents": ["cuti 12 hari"]}),
    ]
    for method, path, payload in probes:
        url = base_url + path
        try:
            if method == "GET":
                response = httpx.get(url, headers=headers, timeout=20)
            else:
                response = httpx.post(url, headers=headers, json=payload, timeout=30)
            body = response.text[:200].replace("\n", " ")
            print(f"  {method} {path:12s} -> HTTP {response.status_code} | {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {method} {path:12s} -> {type(exc).__name__}: {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
