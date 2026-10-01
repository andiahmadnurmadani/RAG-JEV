"""Request/response models — the wire contract of PRD 22-28."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KnowledgeIndexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """PRD 22. ``organization_id`` is accepted for KMS compatibility but must match
    the trusted context (checked in the route)."""

    document_id: str = Field(min_length=1, max_length=200)
    knowledge_base_id: str = Field(min_length=1, max_length=200)
    organization_id: Optional[str] = Field(default=None, max_length=200)
    document_name: Optional[str] = Field(default=None, max_length=300)
    file_url: Optional[str] = Field(default=None, max_length=2000)
    content_base64: Optional[str] = None
    text: Optional[str] = None
    language: Optional[str] = Field(default=None, max_length=16)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    replace: bool = True

    @model_validator(mode="after")
    def _require_source(self) -> "KnowledgeIndexRequest":
        if not (self.file_url or self.content_base64 or self.text):
            raise ValueError("one of file_url, content_base64 or text is required")
        return self


class KnowledgeUpdateRequest(KnowledgeIndexRequest):
    """PRD 22 PUT — same payload, always replacing existing vectors."""

    replace: bool = True


class QueryOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Berapa potongan yang diambil untuk konteks. Kecil = jawaban "tidak lengkap" walau datanya
    # ada; besar = model benar-benar membaca dokumennya. Batas atas tetap 50.
    top_k: int = Field(default=12, ge=1, le=50)
    strict_grounding: bool = True
    include_sources: bool = True
    use_hybrid: Optional[bool] = None
    use_reranker: Optional[bool] = None
    threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    route: Optional[str] = None  # optional caller hint; Jev still decides by default
    # Restrict retrieval to specific documents of *this* tenant (a narrow, never a widen:
    # the tenant filter is applied independently in the repository).
    document_ids: Optional[List[str]] = Field(default=None, max_length=50)
    # Pertanyaan agregat ("total", "paling laku") dihitung dari tabel xlsx/csv/ods yang
    # terindeks; matikan untuk memaksa jalur retrieval teks biasa.
    table_analytics: Optional[bool] = None

    @field_validator("document_ids")
    @classmethod
    def _clean_document_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return None
        cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if any(len(item) > 200 for item in cleaned):
            raise ValueError("each document_id must be at most 200 characters")
        return sorted(set(cleaned)) or None


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    knowledge_base_id: Optional[str] = Field(default=None, max_length=200)
    options: QueryOptions = Field(default_factory=QueryOptions)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    knowledge_base_id: Optional[str] = Field(default=None, max_length=200)
    top_k: int = Field(default=10, ge=1, le=100)
    options: Optional[QueryOptions] = None


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=8000)
    knowledge_base_id: Optional[str] = Field(default=None, max_length=200)
    output_schema: Optional[Dict[str, Any]] = None
    top_k: int = Field(default=10, ge=1, le=50)
    document_ids: Optional[List[str]] = Field(default=None, max_length=50)


class SourceOut(BaseModel):
    document_id: str
    document_name: str = ""
    chunk_id: str = ""
    page: Optional[int] = None
    section: str = ""
    source_url: str = ""
    score: float = 0.0


class DocumentCoverageOut(BaseModel):
    """Berapa bagian satu dokumen yang benar-benar dikirim ke model (transparansi konteks)."""

    document_id: str = ""
    document_name: str = ""
    included: int = 0
    total: int = 0
    complete: bool = False
    ordered: bool = False


class QueryUsageOut(BaseModel):
    retrieved_chunks: int = 0
    reranked_chunks: int = 0
    reranker: str = "none"
    context_tokens: int = 0
    # Berapa potongan dokumen yang BENAR-BENAR sampai ke konteks (termasuk bagian pelengkap
    # supaya dokumennya utuh) - bukan sekadar berapa yang ditemukan pencarian.
    context_chunks: int = 0
    context_expanded_chunks: int = 0
    document_coverage: List[DocumentCoverageOut] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    # Alasan model berhenti menurut penyedia ("stop", "length", ...). "length" berarti jawaban
    # terpotong batas token keluaran - naikkan max_tokens, bukan menambah knowledge.
    finish_reason: str = ""
    retrieval_ms: float = 0.0
    rerank_ms: float = 0.0
    generation_ms: float = 0.0
    analytics_ms: float = 0.0
    computed_rows: int = 0
    rows_skipped: int = 0


class ComputedOut(BaseModel):
    """Hasil perhitungan deterministik atas tabel (angka dari kode, bukan dari LLM)."""

    model_config = ConfigDict(extra="allow")

    operation: str = ""
    metric: Optional[str] = None
    group_by: Optional[str] = None
    document_id: str = ""
    document_name: str = ""
    sheet: str = ""
    rows_total: int = 0
    rows_scanned: int = 0
    rows_matched: int = 0
    rows_skipped: int = 0
    scope: str = ""
    explanation: str = ""
    result: List[Dict[str, Any]] = Field(default_factory=list)


class QueryDataOut(BaseModel):
    answer: str
    grounded: bool
    sources: List[SourceOut] = Field(default_factory=list)
    usage: QueryUsageOut = Field(default_factory=QueryUsageOut)
    route: Optional[Dict[str, Any]] = None
    model: str = ""
    no_answer_reason: Optional[str] = None
    computed: Optional[ComputedOut] = None
    # Alasan perhitungan tabel tidak bisa dilakukan (mis. kolom tidak ada).
    table_note: Optional[str] = None


class SearchResultOut(BaseModel):
    document_id: str
    chunk_id: str
    content: str
    score: float
    page: Optional[int] = None
    document_name: str = ""
    section: str = ""
    source_url: str = ""


class SearchDataOut(BaseModel):
    results: List[SearchResultOut] = Field(default_factory=list)
    route: Optional[Dict[str, Any]] = None
    # Honest stage reporting: a search with the reranker off must not look reranked.
    hybrid: bool = True
    reranker: str = "none"
    retrieval_ms: float = 0.0


class ExtractDataOut(BaseModel):
    items: List[Any] = Field(default_factory=list)
    sources: List[SourceOut] = Field(default_factory=list)
    not_found: bool = False
    route: Optional[Dict[str, Any]] = None


class IndexDataOut(BaseModel):
    document_id: str
    status: Literal["queued", "processing", "completed", "failed", "deleted"]
    job_id: Optional[str] = None
    chunks: int = 0


class DocumentStatusOut(BaseModel):
    document_id: str
    document_name: str = ""
    status: str
    stage: str = ""
    knowledge_base_id: str = ""
    chunks: int = 0
    tokens: int = 0
    pages: int = 0
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    duration_ms: Optional[float] = None
    # Jumlah vektor di penyimpanan. -1 = tidak bisa dihitung (Qdrant lokal sesekali gagal
    # menghitung setelah penghapusan); ditampilkan sebagai "tidak diketahui", bukan 0 palsu.
    vectors_in_store: int = 0
    tables: int = 0


class HealthOut(BaseModel):
    status: str = "ok"


class ReadyOut(BaseModel):
    status: str
    dependencies: Dict[str, str] = Field(default_factory=dict)
    detail: Dict[str, Any] = Field(default_factory=dict)
