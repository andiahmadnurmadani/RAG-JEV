#!/usr/bin/env python3
"""Buat halaman referensi API (Markdown) dari spesifikasi OpenAPI.

    python scripts/gen_api_reference.py
    python scripts/gen_api_reference.py --spec openapi.json --out docs/api-reference.md

Sengaja **generik**: masukan apa pun yang berbentuk OpenAPI 3.x (bukan hanya proyek ini),
sehingga bisa dipakai ulang oleh aplikasi lain (lihat docs/reuse.md).

Berkas keluaran adalah hasil generate — jangan disunting manual.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
BANNER = ("<!-- BERKAS HASIL GENERATE: dibuat oleh scripts/gen_api_reference.py "
          "dari {spec}. Jangan disunting manual. -->")


def load(spec_path: Path) -> Dict[str, Any]:
    text = spec_path.read_text(encoding="utf-8")
    if spec_path.suffix in {".yaml", ".yml"}:
        import yaml  # hanya dibutuhkan untuk berkas YAML

        return yaml.safe_load(text)
    return json.loads(text)


def resolve(spec: Dict[str, Any], node: Any, depth: int = 0) -> Any:
    """Ikuti $ref (maksimum beberapa tingkat) supaya tabel bisa dibaca manusia."""
    while isinstance(node, dict) and "$ref" in node and depth < 8:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node, depth = target, depth + 1
    return node


def ref_name(node: Any) -> Optional[str]:
    if isinstance(node, dict) and "$ref" in node:
        return node["$ref"].split("/")[-1]
    return None


def type_of(spec: Dict[str, Any], schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return ref_name(schema) or "object"
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and isinstance(schema[key], list):
            parts = [type_of(spec, s) for s in schema[key]]
            return " | ".join(dict.fromkeys(parts))
    t = schema.get("type")
    if t == "array":
        return f"array<{type_of(spec, schema.get('items', {}))}>"
    if t is None and "properties" in schema:
        return "object"
    if t == "string" and schema.get("enum"):
        return "enum(" + ", ".join(f"`{v}`" for v in schema["enum"][:8]) + ")"
    if t == "string" and schema.get("format"):
        return f"{t}({schema['format']})"
    return str(t or "any")


def props_table(spec: Dict[str, Any], schema: Any) -> List[str]:
    schema = resolve(spec, schema)
    props = (schema or {}).get("properties") or {}
    if not props:
        return []
    required = set((schema or {}).get("required") or [])
    lines = ["| Field | Tipe | Wajib | Keterangan |", "|---|---|---|---|"]
    for name, sub in props.items():
        desc = ""
        resolved = resolve(spec, sub)
        if isinstance(resolved, dict):
            desc = (resolved.get("description") or "").replace("\n", " ").strip()
            if resolved.get("default") is not None:
                desc = (desc + f" (default: `{resolved['default']}`)").strip()
        lines.append(f"| `{name}` | {type_of(spec, sub)} | {'ya' if name in required else '—'} | {desc} |")
    return lines


def endpoint_block(spec: Dict[str, Any], method: str, path: str, op: Dict[str, Any]) -> List[str]:
    out: List[str] = [f"### `{method.upper()} {path}`", ""]
    if op.get("summary"):
        out += [f"**{op['summary']}**", ""]
    if op.get("description"):
        out += [op["description"].strip(), ""]
    if op.get("tags"):
        out += [f"Tag: {', '.join(op['tags'])}", ""]

    params = op.get("parameters") or []
    if params:
        out += ["| Parameter | Di | Tipe | Wajib | Keterangan |", "|---|---|---|---|---|"]
        for p in params:
            p = resolve(spec, p)
            out.append(
                f"| `{p.get('name')}` | {p.get('in')} | {type_of(spec, p.get('schema', {}))} | "
                f"{'ya' if p.get('required') else '—'} | {(p.get('description') or '').replace(chr(10), ' ')} |"
            )
        out.append("")

    body = (op.get("requestBody") or {}).get("content") or {}
    if body:
        for media, spec_body in body.items():
            out += [f"Body `{media}`:", ""]
            schema = spec_body.get("schema", {})
            resolved = resolve(spec, schema)
            title = ref_name(schema) or resolved.get("title") or ""
            if title:
                out += [f"Skema: `{title}`", ""]
            table = props_table(spec, schema)
            out += table if table else ["_(tanpa properti)_"]
            out.append("")

    responses = op.get("responses") or {}
    if responses:
        out += ["| Kode | Arti |", "|---|---|"]
        for code, resp in responses.items():
            resp = resolve(spec, resp)
            out.append(f"| {code} | {(resp.get('description') or '').replace(chr(10), ' ')} |")
        out.append("")
    out.append("")
    return out


def render(spec: Dict[str, Any], spec_name: str) -> str:
    info = spec.get("info") or {}
    out: List[str] = [
        BANNER.format(spec=spec_name),
        "",
        "# Referensi API (hasil generate)",
        "",
        f"Berkas ini dibuat dari `{spec_name}` — **jangan disunting manual**.",
        "",
        f"- Judul: **{info.get('title', '')}**",
        f"- Versi: `{info.get('version', '')}` · OpenAPI `{spec.get('openapi', '')}`",
    ]
    for server in spec.get("servers") or []:
        out.append(f"- Server: `{server.get('url', '')}`")
    if info.get("description"):
        out += ["", info["description"].strip()]
    out += ["", "Untuk mencoba langsung dari browser: [Swagger UI](/docs) · [ReDoc](/redoc) · "
                "[`openapi.json`](openapi.json) · [`openapi.yaml`](openapi.yaml)", ""]

    schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    if schemes:
        out += ["## Autentikasi", ""]
        for name, scheme in schemes.items():
            out.append(f"- `{name}`: {scheme.get('type')} "
                       f"{scheme.get('scheme', '')} {scheme.get('description', '') or ''}".rstrip())
        out.append("")

    paths = spec.get("paths") or {}
    out += ["## Daftar endpoint", "", "| Metode | Path | Ringkasan |", "|---|---|---|"]
    for path in sorted(paths):
        for method, op in sorted(paths[path].items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            out.append(f"| `{method.upper()}` | `{path}` | {(op.get('summary') or '').strip()} |")
    out += ["", "Total: "
                f"{sum(1 for p in paths.values() for m in p if m.lower() in {'get','post','put','patch','delete'})} "
                "operasi.", ""]

    out += ["## Detail", ""]
    for path in sorted(paths):
        out += [f"## `{path}`", ""]
        for method, op in sorted(paths[path].items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            out += endpoint_block(spec, method, path, op)

    schemas = ((spec.get("components") or {}).get("schemas") or {})
    if schemas:
        out += ["## Skema", ""]
        for name in sorted(schemas):
            out += [f"### `{name}`", ""]
            table = props_table(spec, schemas[name])
            out += table if table else ["_(tanpa properti)_"]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate referensi API dari OpenAPI")
    parser.add_argument("--spec", default="docs/openapi.json", help="berkas OpenAPI (json/yaml)")
    parser.add_argument("--out", default="docs/api-reference.md", help="berkas Markdown keluaran")
    args = parser.parse_args()

    spec_path = (ROOT / args.spec).resolve()
    out_path = (ROOT / args.out).resolve()
    if not spec_path.exists():
        print(f"spesifikasi tidak ditemukan: {spec_path}\nJalankan dulu: python scripts/export_openapi.py")
        return 1

    spec = load(spec_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(spec, args.spec), encoding="utf-8")
    n_ops = sum(1 for p in (spec.get("paths") or {}).values()
                for m in p if m.lower() in {"get", "post", "put", "patch", "delete"})
    print(f"OK: {out_path.relative_to(ROOT)} — {n_ops} operasi, "
          f"{len((spec.get('components') or {}).get('schemas') or {})} skema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
