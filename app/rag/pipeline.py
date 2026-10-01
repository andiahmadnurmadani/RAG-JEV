"""End-to-end orchestration for /query, /search, /extract (PRD 12, 16, 18, 26).

    tenant context -> Jev route -> hybrid retrieval -> rerank
                   -> threshold / no-answer -> context -> LLM -> answer + sources

Everything above the API layer lives here so route handlers stay thin and the
pipeline can be unit-tested without HTTP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.metrics import incr, observe
from app.core.tenant import TrustedContext
from app.jev.policies import RouteDecision
from app.jev.router import JevRouter
from app.parsing.tables import scope_note as table_scope_note
from app.qdrant import repository
from app.rag.chunker import estimate_tokens
from app.rag.context import BuiltContext, DocumentCoverage, build_context, merge_expanded
from app.rag.generator import NO_ANSWER_EN, NO_ANSWER_ID, Generator
from app.rag.retriever import Candidate, RetrievalResult, Retriever
from app.tables import analytics as table_analytics
from app.tables.store import TableStore

logger = get_logger(__name__)


@dataclass
class PipelineResult:
    answer: str = ""
    grounded: bool = False
    sources: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    context: Optional[BuiltContext] = None
    route: Optional[RouteDecision] = None
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    no_answer_reason: Optional[str] = None
    injection_flags: List[str] = field(default_factory=list)
    extracted: List[Any] = field(default_factory=list)
    raw_extraction: Dict[str, Any] = field(default_factory=dict)
    computed: Optional[Dict[str, Any]] = None
    table_note: Optional[str] = None
    # Berapa bagian setiap dokumen yang benar-benar masuk konteks (transparansi ke klien:
    # "dokumen ini dibaca lengkap" vs "sebagian").
    document_coverage: List[Dict[str, Any]] = field(default_factory=list)


class RagPipeline:
    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        generator: Generator,
        jev: JevRouter,
        tables: Optional[TableStore] = None,
    ) -> None:
        self._settings = settings
        self._retriever = retriever
        self._generator = generator
        self._jev = jev
        self._tables = tables

    # ------------------------------------------------------------------ #
    def route(self, query: str, context: TrustedContext, hint: Optional[str] = None) -> RouteDecision:
        if hint:
            from app.jev.policies import coerce_capability

            capability = coerce_capability(hint)
            if capability:
                return RouteDecision(
                    capability=capability,
                    source="request_hint",
                    confidence=1.0,
                    reason="caller supplied route hint",
                )
        return self._jev.route(query, tenant_context=context.as_dict())

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        *,
        query: str,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        final_k: int,
        use_hybrid: Optional[bool] = None,
        use_reranker: Optional[bool] = None,
        threshold: Optional[float] = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        result = self._retriever.retrieve(
            query=query,
            organization_id=context.organization_id,  # never from the request body
            knowledge_base_id=knowledge_base_id,
            final_k=final_k,
            use_hybrid=use_hybrid,
            use_reranker=use_reranker,
            threshold=threshold,
            document_ids=document_ids,
        )
        elapsed = time.perf_counter() - started
        result.elapsed_ms = round(elapsed * 1000, 2)
        observe("retrieval_latency", elapsed)
        incr("retrieval_requests")
        return result

    # ------------------------------------------------------------------ #
    def answer(
        self,
        *,
        query: str,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        top_k: int = 5,
        strict_grounding: Optional[bool] = None,
        include_sources: bool = True,
        use_hybrid: Optional[bool] = None,
        use_reranker: Optional[bool] = None,
        threshold: Optional[float] = None,
        route_hint: Optional[str] = None,
        document_ids: Optional[Sequence[str]] = None,
        table_analytics: Optional[bool] = None,
    ) -> PipelineResult:
        settings = self._settings
        strict = settings.strict_grounding if strict_grounding is None else strict_grounding
        decision = self.route(query, context, route_hint)
        result = PipelineResult(route=decision, model=self._generator.model)

        # Pertanyaan agregat ("total", "paling laku") dihitung dari tabel nyata lebih dulu;
        # jalur retrieval tidak bisa menjumlahkan baris yang tidak ikut terambil.
        use_tables = settings.table_analytics_enabled if table_analytics is None else table_analytics
        table_reason: Optional[str] = None
        if use_tables and self._tables is not None:
            computed, table_reason = self._answer_from_tables(
                query=query,
                context=context,
                knowledge_base_id=knowledge_base_id,
                document_ids=document_ids,
                decision=decision,
            )
            if computed is not None:
                return computed

        retrieval = self.retrieve(
            query=query,
            context=context,
            knowledge_base_id=knowledge_base_id,
            final_k=max(top_k, 1),
            use_hybrid=use_hybrid,
            use_reranker=use_reranker,
            threshold=threshold,
            document_ids=document_ids,
        )
        result.candidates = retrieval.candidates

        threshold_value = settings.relevance_threshold if threshold is None else threshold
        best = retrieval.best_score
        if not retrieval.candidates:
            reason = "no_candidates"
            return self._with_table_hint(self._no_answer(result, reason, retrieval), table_reason)
        if retrieval.reranked and best < threshold_value:
            observe("best_relevance_score", best)
            return self._with_table_hint(self._no_answer(result, "below_threshold", retrieval), table_reason)

        observe("best_relevance_score", best)
        budget = settings.context_token_budget
        candidates, coverage = self._expand_context(
            retrieval.candidates,
            context=context,
            knowledge_base_id=knowledge_base_id,
            document_ids=document_ids,
            budget_tokens=budget,
        )
        result.document_coverage = [item.to_dict() for item in coverage]
        built = build_context(
            candidates,
            max_tokens=budget,
            max_chunks=max(1, len(candidates)),
            coverage=coverage,
        )
        result.context = built
        result.injection_flags = built.injection_flags
        if built.injection_flags:
            logger.warning("prompt-injection markers found in retrieved context: %s", built.injection_flags)
            incr("prompt_injection_flags")

        if not built.used:
            return self._with_table_hint(self._no_answer(result, "context_empty", retrieval), table_reason)

        started = time.perf_counter()
        generated = self._generator.answer(query=query, context=built, strict_grounding=strict)
        generation_ms = round((time.perf_counter() - started) * 1000, 2)
        observe("generation_latency", generation_ms / 1000.0)
        incr("generation_requests")

        result.answer = generated.answer
        result.grounded = generated.grounded
        result.model = generated.usage.model or self._generator.model
        if not generated.grounded:
            incr("no_answer_total")
            result.no_answer_reason = "model_reported_insufficient_context"
            if strict:
                return self._with_table_hint(
                    self._no_answer(result, "strict_grounding", retrieval, keep_candidates=True), table_reason
                )
        if include_sources:
            # Only chunks the model was actually shown are citable (PRD 40).
            result.sources = built.citations()
        result.usage = {
            "retrieved_chunks": retrieval.count,
            "reranked_chunks": retrieval.count if retrieval.reranked else 0,
            "reranker": retrieval.reranker_used,
            "rerank_ms": retrieval.rerank_ms,
            "context_tokens": built.tokens,
            "context_chunks": len(built.used),
            "context_expanded_chunks": sum(1 for candidate in built.used if candidate.expanded),
            "document_coverage": [item.to_dict() for item in coverage],
            "input_tokens": generated.usage.input_tokens,
            "output_tokens": generated.usage.output_tokens,
            "retrieval_ms": retrieval.elapsed_ms,
            "generation_ms": generation_ms,
        }
        if table_reason:
            # Perhitungan tabel diminta tetapi ditolak: klien harus tahu alasannya.
            result.table_note = table_reason
        return result

    # ------------------------------------------------------------------ #
    def _expand_context(
        self,
        candidates: Sequence[Candidate],
        *,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        document_ids: Optional[Sequence[str]],
        budget_tokens: int,
    ) -> tuple[List[Candidate], List[DocumentCoverage]]:
        """Lengkapi konteks dengan SISA potongan dokumen yang sudah terambil.

        Pencarian kemiripan selalu menghasilkan sebagian: beberapa potongan teratas dari
        dokumen yang bisa punya puluhan bagian. Pertanyaan seperti "struktur lengkap database
        X" tidak bisa dijawab dari sebagian - model lalu menulis bahwa datanya tidak ada di
        konteks, padahal datanya ADA di indeks. Di sini dokumen yang muncul di hasil pencarian
        diikuti sampai habis (urut dokumen) selama anggaran token masih cukup, dan kelengkapan
        yang benar-benar tercapai dilaporkan apa adanya.
        """
        settings = self._settings
        extras: List[tuple[str, List[Candidate]]] = []
        allowed = {str(item) for item in (document_ids or []) if item}
        top_documents: List[Candidate] = []
        for candidate in candidates:
            if allowed and candidate.document_id not in allowed:
                # Lingkup dokumen yang diminta klien tetap mengikat di jalur pelengkap.
                continue
            if candidate.document_id not in [item.document_id for item in top_documents]:
                top_documents.append(candidate)

        if settings.context_expand_documents and top_documents:
            spent = sum(estimate_tokens(candidate.content) + 60 for candidate in candidates)
            for top in top_documents[: max(1, int(settings.context_expand_max_documents))]:
                if spent >= budget_tokens:
                    break
                try:
                    parts = repository.list_document_chunks(
                        settings,
                        organization_id=context.organization_id,
                        document_id=top.document_id,
                        knowledge_base_id=knowledge_base_id,
                    )
                except Exception as exc:  # noqa: BLE001 - pelengkap, bukan jalur wajib
                    logger.warning("gagal melengkapi dokumen %s: %s", top.document_id, exc)
                    continue
                if not parts:
                    continue
                already = {item.chunk_id for item in candidates if item.document_id == top.document_id}
                added: List[Candidate] = []
                for position, payload in enumerate(parts, start=1):
                    chunk_id = str(payload.get("chunk_id") or "")
                    content = str(payload.get("content") or "")
                    if not chunk_id or not content.strip() or chunk_id in already:
                        continue
                    tokens = estimate_tokens(content)
                    if spent + tokens > budget_tokens:
                        break
                    added.append(
                        Candidate(
                            chunk_id=chunk_id,
                            document_id=top.document_id,
                            content=content,
                            document_name=str(payload.get("document_name") or top.document_name),
                            page=payload.get("page"),
                            section=str(payload.get("section") or ""),
                            source_url=str(payload.get("source_url") or ""),
                            language=str(payload.get("language") or ""),
                            expanded=True,
                            document_order=position,
                        )
                    )
                    spent += tokens
                if added:
                    extras.append((top.document_id, added))

        coverage = self._document_coverage(candidates, extras, context=context)
        return merge_expanded(candidates, extras), coverage

    def _document_coverage(
        self,
        candidates: Sequence[Candidate],
        extras: Sequence[tuple[str, Sequence[Candidate]]],
        *,
        context: TrustedContext,
    ) -> List[DocumentCoverage]:
        """Hitung berapa bagian tiap dokumen yang ikut ke konteks vs jumlah di indeks."""

        settings = self._settings
        tally: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            entry = tally.setdefault(candidate.document_id, {"name": candidate.document_name, "included": 0})
            entry["included"] += 1
        for document_id, parts in extras:
            entry = tally.setdefault(
                document_id,
                {"name": parts[0].document_name if parts else "", "included": 0},
            )
            entry["included"] += len(parts)

        coverage: List[DocumentCoverage] = []
        for document_id, entry in tally.items():
            try:
                total = repository.count_document(
                    settings, organization_id=context.organization_id, document_id=document_id
                )
            except Exception as exc:  # noqa: BLE001 - pelengkap
                logger.warning("gagal menghitung dokumen %s: %s", document_id, exc)
                total = 0
            if total <= 0:
                # Jumlah tidak diketahui (-1) atau nol: jangan mengaku "sebagian" atas dasar
                # angka yang tidak dipercaya; laporkan apa yang benar-benar dikirim.
                total = entry["included"]
            coverage.append(
                DocumentCoverage(
                    document_id=document_id,
                    document_name=str(entry["name"] or document_id),
                    included=entry["included"],
                    total=int(total),
                    complete=entry["included"] >= int(total),
                    ordered=any(document_id == doc for doc, _ in extras),
                )
            )
        coverage.sort(key=lambda item: (not item.complete, -item.included))
        return coverage

    # ------------------------------------------------------------------ #
    def _answer_from_tables(
        self,
        *,
        query: str,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        document_ids: Optional[Sequence[str]],
        decision: Optional[RouteDecision],
    ) -> tuple[Optional[PipelineResult], Optional[str]]:
        """Hitung jawaban dari tabel nyata. Mengembalikan (hasil, alasan gagal)."""

        assert self._tables is not None
        try:
            tables = self._tables.list_tables(
                organization_id=context.organization_id,
                knowledge_base_id=knowledge_base_id,
                document_ids=document_ids,
            )
        except AppError as exc:
            logger.warning("baca tabel gagal: %s", exc)
            return None, None
        except Exception as exc:  # noqa: BLE001 - store rusak tidak boleh mematikan /query
            logger.warning("baca tabel gagal tak terduga: %s", exc)
            return None, None
        if not tables:
            return None, None

        started = time.perf_counter()
        plan = table_analytics.build_plan(self._generator.client, query=query, tables=tables)
        if plan is None:
            return None, None
        table = tables[0]
        if plan.table_index is not None and 0 <= plan.table_index < len(tables):
            table = tables[plan.table_index]

        rows = self._tables.load_rows(table.table_id)
        try:
            plan = table_analytics.validate_plan(plan, table, rows)
        except table_analytics.PlanError as exc:
            columns = ", ".join(exc.available_columns[:20])
            reason = f"Perhitungan tidak bisa dilakukan: {exc.reason}. Kolom yang tersedia: {columns}"
            logger.info("rencana tabel ditolak: %s", exc.reason)
            return None, reason

        try:
            facts = table_analytics.execute_plan(plan, table, rows)
        except table_analytics.PlanError as exc:
            reason = f"Perhitungan tidak bisa dilakukan: {exc.reason}"
            return None, reason

        analytics_ms = round((time.perf_counter() - started) * 1000, 2)
        narrate_started = time.perf_counter()
        text, usage = table_analytics.narrate(self._generator.client, query=query, facts=facts)
        generation_ms = round((time.perf_counter() - narrate_started) * 1000, 2)

        # Pemotongan baris/kolom tidak boleh senyap: kalau tabel yang dipakai dipotong,
        # kalimatnya ikut ke jawaban (dan ke table_note) - bukan hanya tersimpan di basis data.
        note = table_scope_note(table)
        if note:
            text = f"{text} {note}" if text else note
            logger.warning("jawaban tabel memakai tabel terbatas: %s", note)

        result = PipelineResult(
            answer=text,
            grounded=True,
            route=decision,
            model=self._generator.model,
            computed=facts,
            table_note=note or None,
        )
        result.usage = {
            "retrieved_chunks": 0,
            "reranked_chunks": 0,
            "reranker": "none",
            "rerank_ms": 0.0,
            "context_tokens": 0,
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "retrieval_ms": 0.0,
            "generation_ms": generation_ms,
            "analytics_ms": analytics_ms,
            "computed_rows": facts.get("rows_matched", 0),
            "rows_skipped": facts.get("rows_skipped", 0),
        }
        observe("analytics_latency", analytics_ms / 1000.0)
        incr("analytics_requests")
        incr("analytics_rows", facts.get("rows_matched", 0))
        return result, None

    @staticmethod
    def _with_table_hint(result: PipelineResult, hint: Optional[str]) -> PipelineResult:
        """Saat tidak ada konteks teks, sebutkan kenapa tabel pun tidak bisa dipakai."""

        if hint:
            result.table_note = hint
            result.answer = f"{result.answer} {hint}" if result.answer else hint
            if not result.no_answer_reason:
                result.no_answer_reason = "table_plan_invalid"
        return result

    # ------------------------------------------------------------------ #
    def search(
        self,
        *,
        query: str,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        final_k: int = 10,
        use_hybrid: Optional[bool] = None,
        use_reranker: Optional[bool] = None,
        threshold: Optional[float] = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> RetrievalResult:
        return self.retrieve(
            query=query,
            context=context,
            knowledge_base_id=knowledge_base_id,
            final_k=final_k,
            use_hybrid=use_hybrid,
            use_reranker=use_reranker,
            threshold=threshold,
            document_ids=document_ids,
        )

    # ------------------------------------------------------------------ #
    def extract(
        self,
        *,
        query: str,
        context: TrustedContext,
        knowledge_base_id: Optional[str],
        output_schema: Optional[Dict[str, Any]] = None,
        top_k: int = 10,
        document_ids: Optional[Sequence[str]] = None,
    ) -> PipelineResult:
        decision = self.route(query, context)
        retrieval = self.retrieve(
            query=query,
            context=context,
            knowledge_base_id=knowledge_base_id,
            final_k=top_k,
            document_ids=document_ids,
        )
        result = PipelineResult(route=decision, candidates=retrieval.candidates, model=self._generator.model)
        if not retrieval.candidates:
            result.no_answer_reason = "no_candidates"
            return result

        budget = self._settings.context_token_budget
        candidates, coverage = self._expand_context(
            retrieval.candidates,
            context=context,
            knowledge_base_id=knowledge_base_id,
            document_ids=document_ids,
            budget_tokens=budget,
        )
        result.document_coverage = [item.to_dict() for item in coverage]
        built = build_context(
            candidates,
            max_tokens=budget,
            max_chunks=max(1, len(candidates)),
            coverage=coverage,
        )
        result.context = built
        result.injection_flags = built.injection_flags
        if not built.used:
            result.no_answer_reason = "context_empty"
            return result

        payload = self._generator.extract(query=query, context=built, output_schema=output_schema)
        items = payload.get("items")
        if items is None and payload.get("not_found"):
            items = []
        if items is None:
            # tolerate a bare object/array response from a lenient model
            items = payload if isinstance(payload, list) else [payload]
        if isinstance(items, list):
            result.answer = ""
            result.grounded = bool(items) and not payload.get("not_found", False)
            result.sources = built.citations()
            result.usage = {"retrieved_chunks": retrieval.count, "extracted_items": len(items)}
            result.extracted = items
        else:
            result.no_answer_reason = "invalid_extraction_payload"
        result.raw_extraction = payload
        return result

    # ------------------------------------------------------------------ #
    def _no_answer(
        self,
        result: PipelineResult,
        reason: str,
        retrieval: RetrievalResult,
        *,
        keep_candidates: bool = False,
    ) -> PipelineResult:
        incr("no_answer_total")
        result.answer = NO_ANSWER_ID if self._settings.answer_language == "id" else NO_ANSWER_EN
        result.grounded = False
        result.sources = []
        result.no_answer_reason = reason
        if not keep_candidates:
            result.candidates = []
        result.usage = {
            "retrieved_chunks": retrieval.count,
            "reranked_chunks": retrieval.count if retrieval.reranked else 0,
            "reranker": retrieval.reranker_used,
            "rerank_ms": retrieval.rerank_ms,
            "context_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "retrieval_ms": retrieval.elapsed_ms,
        }
        logger.info("no-answer path taken (%s) for org %s", reason, "-")
        return result


def ensure_knowledge_base(knowledge_base_id: Optional[str]) -> None:
    if not knowledge_base_id:
        raise AppError(
            "VALIDATION_ERROR",
            "knowledge_base_id is required: retrieval is always scoped to a knowledge base",
        )
