"""Application configuration (PRD sections 8, 42, 43).

Every model / provider choice is an env var so the machine-specific adaptation
(low-RAM box vs production GPU box) never requires a code change.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Literal

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
    # ---- pertanyaan yang tidak terjawab (dikumpulkan untuk ditinjau operator) ----
    unanswered_enabled: bool = True
    # Alasan "tidak terjawab" yang dicatat (lihat no_answer_reason di /query).
    unanswered_reasons: str = "no_candidates,below_threshold,strict_grounding,context_empty"
    unanswered_retention_days: int = 90
    unanswered_max_entries: int = 5000
    # Kosong = di samping TABLE_STORE_PATH (volume data yang sama).
    unanswered_store_path: str = ""
    settings_override_path: str = str(BASE_DIR / "data" / "settings.json")
    # Registry kunci API yang dibuat dari layar Pengaturan (lihat app/core/api_keys.py).
    # Berisi hash kunci, bukan kuncinya; mode 0600.
    api_keys_path: str = str(BASE_DIR / "data" / "api_keys.json")

    # ---- service ---------------------------------------------------------
    app_env: str = "development"
    app_port: int = 8000
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    cors_origins_env: str = Field(default="", alias="CORS_ORIGINS")  # comma-separated; empty = same-origin only
    # Situs dokumentasi (hasil `mkdocs build`) disajikan di /guide bila direktorinya ada.
    # Lihat docs/deployment.md dan docs/reuse.md; kosongkan untuk mematikan mount ini.
    docs_site_dir: str = str(BASE_DIR / "site")

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
    embedding_fastembed_model: str = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    # Diisi otomatis selama migrasi embedding: koleksi sumber yang masih disalin (penghapusan
    # ikut diterapkan di sana). Bukan untuk disetel manual.
    qdrant_migration_source: str = ""
    embedding_dim: int = 0                    # 0 => detected from the model
    embedding_batch_size: int = 16
    embedding_device: str = "cpu"             # cpu | cuda
    embedding_max_length: int = 1024

    # ---- reranker (PRD 8.3) ----------------------------------------------
    # ``lexical`` = reranker bawaan tanpa dependensi (lihat app/rag/reranker.py): skor silang
    # berbasis IDF + bonus frasa + kedekatan kata. Ini yang dipakai bila model neural tidak
    # terpasang - jauh lebih baik daripada ``none`` yang hanya meneruskan urutan fusi.
    # ``none`` tetap tersedia sebagai pilihan sadar untuk mematikan reranking.
    reranker_provider: Literal["sentence_transformers", "fastembed", "lexical", "none"] = "lexical"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    # fastembed has no ONNX port of bge-reranker-v2-m3; this is its multilingual equivalent
    reranker_fastembed_model: str = "jinaai/jina-reranker-v2-base-multilingual"
    reranker_device: str = "cpu"
    # Reranker neural: pasangan per batch & batas karakter per potongan (hemat memori/latensi).
    reranker_batch_size: int = 8
    reranker_max_chars: int = 2000
    # Bila provider neural diminta tetapi pustakanya tidak ada di image, jangan gagal: turun ke
    # reranker leksikal dan laporkan penggantinya (agar operator tahu kualitasnya bukan neural).
    reranker_fallback_to_lexical: bool = True
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
    final_top_k: int = 12
    rrf_k: int = 60
    rerank_threshold: float = 0.35
    strict_grounding: bool = True
    # aliases used by the retrieval module (env: RETRIEVAL_DENSE_TOP_K, ...)
    retrieval_dense_top_k: int = 60
    retrieval_sparse_top_k: int = 60
    retrieval_hybrid: bool = True
    retrieval_dense_enabled: bool = True
    dense_weight: float = 1.0
    # Bobot maksimum sisi vektor bila embedder masih ``hash`` (penghitung kata, bukan makna).
    hash_dense_weight: float = 0.25
    # Ambang RELATIF: kandidat di bawah (terbaik x nilai ini) dibuang sebagai derau ekor daftar.
    relevance_threshold: float = 0.35
    # Ambang ABSOLUT kandidat terbaik (skor reranker 0..1): di bawah ini layanan menjawab "tidak
    # ditemukan" tanpa memanggil LLM. 0 = matikan. Dengan embedder semantik, kemiripan vektor
    # >= semantic_min_similarity juga dianggap cukup relevan.
    min_relevance: float = 0.28
    semantic_min_similarity: float = 0.45
    # Penggabungan skor bila embedder semantik: bobot makna vs kata kunci, dan kalibrasi kosinus
    # (di bawah floor = tak berhubungan, di atas ceil = sangat mirip). Dikalibrasi untuk mpnet.
    semantic_weight: float = 0.6
    semantic_floor: float = 0.25
    semantic_ceil: float = 0.80
    reranker_enabled: bool = True
    reranker_candidates: int = 120
    # Fokus dokumen: hanya dokumen dengan skor terbaik >= (dokumen teratas x rasio), paling banyak
    # N dokumen. Mencegah konteks bercampur saat knowledge berisi ribuan dokumen.
    document_focus_ratio: float = 0.8
    max_context_documents: int = 3
    # Berapa banyak potongan dari SATU dokumen yang boleh masuk konteks. Batas kecil membuat
    # pertanyaan "seluruh isi dokumen ini" mustahil dijawab: pertanyaan seperti itu butuh
    # dokumennya utuh, bukan tiga potongan paling mirip.
    max_chunks_per_document: int = 8
    # Indeks BM25 versi lama dibangun ulang dari Qdrant saat layanan menyala (latar belakang).
    sparse_rebuild_on_startup: bool = True
    answer_language: Literal["id", "en"] = "id"
    # Buang aksara dari tulisan lain (China, Jepang, Korea, Arab, Kiril, Thai) yang DISELIPKAN
    # model ke jawaban/ringkasan. Aksara yang memang ada di dokumen tetap utuh - perbandingan
    # dengan konteks yang menentukan. Matikan bila knowledge memang berbahasa/beraksara lain.
    text_strip_foreign: bool = True

    # ---- konteks yang dikirim ke LLM (PRD 12) ----------------------------
    # Anggaran token untuk blok RETRIEVED_CONTEXT. Ini yang menentukan berapa bagian dokumen
    # benar-benar terbaca model; terlalu kecil = jawaban "tidak lengkap" walau datanya ada.
    # 24000 dipilih dari pengukuran nyata: PDF "Struktur Lengkap Database KMS Telin" (20
    # halaman, 50k karakter) menjadi 18.5k token, jadi dokumen sekelas itu muat UTUH dalam
    # satu panggilan. Model berjendela kecil bisa menurunkannya dari panel Ambil (tanpa redeploy).
    context_max_tokens: int = 24000
    # Sertakan sisa potongan dokumen yang terambil (urutan dokumen) supaya pertanyaan yang
    # menyangkut satu dokumen utuh bisa dijawab lengkap, bukan hanya potongan teratas.
    context_expand_documents: bool = True
    context_expand_max_documents: int = 3
    context_expand_min_chunks: int = 2      # hanya dokumen dengan >= N potongan di konteks
    # Potongan tetangga (sebelum & sesudah) yang ikut di sekitar setiap hasil pencarian. Kalimat
    # sering terpotong di batas potongan; tetangganya melengkapi tanpa membanjiri konteks.
    context_neighbor_chunks: int = 1
    # Dokumen kecil (total token <= nilai ini) disertakan utuh: murah, dan jawabannya lengkap.
    context_full_document_tokens: int = 3000

    # ---- generation (PRD 8.1, 17) ----------------------------------------
    llm_provider: Literal["openai_compatible", "ollama", "mock"] = "mock"
    llm_base_url: str = "https://9router.yanto.top/v1"
    llm_api_key: str = ""
    # "Qwen/Qwen3-4B" = default sisa target PRD (server lokal). Layanan tidak terikat model:
    # nama model dibaca per panggilan, jadi setelan/env/UI bisa memakai model apa pun
    # yang disediakan endpoint OpenAI-compatible.
    llm_model: str = "Qwen/Qwen3-4B"
    llm_timeout: float = 120.0
    # Batas token keluaran. 1024 terlalu kecil untuk pertanyaan yang menyangkut dokumen besar:
    # model berhenti di tengah jalan dan jawabannya kosong - yang lalu terbaca seolah datanya
    # tidak ada. Diukur pada dokumen KMS Telin asli: daftar seluruh tabel + kolomnya menuntut
    # 5.941 token keluaran, jadi 4096 pun masih terpotong; 8192 menyelesaikannya (finish_reason=stop).
    llm_max_tokens: int = 8192
    llm_temperature: float = 0.1
    # Sampling. Sebelumnya kedua nilai ini TIDAK pernah dikirim, jadi endpoint memakai
    # bawaannya sendiri. top_p yang lebih rapat memangkas ekor distribusi - tempat kata aneh
    # seperti "pemb.cgiian" / "praktikumaccording" (campur bahasa, kata terpotong) berasal.
    # 0 = jangan kirim (biarkan endpoint memutuskan).
    llm_top_p: float = 0.9
    # 0: penalti frekuensi membuat model menghindari token yang memang harus berulang (angka,
    # nama kolom, "Rp", tahun) - merugikan jawaban faktual dan tabel.
    llm_frequency_penalty: float = 0.0
    llm_presence_penalty: float = 0.0
    # Perbaikan jawaban yang terdeteksi rusak (kata tercampur/terpotong). 0 = matikan.
    llm_repair_attempts: int = 1
    llm_context_chars: int = 96000

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

    # ---- kode akses konsol + sesi browser --------------------------------
    # Satu kode akses (passcode) membuka konsol tanpa perlu menempel API key. Kode disimpan
    # sebagai hash di berkas; sesi yang "ingat saya" berlaku ui_remember_days hari.
    access_path: str = str(BASE_DIR / "data" / "access.json")
    sessions_path: str = str(BASE_DIR / "data" / "sessions.json")
    ui_session_hours: int = 12
    ui_remember_days: int = 7
    ui_session_organization_id: str = "default"
    ui_session_user_id: str = "operator"
    ui_session_application_id: str = "rag-console"
    # Izin yang diberikan ke sesi konsol: konsol adalah alat operator, jadi bawaannya penuh.
    ui_session_permissions: str = "read,write,admin,*"
    # Konsol tanpa gerbang kode akses: cukup API key. Saat true, layar konsol tidak meminta
    # kode akses, dan kunci API apa pun yang sah membuka seluruh layar Pengaturan (termasuk
    # membuat kunci, memasang kode akses, mengubah model, dan setelan lainnya). Bawaannya true
    # karena ini pemasangan satu-operator; setel false bila konsol harus dijaga kode akses dan
    # hanya kunci berizin 'admin' boleh membuka Pengaturan.
    console_api_key_only: bool = True
    # Kunci admin pertama saat layanan belum punya kunci sama sekali (lihat app/core/bootstrap.py).
    # Nilainya ditulis ke berkas mode 0600, bukan ke log; cabut lewat panel Kunci API setelah
    # kode akses dipasang. Matikan dengan BOOTSTRAP_ADMIN_KEY=false.
    # Kosong = ikut direktori API_KEYS_PATH, supaya berkasnya hidup di volume data yang sama
    # (penting untuk container: berkas di dalam image hilang setiap redeploy).
    bootstrap_admin_key: bool = True
    bootstrap_admin_key_path: str = ""

    # ---- file handling (PRD 34) ------------------------------------------
    max_upload_mb: int = 32
    allowed_mime: str = "application/pdf,text/plain,text/markdown,text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/json"
    storage_dir: str = str(BASE_DIR / "data" / "storage")
    sparse_dir: str = str(BASE_DIR / "data" / "sparse")
    registry_path: str = str(BASE_DIR / "data" / "registry.json")
    max_pages: int = 2000
    file_fetch_timeout: float = 120.0
    job_store_path: str = str(BASE_DIR / "data" / "jobs.json")

    # ---- sumber dari web (crawl) ----------------------------------------
    # "Knowledge dari web": satu URL diambil, tautan di dalamnya diikuti sampai kedalaman
    # tertentu, dan SETIAP halaman jadi dokumen tersendiri. Bawaannya konservatif supaya satu
    # permintaan tidak menjelajahi seluruh situs tanpa disadari.
    web_crawl_enabled: bool = True
    web_crawl_max_pages: int = 20
    web_crawl_max_depth: int = 2
    web_crawl_same_host: bool = True
    web_crawl_timeout: float = 20.0
    web_crawl_max_page_bytes: int = 5 * 1024 * 1024
    # Ambil juga dokumen yang ditautkan (PDF/DOCX/...) di halaman web, bukan hanya HTML.
    web_crawl_follow_files: bool = True
    # Hormati robots.txt? Bawaannya ya. Dimatikan hanya untuk situs milik sendiri.
    web_crawl_respect_robots: bool = True
    # Izinkan URL yang menunjuk alamat privat/internal. MATI secara bawaan: menyalakannya
    # membuka SSRF (server disuruh membaca jaringan dalam). Nyalakan hanya untuk intranet
    # yang memang tepercaya.
    allow_private_urls: bool = False
    # Agen pengguna saat mengambil halaman web (disebut jujur, bukan menyamar).
    web_user_agent: str = "RAG-Service/1.0 (+knowledge-fetcher)"

    # ---- ringkasan knowledge turunan -------------------------------------
    # Saat dokumen diindeks, isinya diringkas dan ringkasannya diindeks sebagai potongan
    # tersendiri (document_id sama). Pertanyaan "ringkas dokumen ini" lalu dijawab dari satu
    # potongan padat, bukan dari sebagian potongan hasil pencarian kemiripan.
    document_summary_enabled: bool = True
    # Jendela token per kelompok saat meringkas dokumen besar (map-reduce).
    summary_window_tokens: int = 12000
    # Batas panjang ringkasan yang diminta dari model (jawaban panjang butuh ruang).
    summary_max_tokens: int = 2048
    # Dokumen dengan potongan lebih dari ini hanya diringkas sebagian (dilaporkan apa adanya).
    summary_max_parts: int = 400
    # Berapa dokumen yang ringkasannya boleh ikut saat pertanyaannya minta ringkasan.
    summary_max_documents: int = 3
    # Batas WAKTU membuat ringkasan (detik). Ringkasan dokumen besar bisa memakan puluhan
    # panggilan model, dan karena pengindeksan berjalan berurutan, itu menahan unggahan lain
    # di antrian. Setelah batas ini tercapai, ringkasan dihentikan dan alasannya dilaporkan -
    # isi dokumen sudah tersimpan lebih dulu, jadi tidak ada yang hilang.
    summary_budget_seconds: float = 120.0
    # Batas jumlah kelompok (panggilan map) supaya dokumen raksasa tidak menjelajah tanpa ujung.
    summary_max_stages: int = 12

    # Ekstensi yang boleh jadi knowledge (lihat app/parsing/formats.py).
    # Kosong = pakai daftar default katalog, dikurangi format yang belum tersedia di mesin ini.
    upload_extensions: str = ""

    # ---- worker (PRD 33) -------------------------------------------------
    worker_concurrency: int = 1
    worker_poll_seconds: float = 0.5
    indexing_workers: int = 1
    # Batas antrean pengindeksan (jumlah job dan total isi base64 yang menunggu di RAM). Lewat
    # batas, unggahan baru ditolak 429 "coba lagi" alih-alih membuat container kehabisan memori.
    indexing_max_queued: int = 1000
    indexing_max_queued_mb: int = 1024
    # Jalur terpisah untuk membuat ringkasan. Ringkasan memanggil LLM (puluhan kali untuk
    # dokumen besar), jadi kalau ia dikerjakan di worker indeks yang sama, unggahan lain
    # mengantri menunggu. Dengan jalur sendiri, isi dokumen tetap diproses berurutan cepat
    # dan ringkasan menyusul tanpa menahan antrian.
    summary_workers: int = 2

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb) * 1024 * 1024

    @property
    def context_token_budget(self) -> int:
        """Anggaran token blok konteks untuk LLM.

        ``CONTEXT_MAX_TOKENS`` bila diisi; kalau 0, diturunkan dari ``LLM_CONTEXT_CHARS``
        (≈4 karakter per token) supaya pemasangan lama tetap punya batas yang masuk akal.
        """
        if self.context_max_tokens and self.context_max_tokens > 0:
            return max(512, int(self.context_max_tokens))
        return max(512, int(self.llm_context_chars) // 4)

    @property
    def cors_origins(self) -> List[str]:
        """Opt-in cross-origin access for the test console (empty = same-origin only)."""
        return [origin.strip() for origin in self.cors_origins_env.split(",") if origin.strip()]

    @property
    def ui_session_permission_list(self) -> List[str]:
        """Izin sesi konsol. Kosong = sesi tidak berguna, jadi jatuh ke read+write."""
        raw = [item.strip() for item in self.ui_session_permissions.split(",") if item.strip()]
        return raw or ["read", "write"]

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
        for file_path in (
            self.registry_path,
            self.job_store_path,
            self.settings_override_path,
            self.api_keys_path,
            self.access_path,
            self.sessions_path,
        ):
            if file_path:
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
