FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
# CPU-only torch keeps the image ~2 GB smaller than the default CUDA wheels.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
 && pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY pyproject.toml ./

RUN mkdir -p /data/storage /data/sparse /data/qdrant
ENV QDRANT_LOCAL_PATH=/data/qdrant \
    STORAGE_DIR=/data/storage \
    SPARSE_DIR=/data/sparse \
    JOB_STORE_PATH=/data/jobs.json

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).status==200 else 1)"

CMD ["python", "-m", "app.main"]
