#!/usr/bin/env bash
# Menjalankan layanan RAG dalam mode "live" untuk dipakai dari UI.
#
# Kunci API tidak ditulis di berkas ini: nilainya diambil dari .env Hermes lokal
# (atau dari environment warisan). Bila keduanya kosong, layanan tetap jalan dengan
# LLM_PROVIDER=mock + Jev mati, jadi UI masih bisa dibuka untuk uji tampilan.
#
# Pemakaian:
#   bash scripts/run_live.sh            # port 8099
#   PORT=8100 bash scripts/run_live.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# --- kunci dari .env Hermes (tidak pernah dicetak) --------------------------
HERMES_ENV="${HERMES_ENV:-$HOME/AppData/Local/hermes/.env}"
if [ -f "$HERMES_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HERMES_ENV"
  set +a
fi

GATEWAY_KEY="${GATEWAY_KEY:-${HERMES_CUSTOM_9ROUTER_YANTO_TOP_API_KEY:-}}"
JEV_KEY="${JEV_KEY:-${HERMES_CUSTOM_LOCALHOST_20128_API_KEY:-}}"

LIVE_DIR="data/live"
mkdir -p "$LIVE_DIR"

export APP_ENV="${APP_ENV:-local}"
export PORT="${PORT:-8099}"
export HOST="${HOST:-127.0.0.1}"

# --- penyimpanan lokal (mode embedded, tanpa Qdrant server) ------------------
export QDRANT_URL=""
export QDRANT_LOCAL_PATH="$LIVE_DIR/qdrant"
export QDRANT_COLLECTION="${QDRANT_COLLECTION:-live_chunks}"
export SPARSE_DIR="$LIVE_DIR/sparse"
export STORAGE_DIR="$LIVE_DIR/storage"
export REGISTRY_PATH="$LIVE_DIR/registry.json"
export JOB_STORE_PATH="$LIVE_DIR/jobs.json"
export SETTINGS_OVERRIDE_PATH="$LIVE_DIR/settings.json"

# --- model lokal mesin ini ---------------------------------------------------
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-hash}"
export RERANKER_PROVIDER="${RERANKER_PROVIDER:-none}"
export RERANKER_ENABLED="${RERANKER_ENABLED:-false}"

# --- generator jawaban -------------------------------------------------------
export LLM_PROVIDER="${LLM_PROVIDER:-openai_compatible}"
export LLM_BASE_URL="${LLM_BASE_URL:-https://9router.yanto.top/v1}"
export LLM_MODEL="${LLM_MODEL:-cmc/Qwen/Qwen3.6-Plus}"
export LLM_API_KEY="${LLM_API_KEY:-$GATEWAY_KEY}"

# --- Jev (lapisan keputusan) -------------------------------------------------
export JEV_ENABLED="${JEV_ENABLED:-true}"
export JEV_MODE="${JEV_MODE:-live}"
export JEV_PROVIDER="${JEV_PROVIDER:-systemone}"
export JEV_SYSTEMONE_URL="${JEV_SYSTEMONE_URL:-http://localhost:20128/v1/systemone}"
export JEV_MODEL="${JEV_MODEL:-oc/jev-1.13-free}"
export JEV_API_KEY="${JEV_API_KEY:-$JEV_KEY}"
export JEV_TIMEOUT="${JEV_TIMEOUT:-20}"

export RATE_LIMIT_PER_MINUTE="${RATE_LIMIT_PER_MINUTE:-600}"
export MAX_UPLOAD_MB="${MAX_UPLOAD_MB:-8}"

# --- kunci akses untuk mencoba UI -------------------------------------------
# Dibaca dari berkas supaya tidak perlu escaping JSON di dalam skrip shell.
# live-key-org-a punya izin admin (boleh menyimpan setelan model/Jev);
# live-key-org-b hanya read+write (untuk membuktikan batas admin).
API_KEYS_FILE="${API_KEYS_FILE:-scripts/api_keys_demo.json}"
export API_KEYS_JSON="${API_KEYS_JSON:-$(tr -d '\n' < "$API_KEYS_FILE")}"

echo "menjalankan RAG service di http://${HOST}:${PORT}/ui/"
echo "generator : ${LLM_PROVIDER} ${LLM_MODEL} @ ${LLM_BASE_URL}"
echo "jev       : ${JEV_MODE}/${JEV_PROVIDER} ${JEV_MODEL} @ ${JEV_SYSTEMONE_URL}"
echo "penyimpanan: ${LIVE_DIR}"

exec .venv/Scripts/python.exe -m uvicorn app.main:app --host "$HOST" --port "$PORT"
