# Deploy lewat Dokploy (kroombox) — RAG Service

Ringkas: RAG Service di-deploy sebagai **Application** di Dokploy dengan sumber repo Git + Dockerfile,
satu volume untuk `/data`, dan **satu variabel `API_KEYS_JSON`** sebelum dipakai. Domain + TLS
diserahkan ke pemilik infra (Cloudflare / nginx / Traefik), jadi langkah itu tidak termasuk di sini.

## Prasyarat

- Dokploy dengan Docker Swarm aktif dan minimal satu **Git provider** terhubung, atau repo publik
  (bisa memakai provider `Git` + URL clone, tanpa kredensial).
- Volume untuk `/data` (wajib, supaya knowledge/vektor/tabel tidak hilang saat redeploy).
- Endpoint LLM & embedding kalau tidak memakai profil lokal image penuh:
  - `EMBEDDING_PROVIDER=http` → butuh layanan OpenAI-compatible yang menyediakan `/embeddings`
    (mis. Ollama `nomic-embed-text`, 768 dim) dan dapat dijangkau dari container.
  - `LLM_PROVIDER=openai_compatible` → butuh `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`.

## Langkah di UI Dokploy

1. **Create Application** di dalam project/environment yang diinginkan (mis. `rag-service`).
2. Tab **General**:
   - Provider: **GitHub** (pilih repo + branch) atau **Git** (isi URL `https://github.com/<owner>/<repo>.git`).
   - Build Type: **Dockerfile**, Dockerfile Path: `/Dockerfile`, Build Path: `/`.
   - Port/preview port: **8000** (port internal layanan, `APP_PORT`).
3. Tab **Advanced → Volumes**: mount volume ke **`/data`** (mis. volume name `rag-data`, mount path `/data`).
4. Tab **Environment**: isi variabel di tabel bawah. Minimal yang perlu diubah: `API_KEYS_JSON`,
   `EMBEDDING_PROVIDER`/`EMBEDDING_MODEL`, `LLM_*`, `JEV_ENABLED`.
5. **Deploy**. Build pertama memasang Python + MkDocs (image ramping tanpa torch), jadi beberapa menit.
6. Tab **Domains**: tambahkan domain setelah pemilik infra menyiapkan jalur masuk (Cloudflare Tunnel
   atau vhost nginx yang mem-proxy ke `127.0.0.1:<port>`/service). Port di Dokploy = 8000, Cert:
   `none` bila TLS diterminasi di Cloudflare.

## Variabel environment

| Variabel | Contoh / catatan |
|---|---|
| `APP_ENV` | `production` |
| `APP_PORT` | `8000` |
| `API_KEYS_JSON` | **wajib diisi**. JSON map: `{"<kunci>":{"user_id":"u1","organization_id":"org1","application_id":"app1","permissions":["knowledge:read","knowledge:write","search:read","tables:read","analytics:read"]}}`. Kosong = semua permintaan API ditolak 401 (bukan akses terbuka). |
| `API_KEYS_PATH` | `/data/api_keys.json` (sudah diset di image). Berkas kunci yang dibuat dari panel **Kunci API** di layar Pengaturan; isinya hash, bukan kunci. Biarkan di `/data` supaya bertahan saat redeploy. |
| `QDRANT_URL` | kosongkan untuk mode embedded (1 replika); isi `http://<host>:6333` bila memakai Qdrant server |
| `QDRANT_COLLECTION` | `knowledge_chunks` |
| `EMBEDDING_PROVIDER` | `hash` (uji cepat), `http` (Ollama/gateway), `fastembed`/`sentence_transformers` (profil image penuh) |
| `EMBEDDING_MODEL` | untuk `http`: mis. `nomic-embed-text`; `EMBEDDING_DIM` biarkan `0` (dideteksi) kecuali perlu dipaksa |
| `RERANKER_PROVIDER` / `RERANKER_ENABLED` | `none` / `false` pada image ramping |
| `LLM_PROVIDER` | `openai_compatible` (produksi) atau `mock` (uji) |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | endpoint OpenAI-compatible yang dijangkau container |
| `JEV_ENABLED` / `JEV_MODE` | `false` / `heuristic` sampai kunci Jev siap |
| `DOCS_SITE_DIR` | `/srv/site` (situs MkDocs sudah dibangun di image) |
| `MAX_UPLOAD_MB` | default `32`; jaga konsisten dengan `client_max_body_size` di reverse proxy |

## Verifikasi setelah deploy

```bash
# dari host yang menjalankan Dokploy (port internal container)
docker exec -it $(docker ps --filter name=rag-service --format '{{.Names}}' | head -1) \
  python scripts/deploy_smoke.py --base-url http://127.0.0.1:8000        # read-only
# atau, bila port sudah dipublikasikan/di-proxy:
python scripts/deploy_smoke.py --base-url http://<host>:<port> --api-key <kunci>
```

Yang harus 200 tanpa kunci: `/api/v1/health`, `/api/v1/ready`, `/docs`, `/redoc`, `/openapi.json`,
`/ui/`, `/guide/`. Yang harus **401 tanpa kunci**: `/api/v1/knowledge`, `/api/v1/search`,
`/api/v1/tables`. Bila ketiganya menjawab 200, `API_KEYS_JSON` belum diisi — hentikan sebelum domain
dipublikasikan.

## Catatan operasi

- **Data**: semua state ada di volume `/data` (`qdrant/`, `storage/`, `sparse/`, `registry.json`,
  `jobs.json`, `tables.sqlite`, `settings.json`). Backup = arsipkan volume.
- **Situs dokumentasi**: dibangun saat build (`bash scripts/build_docs.sh`) sehingga `/guide/` ikut
  tersaji; perubahan dokumentasi perlu rebuild image.
- **Upgrade**: `git pull`/commit baru → Deploy ulang. Volume tidak tersentuh.
- **Skala**: mode embedded Qdrant = satu proses. Untuk replika/worker lebih dari satu, pindah ke
  Qdrant server (`QDRANT_URL`) lebih dulu.
- **Profil lokal**: bila ingin embedding/reranker lokal di dalam image, build dengan
  `--build-arg WITH_LOCAL_MODELS=1` (image beberapa GB) lalu set `EMBEDDING_PROVIDER=fastembed`
  (ONNX, tanpa torch) atau `sentence_transformers` (butuh torch, sudah termasuk).
