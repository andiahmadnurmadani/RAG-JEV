#!/usr/bin/env bash
# Verifikasi bahwa layanan benar-benar menyajikan dokumentasi & UI-nya:
#   /guide/ (situs MkDocs), /docs + /redoc (Swagger/ReDoc), /openapi.json, /ui/, dan rute API.
#
# Dijalankan pada PORT terpisah dengan penyimpanan terpisah supaya instans yang sedang
# jalan tidak terganggu. Hasil rinci ada di docs/verification.md bagian 9.
#
#   bash scripts/check_docs_serve.sh            # port 8100, DOCS_SITE_DIR=site
#   PORT=8111 bash scripts/check_docs_serve.sh
#
# Prasyarat: site/ sudah dibangun (bash scripts/build_docs.sh).
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8100}"
D="${CHECK_DIR:-data/tmp/docscheck}"
SITE_DIR="${DOCS_SITE_DIR:-site}"

if [ ! -f "$SITE_DIR/index.html" ]; then
  echo "site/ belum dibangun — jalankan dulu: bash scripts/build_docs.sh" >&2
  exit 1
fi

PY=""
for cand in .venv/Scripts/python.exe .venv/bin/python python3 python; do
  if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || { echo "python tidak ditemukan" >&2; exit 1; }

mkdir -p "$D"
export QDRANT_URL=""
export QDRANT_LOCAL_PATH="$D/qdrant"
export QDRANT_COLLECTION="docscheck_chunks"
export SPARSE_DIR="$D/sparse" STORAGE_DIR="$D/storage" REGISTRY_PATH="$D/registry.json"
export JOB_STORE_PATH="$D/jobs.json" TABLE_STORE_PATH="$D/tables.sqlite" SETTINGS_OVERRIDE_PATH="$D/settings.json"
export DOCS_SITE_DIR="$SITE_DIR"
export EMBEDDING_PROVIDER="hash" RERANKER_PROVIDER="none" RERANKER_ENABLED="false"
export LLM_PROVIDER="mock" JEV_ENABLED="false"
export APP_PORT="$PORT" RATE_LIMIT_PER_MINUTE=600

"$PY" -m app.main > "$D/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT

fail=0
for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/health" || true)
  [ "$code" = "200" ] && break
  sleep 1
done

check() { # label path pola-yang-harus-ada
  local label="$1" path="$2" pat="$3" code
  code=$(curl -s -o "$D/body.tmp" -w '%{http_code}' "http://127.0.0.1:$PORT$path")
  if [ "$code" = "200" ] && grep -q "$pat" "$D/body.tmp" 2>/dev/null; then
    echo "  PASS $label — HTTP $code"
  else
    echo "  FAIL $label — HTTP $code | '$pat' tidak ditemukan"
    fail=1
  fi
}

echo "== dokumentasi & UI (port $PORT, site=$SITE_DIR) =="
check "GET /guide/ (situs MkDocs)"          "/guide/" "RAG Service"
check "GET /guide/integration/"             "/guide/integration/" "Integrasi klien"
check "GET /guide/api-reference/"           "/guide/api-reference/" "Referensi API"
check "GET /guide/deployment/"              "/guide/deployment/" "systemd"
check "GET /guide/openapi.json"             "/guide/openapi.json" '"paths"'
check "GET /guide/search/search_index.json" "/guide/search/search_index.json" '"docs"'
check "GET /docs (Swagger UI)"              "/docs" "Swagger UI"
check "GET /redoc (ReDoc)"                  "/redoc" "ReDoc"
check "GET /openapi.json"                   "/openapi.json" '"paths"'
check "GET /ui/ (konsol)"                   "/ui/" "RAG Chat"
echo "== rute API tetap utuh =="
check "GET /api/v1/health"                  "/api/v1/health" '"ok"'
check "GET /api/v1/ready"                   "/api/v1/ready" '"ready"'
check "GET /api/v1/metrics"                 "/api/v1/metrics" 'http_requests_total'
echo "== yang tidak boleh bocor =="
for p in "/.env" "/.git/config" "/guide/../.env"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$p")
  if [ "$code" = "404" ]; then
    echo "  PASS $p — HTTP 404"
  else
    echo "  FAIL $p — HTTP $code (seharusnya 404)"; fail=1
  fi
done
echo "== judul halaman yang disajikan =="
for p in /guide/ /guide/integration/ /guide/api-reference/ /docs /redoc /ui/; do
  echo "  $p -> $(curl -s "http://127.0.0.1:$PORT$p" | grep -oE '<title>[^<]*' | head -1 | sed 's/<title>//')"
done
echo "  / -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/") location: $(curl -s -o /dev/null -w '%{redirect_url}' "http://127.0.0.1:$PORT/")"

[ "$fail" = "0" ] && echo && echo "SEMUA PEMERIKSAAN LULUS" || { echo && echo "ADA YANG GAGAL — lihat $D/server.log" >&2; exit 1; }
