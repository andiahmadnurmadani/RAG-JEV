#!/usr/bin/env bash
# Wait for the BAAI/bge-m3 weights to finish downloading, then run the evaluation with
# REAL semantic embeddings (sentence_transformers, CPU). Writes data/eval/report-bge-m3.json.
set -u
cd /e/rag-service
PY=.venv/Scripts/python.exe
LOG=data/eval/bge-m3-eval.log
: > "$LOG"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "waiting for BAAI/bge-m3 weights (loading from cache; downloads resume automatically)"
$PY - <<'PY' 2>&1 | tee -a "$LOG"
import time
t0 = time.time()
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("BAAI/bge-m3", device="cpu", cache_folder=None)
print(f"loaded in {time.time()-t0:.1f}s dim={model.get_sentence_embedding_dimension()}", flush=True)
t0 = time.time()
vecs = model.encode(["prosedur cuti tahunan", "batas waktu pelaporan insiden"], normalize_embeddings=True)
print(f"encoded 2 texts in {time.time()-t0:.1f}s shape={vecs.shape}", flush=True)
PY
rc=$?
say "model load exit=$rc"
[ $rc -ne 0 ] && exit $rc

say "eval: bge-m3 embeddings, reranker none"
EVAL_WORKSPACE="E:/rag-service/data/eval/ws-bge-m3" \
EMBEDDING_PROVIDER=sentence_transformers \
EMBEDDING_MODEL=BAAI/bge-m3 \
EMBEDDING_DEVICE=cpu \
RERANKER_PROVIDER=none \
  $PY -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report-bge-m3.json 2>&1 | tee -a "$LOG"
say "eval exit=${PIPESTATUS[0]}"
say "DONE"
