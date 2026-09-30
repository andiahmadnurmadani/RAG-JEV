#!/usr/bin/env bash
# Real-model evaluation run: BAAI/bge-m3 embeddings (+ optional bge-reranker-v2-m3),
# both through sentence_transformers on CPU. Downloads the weights once, then runs the
# evaluation harness twice and stores both reports.
set -u
cd /e/rag-service
PY=.venv/Scripts/python.exe
LOG=data/eval/real-models.log
: > "$LOG"
echo "[$(date +%H:%M:%S)] probing BAAI/bge-m3 (sentence_transformers, CPU)" | tee -a "$LOG"

$PY - <<'PY' 2>&1 | tee -a "$LOG"
import time
t0 = time.time()
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("BAAI/bge-m3", device="cpu")
print(f"loaded bge-m3 in {time.time()-t0:.1f}s dim={m.get_sentence_embedding_dimension()}", flush=True)
t0 = time.time()
v = m.encode(["prosedur cuti tahunan", "batas waktu pelaporan insiden"], normalize_embeddings=True)
print(f"encoded 2 texts in {time.time()-t0:.1f}s shape={v.shape}", flush=True)
PY
probe=$?
echo "[$(date +%H:%M:%S)] probe exit=$probe" | tee -a "$LOG"
[ $probe -ne 0 ] && exit $probe

echo "[$(date +%H:%M:%S)] eval #1: bge-m3 embeddings, no reranker" | tee -a "$LOG"
EMBEDDING_PROVIDER=sentence_transformers \
EMBEDDING_MODEL=BAAI/bge-m3 \
EMBEDDING_DEVICE=cpu \
RERANKER_PROVIDER=none \
  $PY -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report-bge-m3.json 2>&1 | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] eval #1 exit=${PIPESTATUS[0]}" | tee -a "$LOG"

echo "[$(date +%H:%M:%S)] eval #2: bge-m3 + bge-reranker-v2-m3" | tee -a "$LOG"
EMBEDDING_PROVIDER=sentence_transformers \
EMBEDDING_MODEL=BAAI/bge-m3 \
EMBEDDING_DEVICE=cpu \
RERANKER_PROVIDER=sentence_transformers \
RERANKER_MODEL=BAAI/bge-reranker-v2-m3 \
RERANKER_DEVICE=cpu \
  $PY -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report-bge-m3-reranked.json 2>&1 | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] eval #2 exit=${PIPESTATUS[0]}" | tee -a "$LOG"
echo "[$(date +%H:%M:%S)] DONE" | tee -a "$LOG"
