"""Lexical (BM25) index — the keyword half of hybrid retrieval (PRD 14).

Stored per ``(organization_id, knowledge_base_id)`` pair so a lexical query can
never see another tenant's documents, exactly like the vector side. Documents are
JSON files on disk: small, inspectable, no extra service to run.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


def tokenize(text: str) -> List[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


@dataclass
class SparseEntry:
    chunk_id: str
    document_id: str
    tokens: List[str]


@dataclass
class _Scope:
    entries: List[SparseEntry] = field(default_factory=list)
    model: object = None
    dirty: bool = True


class SparseIndex:
    """Persisted BM25 index, partitioned by tenant scope."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = Path(settings.sparse_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._scopes: Dict[str, _Scope] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def scope_key(organization_id: str, knowledge_base_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{organization_id}__{knowledge_base_id}")
        return safe

    def _path(self, scope: str) -> Path:
        return self._root / f"{scope}.json"

    def _scope(self, organization_id: str, knowledge_base_id: str) -> _Scope:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            if scope not in self._scopes:
                loaded = _Scope()
                path = self._path(scope)
                if path.exists():
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        loaded.entries = [SparseEntry(**item) for item in payload.get("entries", [])]
                        loaded.dirty = False
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("sparse index %s unreadable (%s); starting empty", scope, exc)
                self._scopes[scope] = loaded
            return self._scopes[scope]

    def _persist(self, organization_id: str, knowledge_base_id: str, scope_obj: _Scope) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        payload = {
            "organization_id": organization_id,
            "knowledge_base_id": knowledge_base_id,
            "entries": [dict(chunk_id=e.chunk_id, document_id=e.document_id, tokens=e.tokens) for e in scope_obj.entries],
        }
        tmp = self._path(scope).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(scope))
        scope_obj.dirty = False

    # ------------------------------------------------------------------ #
    def upsert(
        self,
        organization_id: str,
        knowledge_base_id: str,
        items: Sequence[Tuple[str, str, str]],
    ) -> int:
        """``items`` = (chunk_id, document_id, content). Replaces those chunks.

        Identity is the *(document_id, chunk_id)* pair: ``chunk_0001`` exists in every
        document, so matching on the bare chunk id would make a second document's
        indexing silently delete the first document's lexical entries.
        """
        added = 0
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            incoming = {(document_id, chunk_id) for chunk_id, document_id, _ in items}
            scope_obj.entries = [
                entry for entry in scope_obj.entries if (entry.document_id, entry.chunk_id) not in incoming
            ]
            for chunk_id, document_id, content in items:
                scope_obj.entries.append(SparseEntry(chunk_id=chunk_id, document_id=document_id, tokens=tokenize(content)))
                added += 1
            scope_obj.model = None
            self._persist(organization_id, knowledge_base_id, scope_obj)
        return added

    def remove_document(self, organization_id: str, knowledge_base_id: str, document_id: str) -> int:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            before = len(scope_obj.entries)
            scope_obj.entries = [entry for entry in scope_obj.entries if entry.document_id != document_id]
            removed = before - len(scope_obj.entries)
            if removed:
                scope_obj.model = None
                self._persist(organization_id, knowledge_base_id, scope_obj)
        return removed

    def drop_scope(self, organization_id: str, knowledge_base_id: str) -> None:
        scope = self.scope_key(organization_id, knowledge_base_id)
        with self._lock:
            self._scopes.pop(scope, None)
            path = self._path(scope)
            if path.exists():
                path.unlink()

    def _ensure_model(self, scope_obj: _Scope):
        if scope_obj.model is None or scope_obj.dirty:
            from rank_bm25 import BM25Plus

            # BM25Plus (not Okapi): its idf is log((N+1)/n) which stays positive even
            # for a one-document scope, where Okapi's idf floor makes every score <= 0.
            corpus = [entry.tokens or [""] for entry in scope_obj.entries]
            scope_obj.model = BM25Plus(corpus) if corpus else None
            scope_obj.dirty = False
        return scope_obj.model

    def search(
        self,
        query: str,
        organization_id: str,
        knowledge_base_id: str,
        top_k: int = 30,
        document_ids: Optional[Sequence[str]] = None,
    ) -> List[Tuple[str, float]]:
        """Return ``[(f"{document_id}::{chunk_id}", normalised_score)]``.

        The key is composite because ``chunk_0001`` repeats in every document; a bare
        chunk id would make two documents indistinguishable for the fusion stage.

        ``document_ids`` narrows the search to a subset of this scope's documents. Scores
        come from the scope-wide BM25 model (its idf is corpus-wide), so the subset only
        *filters* candidates — it does not rebuild statistics. That is a deliberate
        trade-off: rebuilding the index per prompt would cost more than the ranking
        difference, and the scores are normalised against the surviving set anyway.
        """
        scope_obj = self._scope(organization_id, knowledge_base_id)
        wanted = {str(item) for item in (document_ids or []) if item} or None
        with self._lock:
            if not scope_obj.entries:
                return []
            model = self._ensure_model(scope_obj)
            if model is None:
                return []
            scores = model.get_scores(tokenize(query))
            paired = [
                (f"{entry.document_id}::{entry.chunk_id}", float(score))
                for entry, score in zip(scope_obj.entries, scores)
                if wanted is None or entry.document_id in wanted
            ]
        ranked = sorted(paired, key=lambda item: item[1], reverse=True)[:top_k]
        if not ranked:
            return []
        best = max(score for _, score in ranked) or 1.0
        return [(key, score / best) for key, score in ranked if score > 0]

    def document_ids(self, organization_id: str, knowledge_base_id: str) -> List[str]:
        scope_obj = self._scope(organization_id, knowledge_base_id)
        with self._lock:
            return sorted({entry.document_id for entry in scope_obj.entries})

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {scope: len(scope_obj.entries) for scope, scope_obj in self._scopes.items()}
