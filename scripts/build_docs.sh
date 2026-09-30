#!/usr/bin/env bash
# Bangun situs dokumentasi (MkDocs Material) + segarkan referensi API dari kode.
#
#   bash scripts/build_docs.sh
#
# Hasil: site/  (siap disajikan aplikasi di /guide, nginx, atau hosting statis)
set -euo pipefail
cd "$(dirname "$0")/.."

# --- pilih interpreter proyek ------------------------------------------------
PY=""
for cand in .venv/Scripts/python.exe .venv/bin/python python3 python; do
  if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "python tidak ditemukan" >&2; exit 1; fi
echo "interpreter : $PY"

# --- 1. segarkan spesifikasi + referensi API (satu sumber kebenaran: kode) ---
echo "1/3 ekspor OpenAPI ..."
"$PY" scripts/export_openapi.py
echo "2/3 generate referensi API ..."
"$PY" scripts/gen_api_reference.py

# --- 2. bangun situs ---------------------------------------------------------
echo "3/3 mkdocs build ..."
if "$PY" -c "import mkdocs" >/dev/null 2>&1; then
  "$PY" -m mkdocs build --strict
elif command -v uvx >/dev/null 2>&1; then
  # perkakas dokumen tidak dipasang di venv proyek: jalankan di lingkungan terpisah
  uvx --from mkdocs --with mkdocs-material mkdocs build --strict
elif command -v mkdocs >/dev/null 2>&1; then
  mkdocs build --strict
else
  echo "mkdocs tidak ada. Pasang dulu:  uv pip install -r requirements-docs.txt" >&2
  exit 1
fi

# --- 3. laporan --------------------------------------------------------------
if [ ! -f site/index.html ]; then
  echo "build selesai tetapi site/index.html tidak ada — periksa keluaran di atas" >&2
  exit 1
fi
pages=$(find site -name '*.html' | wc -l | tr -d ' ')
size=$(du -sh site 2>/dev/null | cut -f1)
echo
echo "OK: site/ siap — ${pages} halaman, ${size:-?}"
echo "   sajikan dari aplikasi : set DOCS_SITE_DIR=site lalu buka http://<host>:<port>/guide/"
echo "   pratinjau lokal       : mkdocs serve   (http://127.0.0.1:8001)"
