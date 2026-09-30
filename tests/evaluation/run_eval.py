"""RAG evaluation harness (PRD 38, 39): Recall@K, Precision@K, MRR, no-answer.

Runs the *real* pipeline (parser -> chunker -> index -> hybrid retrieve -> rerank)
against the corpus in ``corpus/`` and the questions in ``dataset.json``. Nothing is
simulated except the LLM, which is the mock client when ``LLM_PROVIDER=mock``.

    python -m tests.evaluation.run_eval                    # hash embeddings, fast
    EMBEDDING_PROVIDER=fastembed python -m tests.evaluation.run_eval
    python -m tests.evaluation.run_eval --report E:/rag-service/data/eval/report.json

Why it works this way: embedding/reranking quality can only be judged with the real
models, so the harness takes the provider from the environment and prints which one
it used — a number is only meaningful next to the model that produced it.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE = Path(__file__).resolve().parent
CORPUS = BASE / "corpus"
DATASET = BASE / "dataset.json"

WORKSPACE = Path(os.environ.get("EVAL_WORKSPACE") or (BASE.parent.parent / "data" / "eval" / "workspace"))


def _reset_workspace(workspace: Path) -> None:
    import shutil

    for name in ("qdrant", "sparse", "storage"):
        target = workspace / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for name in ("jobs.json",):
        target = workspace / name
        if target.exists():
            target.unlink(missing_ok=True)


DOC_IDS = {
    "01_sop_cuti.md": "doc_sop_cuti",
    "02_sop_lembur.md": "doc_sop_lembur",
    "03_kebijakan_absensi.md": "doc_sop_absensi",
    "04_iso27001.md": "doc_iso27001",
    "05_laporan_penjualan.md": "doc_penjualan_q1",
    "06_kebijakan_pengadaan.md": "doc_pengadaan",
    "07_onboarding.md": "doc_onboarding",
    "08_sop_insiden_keamanan.md": "doc_sop_insiden",
    "09_kebijakan_perjalanan_dinas.md": "doc_perjalanan_dinas",
    "10_panduan_pengadaan_ti.md": "doc_pengadaan_ti",
}


def _build_isolated_settings(tmp: Path):
    os.environ.setdefault("APP_ENV", "evaluation")
    os.environ["QDRANT_URL"] = ""
    os.environ["QDRANT_LOCAL_PATH"] = str(tmp / "qdrant")
    os.environ["QDRANT_COLLECTION"] = "eval_chunks"
    os.environ["SPARSE_DIR"] = str(tmp / "sparse")
    os.environ["JOB_STORE_PATH"] = str(tmp / "jobs.json")
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["JEV_ENABLED"] = "false"
    os.environ.setdefault("RERANKER_ENABLED", "true")
    os.environ.setdefault("LLM_PROVIDER", "mock")

    from app.core.config import get_settings, reset_settings_cache
    from app.qdrant.client import reset_client

    reset_settings_cache()
    reset_client()
    settings = get_settings()
    settings.ensure_dirs()
    return settings


def _index_corpus(services, organization_id: str, knowledge_base_id: str) -> Dict[str, int]:
    stats: Dict[str, int] = {}
    for path in sorted(CORPUS.glob("*.md")):
        document_id = DOC_IDS.get(path.name, path.stem)
        job = services.worker.submit(
            {
                "document_id": document_id,
                "organization_id": organization_id,
                "knowledge_base_id": knowledge_base_id,
                "document_name": path.name,
                "file_url": "",
                "text": path.read_text(encoding="utf-8"),
                "metadata": {"eval": True},
                "language": "id",
                "replace": True,
            }
        )
        # the worker runs in a thread pool; wait for the synchronous pipeline result
        deadline = time.time() + 300
        record = services.jobs.get(job.job_id)
        while record is not None and record.status not in ("completed", "failed") and time.time() < deadline:
            time.sleep(0.05)
            record = services.jobs.get(job.job_id)
        if record is None or record.status != "completed":
            raise RuntimeError(f"indexing failed for {path.name}: {record.error if record else 'unknown'}")
        stats[document_id] = record.chunks
    return stats


def _precision_at_k(ranked_docs: List[str], expected: List[str], k: int) -> float:
    """Standard definition: relevant retrieved / retrieved (set-based).

    The previous denominator (``min(k, len(expected))``) made Precision@K collapse into
    Recall@K whenever the corpus held fewer than K documents, so a 7-document corpus with
    K=5 always reported 1.0 for both — a number that looked great and measured nothing.
    """
    top = list(dict.fromkeys(ranked_docs[:k]))
    if not top:
        return 0.0
    relevant = set(expected)
    hits = sum(1 for doc in top if doc in relevant)
    return hits / len(top)


def _recall_at_k(ranked_docs: List[str], expected: List[str], k: int) -> float:
    if not expected:
        return 1.0
    top = set(ranked_docs[:k])
    return len(top & set(expected)) / len(set(expected))


def _reciprocal_rank(ranked_docs: List[str], expected: List[str]) -> float:
    for index, doc in enumerate(ranked_docs, start=1):
        if doc in expected:
            return 1.0 / index
    return 0.0


def run(report_path: Optional[Path] = None, use_hybrid: Optional[bool] = None) -> Dict[str, Any]:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    knowledge_base_id = dataset["knowledge_base_id"]
    organization_id = "org_eval"

    from app.api.deps import build_services
    from app.core.tenant import TrustedContext

    # A persistent workspace: Windows keeps the embedded Qdrant file lock open, so a
    # TemporaryDirectory would fail to clean up and mask the real result.
    workspace = WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    _reset_workspace(workspace)
    settings = _build_isolated_settings(workspace)
    services = build_services(settings)
    if True:
        context = TrustedContext(
            user_id="eval_user",
            organization_id=organization_id,
            application_id="app_eval",
            permissions=["read", "write"],
        )

        started = time.perf_counter()
        index_stats = _index_corpus(services, organization_id, knowledge_base_id)
        index_seconds = time.perf_counter() - started

        rows: List[Dict[str, Any]] = []
        for case in dataset["cases"]:
            retrieval = services.rag.retrieve(
                query=case["query"],
                context=context,
                knowledge_base_id=knowledge_base_id,
                final_k=settings.final_top_k,
                use_hybrid=use_hybrid,
            )
            ranked_docs = [candidate.document_id for candidate in retrieval.candidates]
            answer = services.rag.answer(
                query=case["query"],
                context=context,
                knowledge_base_id=knowledge_base_id,
                top_k=settings.final_top_k,
                use_hybrid=use_hybrid,
            )
            rows.append(
                {
                    "query": case["query"],
                    "answerable": case["answerable"],
                    "expected": case["expected_documents"],
                    "retrieved": ranked_docs,
                    "recall_at_k": _recall_at_k(ranked_docs, case["expected_documents"], settings.final_top_k),
                    "precision_at_k": _precision_at_k(ranked_docs, case["expected_documents"], settings.final_top_k),
                    "reciprocal_rank": _reciprocal_rank(ranked_docs, case["expected_documents"]),
                    "grounded": answer.grounded,
                    "sources": len(answer.sources),
                    "no_answer_reason": answer.no_answer_reason,
                }
            )

    from app.qdrant.client import reset_client

    reset_client()  # release the embedded-store lock before anything else runs

    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]

    def mean(values: List[float]) -> float:
        return round(statistics.fmean(values), 4) if values else 0.0

    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "configuration": {
            "embedding_provider": os.environ.get("EMBEDDING_PROVIDER", "hash"),
            "embedding_model": settings.embedding_model,
            "reranker_provider": settings.reranker_provider,
            "reranker_enabled": settings.reranker_enabled,
            "llm_provider": settings.llm_provider,
            "chunk_size": settings.chunk_size,
            "chunk_overlap": settings.chunk_overlap,
            "final_top_k": settings.final_top_k,
            "hybrid": settings.retrieval_hybrid if use_hybrid is None else use_hybrid,
            "relevance_threshold": settings.relevance_threshold,
        },
        "corpus": {"documents": len(index_stats), "chunks": index_stats, "index_seconds": round(index_seconds, 2)},
        "retrieval": {
            "cases": len(answerable),
            f"recall_at_{settings.final_top_k}": mean([row["recall_at_k"] for row in answerable]),
            f"precision_at_{settings.final_top_k}": mean([row["precision_at_k"] for row in answerable]),
            "mrr": mean([row["reciprocal_rank"] for row in answerable]),
            "hit_rate": mean([1.0 if row["recall_at_k"] > 0 else 0.0 for row in answerable]),
        },
        "generation": {
            "grounded_rate_answerable": mean([1.0 if row["grounded"] else 0.0 for row in answerable]),
            "citation_present_rate": mean([1.0 if row["sources"] > 0 else 0.0 for row in answerable]),
        },
        "no_answer": {
            "unanswerable_cases": len(unanswerable),
            "refusal_rate": mean([1.0 if not row["grounded"] else 0.0 for row in unanswerable]),
            "false_grounded": [row["query"] for row in unanswerable if row["grounded"]],
        },
        "cases": rows,
    }

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_summary(report)
    return report


def _print_summary(report: Dict[str, Any]) -> None:
    config = report["configuration"]
    top_k = config["final_top_k"]
    print("=" * 74)
    print("RAG EVALUATION (PRD 38)")
    print("=" * 74)
    print(f"embedding      : {config['embedding_provider']} ({config['embedding_model']})")
    print(f"reranker       : {config['reranker_provider']} (enabled={config['reranker_enabled']})")
    print(f"llm            : {config['llm_provider']}")
    print(f"chunking       : size={config['chunk_size']} overlap={config['chunk_overlap']}")
    print(f"hybrid         : {config['hybrid']} | final_top_k={top_k} | threshold={config['relevance_threshold']}")
    corpus = report["corpus"]
    print(f"corpus         : {corpus['documents']} docs, {sum(corpus['chunks'].values())} chunks, {corpus['index_seconds']}s")
    retrieval = report["retrieval"]
    print("-" * 74)
    print(f"cases (answerable)      : {retrieval['cases']}")
    print(f"Recall@{top_k}             : {retrieval[f'recall_at_{top_k}']}")
    print(f"Precision@{top_k}          : {retrieval[f'precision_at_{top_k}']}")
    print(f"MRR                     : {retrieval['mrr']}")
    print(f"Hit rate                : {retrieval['hit_rate']}")
    generation = report["generation"]
    print(f"grounded rate           : {generation['grounded_rate_answerable']}")
    print(f"citation present rate   : {generation['citation_present_rate']}")
    no_answer = report["no_answer"]
    print(f"refusal rate (unansw.)  : {no_answer['refusal_rate']} over {no_answer['unanswerable_cases']} cases")
    if no_answer["false_grounded"]:
        print("  false-grounded queries:")
        for query in no_answer["false_grounded"]:
            print(f"   - {query}")
    print("=" * 74)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval and grounding quality")
    parser.add_argument("--report", type=Path, default=BASE / "report.json")
    parser.add_argument("--no-hybrid", action="store_true", help="dense-only retrieval (ablation)")
    args = parser.parse_args()
    run(report_path=args.report, use_hybrid=False if args.no_hybrid else None)


if __name__ == "__main__":
    main()
