# RAG Service — image produksi (API + konsol /ui + situs dokumentasi /guide).
#
#   docker build -t rag-service .                                   # ramping (disarankan)
#   docker build --build-arg WITH_LOCAL_MODELS=1 -t rag-service:full .
#
# WITH_LOCAL_MODELS=0 (default) -> tanpa torch/sentence-transformers, tetapi DENGAN fastembed
#     (ONNX) + model embedding multibahasa mpnet di /models: EMBEDDING_PROVIDER=fastembed.
#     Reranker: lexical (bawaan, digabung dengan skor makna).
# WITH_LOCAL_MODELS=1 -> memasang requirements.txt penuh (torch CPU + bge-m3/reranker lokal);
#     image beberapa GB lebih besar dan butuh RAM lebih.
#
# Data persisten SELALU di /data (pasang volume di sana): qdrant, storage, sparse, registry,
# jobs, tables.sqlite, settings.json. Tanpa volume, seluruh knowledge hilang saat container diganti.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface \
    APP_ENV=production

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

# --- dependensi runtime -----------------------------------------------------
ARG WITH_LOCAL_MODELS=0
COPY requirements.txt requirements-base.txt ./
RUN if [ "$WITH_LOCAL_MODELS" = "1" ]; then \
      pip install --index-url https://download.pytorch.org/whl/cpu torch \
      && pip install -r requirements.txt; \
    else \
      pip install -r requirements-base.txt; \
    fi

# --- model embedding semantik (diunduh saat build, bukan saat layanan menyala) ---
# mpnet multibahasa: paling akurat untuk pertanyaan berbahasa Indonesia pada evaluasi kami
# (sinonim "jatah libur pegawai" -> "kuota cuti karyawan"). Ganti lewat build-arg bila perlu.
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-mpnet-base-v2
ENV FASTEMBED_CACHE_PATH=/models
RUN python -c "from fastembed import TextEmbedding; m = TextEmbedding('${EMBEDDING_MODEL}'); print(len(list(m.embed(['siap']))[0]))"

# --- aplikasi --------------------------------------------------------------
COPY pyproject.toml README.md ./
COPY app ./app
COPY tests ./tests

# --- situs dokumentasi (MkDocs) dibangun di dalam image -> /srv/site -------
# Swagger (/docs), ReDoc (/redoc) dan /openapi.json selalu tersedia tanpa langkah ini.
COPY mkdocs.yml requirements-docs.txt ./
COPY docs ./docs
COPY scripts ./scripts
RUN pip install -r requirements-docs.txt \
 && bash scripts/build_docs.sh

# --- direktori data + default produksi -------------------------------------
RUN mkdir -p /data/storage /data/sparse /data/qdrant

ENV QDRANT_URL="" \
    QDRANT_LOCAL_PATH=/data/qdrant \
    STORAGE_DIR=/data/storage \
    SPARSE_DIR=/data/sparse \
    REGISTRY_PATH=/data/registry.json \
    JOB_STORE_PATH=/data/jobs.json \
    TABLE_STORE_PATH=/data/tables.sqlite \
    SETTINGS_OVERRIDE_PATH=/data/settings.json \
    API_KEYS_PATH=/data/api_keys.json \
    ACCESS_PATH=/data/access.json \
    SESSIONS_PATH=/data/sessions.json \
    DOCS_SITE_DIR=/srv/site \
    RERANKER_PROVIDER=lexical \
    RERANKER_ENABLED=true \
    EMBEDDING_PROVIDER=fastembed \
    EMBEDDING_FASTEMBED_MODEL=${EMBEDDING_MODEL}

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status==200 else 1)"

CMD ["python", "-m", "app.main"]
