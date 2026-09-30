#!/usr/bin/env bash
# Real reranker evaluation: downloads BAAI/bge-reranker-v2-m3 (cross-encoder, CPU) and reruns
# the evaluation matrix with the threshold gate ACTIVE. This is the run that can finally
# refuse the three trap questions, so refusal rate becomes meaningful.
set -u
cd /e/rag-service
PY=.venv/Scripts/python.exe
LOG=data/eval/reranker-eval.log
: > "$LOG"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "loading BAAI/bge-reranker-v2-m3 (downloads on first use; ~2.3 GB)"
$PY - <<'PY' 2>&1 | tee -a "$LOG"
import time
from sentence_transformers import CrossEncoder
t0 = time.time()
model = CrossEncoder("BAAI/bge-reranker-v2-m3", device="cpu")
print(f"loaded reranker in {time.time()-t0:.1f}s", flush=True)
t0 = time.time()
scores = model.predict([("berapa kuota cuti tahunan karyawan?", "Kuota cuti tahunan karyawan adalah dua belas hari kerja per tahun.")])
print(f"scored one pair in {time.time()-t0:.1f}s -> {float(scores[0]):.4f}", flush=True)
PY
rc=$?
say "reranker load exit=$rc"
[ $rc -ne 0 ] && exit $rc

say "eval A: bge-m3 embeddings + bge-reranker-v2-m3 (threshold aktif)"
EVAL_WORKSPACE="E:/rag-service/data/eval/ws-reranked-bge" \
EMBEDDING_PROVIDER=sentence_transformers EMBEDDING_MODEL=BAAI/bge-m3 EMBEDDING_DEVICE=cpu \
RERANKER_PROVIDER=sentence_transformers RERANKER_MODEL=BAAI/bge-reranker-v2-m3 RERANKER_DEVICE=cpu \
RERANKER_ENABLED=true \
  $PY -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report-reranked-bge-m3.json 2>&1 | tee -a "$LOG"
say "eval A exit=${PIPESTATUS[0]}"

say "eval B: hash embeddings + bge-reranker-v2-m3 (isolates reranker from embedding quality)"
EVAL_WORKSPACE="E:/rag-service/data/eval/ws-reranked-hash" \
EMBEDDING_PROVIDER=hash \
RERANKER_PROVIDER=sentence_transformers RERANKER_MODEL=BAAI/bge-reranker-v2-m3 RERANKER_DEVICE=cpu \
RERANKER_ENABLED=true \
  $PY -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report-reranked-hash.json 2>&1 | tee -a "$LOG"
say "eval B exit=${PIPESTATUS[0]}"
say "DONE"
