#!/usr/bin/env bash
# Evaluation matrix: {hash, bge-m3} x {hybrid, dense-only} on the 10-document corpus.
# Writes one JSON report per configuration plus a plain-text summary.
set -u
cd /e/rag-service
PY=.venv/Scripts/python.exe
OUT=data/eval/matrix.txt
: > "$OUT"

run_one() {   # $1 = label, $2 = extra args, $3.. = env assignments
  local label="$1"; shift
  local args="$1"; shift
  echo "=================================================================" | tee -a "$OUT"
  echo "== $label" | tee -a "$OUT"
  env "$@" EVAL_WORKSPACE="E:/rag-service/data/eval/ws-$label" \
      timeout 900 $PY -m tests.evaluation.run_eval --report "E:/rag-service/data/eval/report-$label.json" $args 2>&1 \
    | grep -E "^embedding|^reranker|^hybrid|^corpus|cases \(answerable|Recall@5|Precision@5|^MRR|Hit rate|grounded rate|citation present|refusal rate" \
    | tee -a "$OUT"
}

run_one hash-hybrid ""             EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none
run_one hash-dense-only "--no-hybrid" EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none
run_one bge-m3-hybrid ""           EMBEDDING_PROVIDER=sentence_transformers EMBEDDING_MODEL=BAAI/bge-m3 EMBEDDING_DEVICE=cpu RERANKER_PROVIDER=none
run_one bge-m3-dense-only "--no-hybrid" EMBEDDING_PROVIDER=sentence_transformers EMBEDDING_MODEL=BAAI/bge-m3 EMBEDDING_DEVICE=cpu RERANKER_PROVIDER=none

echo "=================================================================" | tee -a "$OUT"
echo "DONE $(date +%H:%M:%S)" | tee -a "$OUT"
