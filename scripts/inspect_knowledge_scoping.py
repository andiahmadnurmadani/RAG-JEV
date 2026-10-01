"""Read-only: buktikan dokumen tersimpan & jelaskan kenapa daftarnya bisa tampak kosong.

Tidak menulis apa pun. Hanya memanggil GET /knowledge dengan beberapa saringan.
"""

import json
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"


def active_key() -> str:
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
    raise SystemExit("kunci aktif tidak ditemukan")


def call(path: str, key: str) -> dict:
    request = urllib.request.Request(BASE + path, method="GET")
    request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return {"_error": error.code, "_body": error.read().decode()[:200]}


def main() -> int:
    key = active_key()
    print("== ringkasan kunci (tanpa nilai) ==")
    for path in (Path("/data/api_keys.json"),):
        data = json.loads(path.read_text())
        records = data.get("keys") if isinstance(data, dict) else data
        items = records.values() if isinstance(records, dict) else (records or [])
        for record in items:
            print(
                "  kunci key_id={} label={!r} org={} revoked={} last_used={}".format(
                    record.get("key_id"),
                    record.get("label"),
                    record.get("organization_id"),
                    bool(record.get("revoked_at")),
                    record.get("last_used_at"),
                )
            )

    print("\n== tanpa saringan kb (semua dokumen organisasi ini) ==")
    alles = (call("/knowledge?limit=200", key).get("data") or {}).get("documents") or []
    for row in alles:
        print("  - {} | kb={} | {}".format(row.get("document_name"), row.get("knowledge_base_id"), row.get("status")))
    print("jumlah:", len(alles))

    print("\n== dengan saringan kb bawaan UI (kb_chat) ==")
    chat = (call("/knowledge?limit=200&knowledge_base_id=kb_chat", key).get("data") or {}).get("documents") or []
    print("jumlah:", len(chat))

    print("\n== daftar knowledge base yang benar-benar terpakai ==")
    kbs: dict = {}
    for row in alles:
        kbs.setdefault(row.get("knowledge_base_id") or "(kosong)", []).append(row.get("document_name"))
    for kb, names in sorted(kbs.items()):
        print("  {} -> {} dokumen: {}".format(kb, len(names), ", ".join(names[:3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
