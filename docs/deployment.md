# Panduan Deploy & Menjalankan Layanan

Dokumen ini untuk **operator**: cara menjalankan layanan di laptop, memasangnya di server
(Ubuntu + systemd + nginx), sampai operasi harian (backup, upgrade, rotasi kunci, troubleshooting).

Berkas pendamping di repositori:
- `Dockerfile`, `docker-compose.yml` — jalur container (API + Qdrant).
- `deploy/rag-service.service` — unit systemd siap pakai.
- `deploy/nginx-rag.conf` — contoh reverse proxy + TLS.
- `scripts/run_live.sh` — jalur lokal cepat (Windows/WSL) dengan kunci dibaca dari `.env` Hermes.
- `scripts/deploy_smoke.py` — pemeriksaan pasca-deploy (health/ready/auth/kueri) tanpa mengubah data.
- `scripts/live_verify.py` — verifikasi end-to-end yang mengunggah dokumen sungguhan.

---

## 0. Ringkasan cepat (paling sering dipakai)

```bash
# 1) siapkan venv + dependensi
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
uv pip install --python .venv/bin/python -r requirements.txt

# 2) konfigurasi
cp .env.example .env && ${EDITOR:-nano} .env

# 3) jalankan (host 0.0.0.0, port dari APP_PORT, default 8000)
.venv/bin/python -m app.main
# API  : http://<host>:8000/api/v1/…
# Konsole: http://<host>:8000/ui/   (root / -> redirect ke /ui/)
```

Status yang harus hijau:

```bash
curl -s localhost:8000/api/v1/health   # {"status":"ok"}
curl -s localhost:8000/api/v1/ready    # per-dependensi: qdrant/embedding/reranker/llm/jev
```

---

## 1. Prasyarat

| Kebutuhan | Keterangan |
|---|---|
| Python | **≥ 3.11** (`pyproject.toml: requires-python = ">=3.11"`); diuji di 3.11 Windows + WSL |
| Paket Python | `uv` (disarankan) atau `venv` + `pip`; `build-essential` (untuk paket biner) |
| RAM | Profil hemat (`fastembed`/`hash`) **≥ 2 GB**; profil PRD (`sentence_transformers` BGE-M3 + reranker) **≥ 6 GB** |
| Disk | Kode ~50 MB + cache model 0,5–2,5 GB + data (Qdrant/sparse/tabel/dokumen) |
| GPU | Opsional. Set `EMBEDDING_DEVICE=cuda` / `RERANKER_DEVICE=cuda` bila tersedia |
| Layanan luar | Endpoint LLM OpenAI-compatible (vLLM/Ollama/9Router/…). Jev opsional |
| Qdrant | **Tidak wajib**: kosongkan `QDRANT_URL` → mode embedded satu proses. Untuk produksi multi-pengguna disarankan Qdrant server |

---

## 2. Pilih profil sumber daya

Tiga profil yang sudah terbukti jalan. Pilih satu, jangan campur tanpa alasan.

| Profil | `EMBEDDING_PROVIDER` | `RERANKER_PROVIDER` | `LLM_PROVIDER` | Catatan |
|---|---|---|---|---|
| **A. Uji API tanpa model** | `hash` | `none` | `mock` | Paling cepat; jawaban tidak memakai LLM. Untuk uji tanpa model |
| **B. Produksi hemat RAM** | `fastembed` (ONNX int8) atau `hash` | `fastembed` atau `none` | `openai_compatible` | Bisa jalan di VPS 2 GB. `hash` = embedding deterministik tanpa unduh model |
| **C. Produksi PRD-exact** | `sentence_transformers` (`BAAI/bge-m3`) | `sentence_transformers` (`BAAI/bge-reranker-v2-m3`) | `openai_compatible` | Kualitas tertinggi; butuh torch + ≥ 6 GB RAM (atau GPU) |

Catatan penting tentang mode `hash`: dipakai untuk uji, **bukan** untuk kualitas pencarian.
Kalau `RERANKER_PROVIDER=none`, ingat set `RERANKER_ENABLED=false`.
`MODEL_RESIDENCY=sequential` (default) menghemat RAM: embedder dan reranker tidak ditahan bersamaan.

---

## 3. Memasang dari kode

```bash
sudo mkdir -p /opt/rag-service && sudo chown "$USER" /opt/rag-service
git clone <url-repo> /opt/rag-service && cd /opt/rag-service

# venv (uv)
curl -LsSf https://astral.sh/uv/install.sh | sh        # sekali per mesin
uv venv .venv --python 3.11
# torch CPU dulu supaya tidak menarik wheel CUDA ~2 GB
uv pip install --python .venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
uv pip install --python .venv/bin/python -r requirements.txt

# alternatif tanpa uv
python3.11 -m venv .venv && .venv/bin/pip install -U pip
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt
```

Windows (dev): sama, tetapi interpreter `.venv/Scripts/python.exe` — lihat `scripts/run_live.sh`
yang sudah menangani hal ini.

---

## 4. Konfigurasi `.env`

Semua variabel ada di `.env.example` (satu berkas, lengkap). **`.env` tidak pernah masuk repo**
(`.gitignore` memuat `.env*`) dan sebaiknya ber-permission `0600`.

Contoh minimal untuk produksi hemat RAM:

```dotenv
APP_ENV=production
APP_PORT=8000
LOG_LEVEL=INFO
API_PREFIX=/api/v1
CORS_ORIGINS=                       # kosong = same-origin saja (UI di /ui/ sudah same-origin)

# penyimpanan
QDRANT_URL=http://127.0.0.1:6333    # kosongkan untuk mode embedded (lihat §5)
QDRANT_COLLECTION=knowledge_chunks
STORAGE_DIR=/opt/rag-service/data/storage
SPARSE_DIR=/opt/rag-service/data/sparse
REGISTRY_PATH=/opt/rag-service/data/registry.json
JOB_STORE_PATH=/opt/rag-service/data/jobs.json
TABLE_STORE_PATH=/opt/rag-service/data/tables.sqlite
SETTINGS_OVERRIDE_PATH=/opt/rag-service/data/settings.json

# model lokal
EMBEDDING_PROVIDER=fastembed
RERANKER_PROVIDER=fastembed
RERANKER_ENABLED=true

# generator (endpoint OpenAI-compatible apa pun)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://<host-gateway>/v1
LLM_API_KEY=<rahasia>
LLM_MODEL=<id-model-yang-ada-di-katalog-gateway>
LLM_TIMEOUT=120

# Jev (lapisan keputusan; opsional)
JEV_ENABLED=true
JEV_MODE=live
JEV_PROVIDER=systemone
JEV_SYSTEMONE_URL=http://127.0.0.1:20128/v1/systemone
JEV_MODEL=<id-model-jev>
JEV_API_KEY=<rahasia>

# keamanan
API_KEYS_JSON={"kunci-org-a":{"user_id":"u1","organization_id":"org_a","application_id":"app_ui","permissions":["read","write","admin"]}}
RATE_LIMIT_PER_MINUTE=240
MAX_UPLOAD_MB=32
INDEXING_WORKERS=1
```

Urutan prioritas konfigurasi: **nilai env** dimuat lebih dulu, lalu **berkas override**
(`SETTINGS_OVERRIDE_PATH`) yang ditulis oleh layar Pengaturan di-*apply* saat startup dan
**menimpa** env. Jadi kalau layar Pengaturan pernah menyimpan model LLM, ubah dari UI atau
hapus dulu berkas override-nya — bukan hanya mengedit `.env`.

---

## 5. Qdrant: embedded atau server

| Mode | Cara | Kapan dipakai | Batasan |
|---|---|---|---|
| **Embedded (default)** | `QDRANT_URL=` (kosong) + `QDRANT_LOCAL_PATH=/…/qdrant` | 1 proses, 1 instans, uji & produksi kecil | **Satu proses saja** (`AlreadyLocked` bila dobel) dan **tidak thread-safe** untuk kueri paralel berat. Jangan jalankan `uvicorn --workers > 1` |
| **Server** | `QDRANT_URL=http://127.0.0.1:6333` (+ `QDRANT_API_KEY`) | Produksi, banyak pengguna | Butuh container/layanan Qdrant |

Server Qdrant cepat dipasang:

```bash
docker run -d --name qdrant --restart unless-stopped \
  -p 6333:6333 -v qdrant_data:/qdrant/storage qdrant/qdrant:latest
```

Koleksi dibuat otomatis saat startup (`ensure_collection()`); tidak perlu buat manual.

---

## 6. Kunci API multi-tenant

`organization_id` **selalu** datang dari konteks tepercaya (kunci API), tidak pernah dari body/prompt.
Format `API_KEYS_JSON` = peta `kunci → konteks`:

```json
{
  "kunci-org-a": {"user_id": "u1", "organization_id": "org_a", "application_id": "app_ui",
                  "permissions": ["read", "write", "admin"]},
  "kunci-org-b": {"user_id": "u2", "organization_id": "org_b", "application_id": "app_web",
                  "permissions": ["read", "write"]}
}
```

- `read` — daftar/cari/tanya; `write` — unggah/ubah/hapus; `admin` — ubah setelan (LLM/Jev/katalog format).
- Praktik aman: taruh di berkas di luar repo, lalu
  `API_KEYS_JSON="$(tr -d '\n' < /etc/rag-service/api_keys.json)"` (pola ini dipakai `scripts/run_live.sh`).
- Selain kunci, tersedia jalur **tenant context bertanda tangan**: `KMS_SHARED_SECRET`,
  `TENANT_CONTEXT_HEADER` (default `X-Tenant-Context`), `REQUIRE_TENANT_CONTEXT_TOKEN`.
- Kunci di header: `Authorization: Bearer <kunci>`. Tanpa kunci → `401 AUTH_INVALID`;
  kunci tanpa izin → `403 AUTH_FORBIDDEN` (+ `details.permission`).

---

## 7. Menjalankan & menguji

```bash
# jalankan langsung
.venv/bin/python -m app.main           # host 0.0.0.0, port APP_PORT, log LOG_LEVEL

# atau uvicorn eksplisit (untuk dev)
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Perlu diingat: jangan menambah `--workers` selama Qdrant embedded dan rate limiter masih in-process.

Uji cepat (semua perintah ini sudah dijalankan pada instans hidup dan hasilnya sesuai):

```bash
curl -s localhost:8000/api/v1/health            # {"status":"ok"}
curl -s localhost:8000/api/v1/ready             # ringkasan per-dependensi
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/ui/          # 200 (konsol)
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/knowledge   # 401 tanpa kunci
K="kunci-org-a"
curl -s -H "Authorization: Bearer $K" localhost:8000/api/v1/knowledge
curl -s -H "Authorization: Bearer $K" localhost:8000/api/v1/metrics
```

Uji unit/integrasi (tanpa model besar):

```bash
.venv/bin/python -m pytest -q
```

Pemeriksaan pasca-deploy (tidak mengubah data):

```bash
BASE_URL=http://localhost:8000 API_KEY=kunci-org-a .venv/bin/python scripts/deploy_smoke.py
# tulis juga: "… --with-write" untuk sekaligus menguji unggah+kueri pada KB uji
```

Hasil pada instans uji (Windows, port 8099, `EMBEDDING_PROVIDER=hash`):

```
read-only   : 10/10 pemeriksaan lulus
--with-write: 14/14 pemeriksaan lulus  (index 202 → completed → query "Biru laut [1]." → delete)
```

Skrip ini tidak mengubah data pada mode default; mode `--with-write` menambahkan lalu menghapus
dokumen ujinya sendiri (diverifikasi: daftar dokumen kembali ke jumlah semula).

---

## 8. Docker / Compose

```bash
cp .env.example .env && ${EDITOR:-nano} .env
docker compose up -d --build
docker compose ps && curl -s localhost:8000/api/v1/health
```

Yang perlu diketahui dari `docker-compose.yml`:

- Dua layanan: `qdrant` (port 6333) dan `rag-api` (port 8000, `depends_on: service_healthy`).
- Volume: `rag_data` (`/data`, semua state) dan `model_cache` (`/root/.cache`, cache model HF/ONNX).
- `env_file: .env` + override `QDRANT_URL=http://qdrant:6333`.
- Healthcheck memakai `GET /api/v1/health` dengan masa tenggang 60 s (unduh model saat pertama jalan).
- Image memakai torch **CPU** (`--index-url .../wheel/cpu`) supaya ~2 GB lebih kecil.

Catatan kejujuran: perintah Docker di bagian ini **belum dijalankan di mesin penyusun dokumen**
(tidak ada Docker engine di Windows host maupun WSL-nya). Berkasnya sudah ada di repo dan
mengikuti struktur resmi; jalankan `docker compose config` dulu di server untuk memvalidasi
sebelum `up -d`.

---

## 9. Deploy bare-metal: systemd + nginx

### 9a. User & direktori

```bash
sudo useradd -r -m -d /opt/rag-service -s /usr/sbin/nologin rag
sudo mkdir -p /opt/rag-service/{data,logs}
sudo chown -R rag:rag /opt/rag-service
# taruh kode + .venv di /opt/rag-service, .env ber-permission 0600 milik rag
sudo chmod 600 /opt/rag-service/.env
```

### 9b. Unit systemd

Salin `deploy/rag-service.service` ke `/etc/systemd/system/`, sesuaikan `User`, `WorkingDirectory`,
dan `EnvironmentFile` bila path-nya berbeda, lalu:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rag-service
systemctl status rag-service --no-pager
journalctl -u rag-service -f          # log JSON satu baris per permintaan (ada request_id)
```

Catatan kejujuran: unit ini ditulis mengikuti pola systemd standar dan `ExecStart`-nya identik
dengan perintah yang sudah terbukti menjalankan layanan (`python -m app.main`), tetapi **belum
pernah di-`systemctl start`** karena mesin penyusun dokumen tidak punya systemd. Periksa dengan
`systemd-analyze verify deploy/rag-service.service` di server sebelum mengaktifkannya.

### 9c. nginx + TLS

```bash
sudo cp deploy/nginx-rag.conf /etc/nginx/sites-available/rag-service
sudo ln -s /etc/nginx/sites-available/rag-service /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d rag.example.com        # TLS; setelah ini paksa redirect ke https
```

Poin penting di contoh nginx:

- `client_max_body_size` **harus ≥ `MAX_UPLOAD_MB`**, kalau tidak unggah besar ditolak nginx (413) mendahului aplikasi.
- `proxy_read_timeout` harus **> `LLM_TIMEOUT`** (default 120 s) supaya kueri panjang tidak diputus.
- `proxy_buffering off` untuk `/api/` agar respons panjang/bertahap tidak ditahan buffer.
- `/ui/` boleh dilayani lewat proxy yang sama (berkas statis, tanpa build step).

Konfigurasi contoh ini juga belum dijalankan di server asli (tidak ada nginx di mesin penyusun
dokumen); selalu `sudo nginx -t` sebelum `reload` dan sesuaikan `server_name`.

---

## 10. Operasi harian

### Backup (semua state ada di berkas)

| Yang perlu disalin | Isi |
|---|---|
| `data/qdrant/` (`QDRANT_LOCAL_PATH`) | vektor + payload (mode embedded) |
| `data/sparse/` (`SPARSE_DIR`) | indeks BM25 per (organisasi, KB) |
| `data/storage/` (`STORAGE_DIR`) | berkas knowledge mentah |
| `data/registry.json` (`REGISTRY_PATH`) | daftar dokumen & status |
| `data/jobs.json` (`JOB_STORE_PATH`) | riwayat job pengindeksan |
| `data/tables.sqlite` (`TABLE_STORE_PATH`) | baris tabel terstruktur (untuk pertanyaan agregat) |
| `data/settings.json` (`SETTINGS_OVERRIDE_PATH`) | override setelan dari UI |

Contoh (hentikan layanan dulu agar Qdrant embedded konsisten):

```bash
sudo systemctl stop rag-service
tar czf /var/backups/rag-$(date +%F).tar.gz -C /opt/rag-service data
sudo systemctl start rag-service
```

Restore: hentikan layanan, timpa `data/`, jalankan ulang, cek `/ready`.
Bila memakai Qdrant server, snapshot dari sisi Qdrant (`/collections/{name}/snapshots`).

### Upgrade

```bash
cd /opt/rag-service
sudo systemctl stop rag-service
git pull --ff-only
uv pip install --python .venv/bin/python -r requirements.txt
sudo systemctl start rag-service && curl -s localhost:8000/api/v1/ready
```

Catatan: berkas `.env` tidak ikut berubah. Bila ada env baru di `.env.example`, tambahkan manual —
env yang tidak dikenal diabaikan (`extra="ignore"`), env lama tetap jalan.

### Mengubah setelan tanpa restart

Setelan LLM/Jev/katalog format bisa diubah dari konsol (`#/settings`, butuh izin `admin`);
perubahannya disimpan ke `SETTINGS_OVERRIDE_PATH` dan berlaku setelah **restart** layanan
(override dibaca saat startup). Env tetap menjadi nilai dasar.

### Rotasi kunci

1. Tambahkan kunci baru di berkas kunci + `API_KEYS_JSON`, restart.
2. Pindahkan klien ke kunci baru.
3. Hapus kunci lama, restart, verifikasi kunci lama → `401`.

---

## 11. Troubleshooting

| Gejala | Sebab paling sering | Tindakan |
|---|---|---|
| Startup gagal `AlreadyLocked` | dua proses memakai Qdrant embedded yang sama | Pastikan satu proses; hentikan proses lama atau pindah ke Qdrant server |
| `401 AUTH_INVALID` | header hilang/salah bentuk | `Authorization: Bearer <kunci>`; jangan kirim `organization_id` di body |
| `403 AUTH_FORBIDDEN` + `details.permission` | kunci tanpa izin (`admin`/`write`) | Pakai kunci ber-izin, atau tambah izin baru |
| Kueri paralel → 500 | Qdrant embedded tidak thread-safe | Pindah ke Qdrant server; jangan multi-worker |
| `vector count = -1` di `GET /knowledge` | Qdrant lokal gagal menghitung ulang | Tidak fatal (artinya "tidak diketahui"); indeks tetap bisa dipakai |
| LLM error/`502` pada `/query` | `LLM_BASE_URL`/`LLM_API_KEY` salah, atau id model tidak ada | Uji `POST /api/v1/settings/llm/models` (daftar model dari gateway) lalu perbarui `LLM_MODEL` |
| Jawaban selalu "tidak ditemukan" | `RELEVANCE_THRESHOLD` terlalu tinggi atau reranker mati | Turunkan ambang; aktifkan reranker; cek `STRICT_GROUNDING` |
| Unggah `413` | batas nginx lebih kecil dari `MAX_UPLOAD_MB` | Naikkan `client_max_body_size` |
| Unggah `415` | ekstensi/mime tidak diizinkan | Periksa `UPLOAD_EXTENSIONS`/`ALLOWED_MIME` dan katalog format di Pengaturan |
| `/ready` → `jev: error` | Jev tidak terjangkau/entitlement kunci | `JEV_MODE=heuristic` atau matikan `JEV_ENABLED`; layanan tetap jalan |
| RAM habis saat startup | memuat embedder + reranker torch | `EMBEDDING_PROVIDER=fastembed`/`hash`, `RERANKER_PROVIDER=none`, `MODEL_RESIDENCY=sequential` |
| Port bentrok | `APP_PORT` sudah dipakai | Ubah `APP_PORT` atau hentikan proses lama |

---

## 12. Checklist go-live

- [ ] `.env` ber-permission `0600`, tidak ada kunci di repo (`git status` bersih dari `.env*`).
- [ ] `QDRANT_URL` diisi (server) untuk pemakaian banyak pengguna; hanya satu proses bila embedded.
- [ ] `API_KEYS_JSON` berisi kunci per organisasi; kunci `admin` tidak dipakai klien umum.
- [ ] `CORS_ORIGINS` dibiarkan kosong kecuali UI dibuka dari origin lain.
- [ ] TLS aktif; `client_max_body_size` ≥ `MAX_UPLOAD_MB`; `proxy_read_timeout` > `LLM_TIMEOUT`.
- [ ] `GET /api/v1/ready` → semua dependensi `ok` (atau `disabled` dengan alasan yang dipahami).
- [ ] `scripts/deploy_smoke.py` lulus; backup terjadwal; `journalctl -u rag-service` bisa dibaca.
- [ ] Diketahui batasannya: rate limit in-process (satu replika), Qdrant embedded satu proses,
      OCR gambar belum aktif.
