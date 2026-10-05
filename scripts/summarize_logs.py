"""Ringkas log container: rentang waktu, jumlah baris, tingkat keparahan, dan contoh galat.

Dijalankan DI DALAM container (atau di host dengan akses ke berkas log) supaya operator bisa
menjawab "log-nya ada di mana dan isinya apa" tanpa membaca 700 baris JSON satu per satu.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/app.log")
if not path.exists():
    raise SystemExit(f"berkas log tidak ada: {path}")

levels: Counter[str] = Counter()
loggers: Counter[str] = Counter()
first = last = None
errors: list[dict] = []
total = 0

for raw in path.read_text(errors="replace").splitlines():
    total += 1
    # Format docker json-file: {"log": "...", "time": "..."} - log bisa berupa baris JSON app.
    stamp = None
    line = raw
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict) and "log" in outer:
            line = outer["log"]
            stamp = outer.get("time")
    except Exception:  # noqa: BLE001
        pass
    if stamp:
        first = first or stamp
        last = stamp
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        record = json.loads(line)
    except Exception:  # noqa: BLE001
        continue
    level = record.get("level", "?")
    levels[level] += 1
    loggers[record.get("logger", "?")] += 1
    if level in {"ERROR", "CRITICAL", "WARNING"}:
        errors.append(record)

print(f"berkas      : {path}")
print(f"ukuran      : {path.stat().st_size / 1024:.1f} KB")
print(f"total baris : {total}")
print(f"rentang     : {first}  ->  {last}")
if last:
    try:
        umur = datetime.now(timezone.utc) - datetime.fromisoformat(last.replace("Z", "+00:00"))
        print(f"baris akhir : {umur.total_seconds() / 60:.1f} menit lalu")
    except Exception:  # noqa: BLE001
        pass
print(f"tingkat     : {dict(levels)}")
print("logger tersibuk:")
for name, count in loggers.most_common(6):
    print(f"  {count:5d}  {name}")

print(f"\nperingatan/galat ({len(errors)}):")
for record in errors[-12:]:
    msg = str(record.get("message", ""))[:110]
    print(f"  [{record.get('level')}] {record.get('ts', '?')} {record.get('logger', '?')}: {msg}")
if not errors:
    print("  tidak ada")
