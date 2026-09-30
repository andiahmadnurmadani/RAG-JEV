"""Application configuration (PRD sections 8, 42, 43).

Every model / provider choice is an env var so the machine-specific adaptation
(low-RAM box vs production GPU box) never requires a code change.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- runtime overrides (settings screen) ----------------------------
    table_store_path: str = "data/tables.sqlite"
    table_analytics_enabled: bool = True
    settings_override_path: str = str(BASE_DIR / "data" / "settings.json")

    # ---- service ---------------------------------------------------------
    app_env: str = "development"
    app_port: int = 8000
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins_env: str = Field(default="", alias="CORS_ORIGINS")  # comma-separated; empty = same-origin only

    # ---- vector store (PRD 8.4, 9) ---------------------------------------
    qdrant_url: str = ""                     # empty => embedded/local mode
    qdrant_api_key: str = ""
    qdrant_local_path: str = str(BASE_DIR / "data" / "qdrant")
    qdrant_collection: str = "knowledge_chunks"
    qdrant_timeout: float = 30.0

    # ---- embedding (PRD 8.2) ---------------------------------------------
    # sentence_transformers -> PRD-exact BAAI/bge-m3 (torch, ~2.3 GB RAM)
    # fastembed             -> ONNX int8 multilingual, light-RAM fallback
    # http                  -> external OpenAI-compatible /embeddings endpoint
    embedding_provider: Literal["sentence_transformers", "fastembed", "http", "hash"] = "fastembed"
    embedding_model: str = "BAAI/bge-m3"
    embedding_fastembed_model: str = "intfloat/multilingual-e5-large"
    embedding_dim: int = 0                    # 0 => detected from the model
    embedding_batch_size: int = 16
    embedding_device: str = "cpu"             # cpu | cuda
    embedding_max_length: int = 1024

    # ---- reranker (PRD 8.3) ----------------------------------------------
    reranker_provider: Literal["sentence_transformers", "fastembed", "none"] = "fastembed"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # fastembed has no ONNX port of bge-reranker-v2-m3; this is its multilingual equivalent
    reranker_fastembed_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    reranker_device: str = "cpu"
    # both        -> keep embedder + reranker resident (needs >= ~6 GB free)
    # sequential  -> load one at a time (low-RAM machines)
    model_residency: Literal["both", "sequential"] = "sequential"

    # ---- retrieval (PRD 11-16) -------------------------------------------
    chunk_size: int = 700                     # tokens (approx)
    chunk_overlap: int = 100
    min_chunk_tokens: int = 40
    dense_top_k: int = 30
    sparse_top_k: int = 30
    fusion_top_k: int = 40
    final_top_k: int = 5
    rrf_k: int = 60
    rerank_threshold: float = 0.35
    strict_grounding: bool = True
    # aliases used by the retrieval module (env: RETRIEVAL_DENSE_TOP_K, ...)
    retrieval_dense_top_k: int = 30
    retrieval_sparse_top_k: int = 30
    retrieval_hybrid: bool = True
    retrieval_dense_enabled: bool = True
    dense_weight: float = 1.0
    relevance_threshold: float = 0.35
    reranker_enabled: bool = True
    reranker_candidates: int = 40
    max_chunks_per_document: int = 3
    answer_language: Literal["id", "en"] = "id"

    # ---- generation (PRD 8.1, 17) ----------------------------------------
    llm_provider: Literal["openai_compatible", "ollama", "mock"] = "mock"
    llm_base_url: str = "https://9router.yanto.top/v1"
    llm_api_key: str = ""
    # "Qwen/Qwen3-4B" = default sisa target PRD (server lokal). Layanan tidak terikat model:
    # nama model dibaca per panggilan, jadi setelan/env/UI bisa memakai model apa pun
    # yang disediakan endpoint OpenAI-compatible.
    llm_model: str = "Qwen/Qwen3-4B"
    llm_timeout: float = 120.0
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.1
    llm_context_chars: int = 12000

    # ---- Jev orchestration (PRD 18, 19) ----------------------------------
    jev_mode: Literal["live", "heuristic", "off"] = "heuristic"
    jev_url: str = "https://www.jevai.org/api/mcp"
    jev_api_key: str = ""
    jev_timeout: float = 20.0                  # <= tool timeout we advertise
    jev_failure_threshold: int = 5             # circuit breaker
    jev_cooldown_seconds: int = 120
    jev_enabled: bool = True
    jev_mcp_url: str = "https://www.jevai.org/api/mcp"
    jev_route_tool: str = "jev_route_task"
    jev_health_tool: str = "jev_check_research"
    # Jev speaks two different protocols and they are not interchangeable:
    #   mcp       -> jevai.org MCP server, 6 tools/call (JSON-RPC)
    #   systemone -> native decision endpoint POST {state, model, questions} (9Router,
    #                api.typesafe.ai, OpenRouter /api/v1/systemone)
    jev_provider: Literal["mcp", "systemone"] = "mcp"
    jev_systemone_url: str = ""
    jev_model: str = ""
    jev_health_cache_seconds: int = 300

    # ---- auth / tenant (PRD 6, 21, 34) -----------------------------------
    # JSON map: {"<api-key>": {"user_id":..,"organization_id":..,...}}
    api_keys_json: str = ""
    # HS256 shared secret with KMS/API gateway for X-Tenant-Context tokens
    kms_shared_secret: str = ""
    tenant_context_header: str = "X-Tenant-Context"
    require_tenant_context_token: bool = False
    rate_limit_per_minute: int = 240

    # ---- file handling (PRD 34) ------------------------------------------
    max_upload_mb: int = 32
    allowed_mime: str = "application/pdf,text/plain,text/markdown,text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/json"
    storage_dir: str = str(BASE_DIR / "data" / "storage")
    sparse_dir: str = str(BASE_DIR / "data" / "sparse")
    registry_path: str = str(BASE_DIR / "data" / "registry.json")
    max_pages: int = 2000
    file_fetch_timeout: float = 120.0
    job_store_path: str = str(BASE_DIR / "data" / "jobs.json")

    # Ekstensi yang boleh jadi knowledge (lihat app/parsing/formats.py).
    # Kosong = pakai daftar default katalog, dikurangi format yang belum tersedia di mesin ini.
    upload_extensions: str = ""

    # ---- worker (PRD 33) -------------------------------------------------
    worker_concurrency: int = 1
    worker_poll_seconds: float = 0.5
    indexing_workers: int = 1

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb) * 1024 * 1024

    @property
    def cors_origins(self) -> List[str]:
        """Opt-in cross-origin access for the test console (empty = same-origin only)."""
        return [origin.strip() for origin in self.cors_origins_env.split(",") if origin.strip()]

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_lt_size(cls, v: int, info) -> int:
        size = info.data.get("chunk_size", 700)
        if v >= size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return v

    # ------------------------------------------------------------------ #
    @property
    def api_keys(self) -> Dict[str, dict]:
        """Parse the API-key registry. Values are the *trusted* tenant facts."""
        if not self.api_keys_json.strip():
            return {}
        data = json.loads(self.api_keys_json)
        if not isinstance(data, dict):
            raise ValueError("API_KEYS_JSON must be an object keyed by API key")
        out: Dict[str, dict] = {}
        for key, ctx in data.items():
            if not isinstance(ctx, dict):
                raise ValueError("each API key entry must be an object")
            for required in ("user_id", "organization_id", "application_id"):
                if not ctx.get(required):
                    raise ValueError(f"API key entry missing '{required}'")
            ctx.setdefault("permissions", [])
            out[key] = ctx
        return out

    @property
    def allowed_mime_list(self) -> List[str]:
        return [m.strip().lower() for m in self.allowed_mime.split(",") if m.strip()]

    def ensure_dirs(self) -> None:
        for path in (self.storage_dir, self.sparse_dir, self.qdrant_local_path):
            Path(path).mkdir(parents=True, exist_ok=True)
        Path(self.registry_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
