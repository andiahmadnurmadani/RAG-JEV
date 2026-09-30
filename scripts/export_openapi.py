#!/usr/bin/env python3
"""Ekspor spesifikasi OpenAPI dari aplikasi (tanpa menjalankan server).

    python scripts/export_openapi.py                       # tulis docs/openapi.json + .yaml
    python scripts/export_openapi.py --out-dir docs        # lokasi lain
    python scripts/export_openapi.py --check               # gagal bila berkas di disk berbeda

Dipanggil oleh scripts/build_docs.sh sebelum situs dibangun, supaya referensi API
di dokumentasi tidak pernah menyimpang dari kode.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build_spec() -> dict:
    """Bangun skema OpenAPI dari instance aplikasi (lifespan tidak dijalankan)."""
    from app.main import app  # noqa: WPS433 (impor di dalam fungsi: butuh sys.path siap)

    return app.openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ekspor OpenAPI dari aplikasi")
    parser.add_argument("--out-dir", default="docs", help="direktori keluaran (default: docs)")
    parser.add_argument("--check", action="store_true",
                        help="bandingkan dengan berkas di disk, keluar 1 bila berbeda")
    args = parser.parse_args()

    out_dir = (ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "openapi.json"
    yaml_path = out_dir / "openapi.yaml"

    spec = build_spec()
    rendered_json = json.dumps(spec, indent=2, ensure_ascii=False) + "\n"

    if args.check:
        current = json_path.read_text(encoding="utf-8") if json_path.exists() else ""
        if current != rendered_json:
            print("BEDA: docs/openapi.json tidak sama dengan skema aplikasi — jalankan tanpa --check")
            return 1
        print("sama: docs/openapi.json sudah sinkron dengan aplikasi")
        return 0

    json_path.write_text(rendered_json, encoding="utf-8")
    n_paths = len(spec.get("paths", {}))
    n_schemas = len((spec.get("components") or {}).get("schemas", {}))

    try:
        import yaml  # pyyaml ada di venv runtime; kalau tidak ada, JSON tetap ditulis
    except ImportError:
        yaml = None
    if yaml is not None:
        yaml_path.write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )

    print(f"OK: {json_path.relative_to(ROOT)}" + (f" + {yaml_path.name}" if yaml else " (pyyaml tidak ada)"))
    print(f"    {n_paths} path, {n_schemas} skema, OpenAPI {spec.get('openapi')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
