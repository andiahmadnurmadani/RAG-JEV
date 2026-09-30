# RAG Service — Multi-Tenant Knowledge API with Jev Orchestration

Implementasi dari `PRD_RAG_Jev_Multi_Tenant.md`: satu layanan FastAPI yang meng-index
dokumen per organisasi, melakukan hybrid retrieval yang selalu dibatasi tenant, dan
menghasilkan jawaban yang 100% bersumber dari konteks (dengan sitasi) atau menolak
menjawab (no-answer). Jev hanya memutuskan **apa yang harus dilakukan**, bukan **data
siapa yang boleh dibaca**.

```
Client / KMS ──Bearer key / X-Tenant-Context──> RAG API
                                                  │
      ┌───────────────────────────────────────────┼──────────────────────────────┐
      ▼                                           ▼                              ▼
  Jev (route)                           Hybrid retrieval                 Qdrant (tenant filter)
  knowledge_query/search/               dense + BM25 → RRF               payload: organization_id,
  extract/summary                       → reranker → threshold           knowledge_base_id, ...
                                                  │
                                                  ▼
                                Context (fenced, cited) → LLM (endpoint OpenAI-compatible;
                                                          model dari setelan, bukan tetap)
                                                  │
                                                  ▼
                                          Answer + sources | No-answer
```

## Status: apa yang sudah berjalan (terverifikasi)

| Bagian | Bukti |
|---|---|
| 263 test (unit + integration) | `pytest tests/unit tests/integration -q` → **263 passed** |
| Indexing asinkron | `POST /knowledge/index` → 202, worker thread, status via `GET /knowledge/{id}` |
| Isolasi tenant | 12 test: Org B tidak melihat/menghapus/membaca status dokumen Org A |
| Hybrid retrieval + fusion | RRF (dense + BM25), reranker, threshold, no-answer teruji |
| Evaluasi retrival | `bash scripts/eval_matrix.sh` → korpus 10 dok / 11 chunk / 15 pertanyaan + 3 jebakan, 4 konfigurasi (lihat `docs/evaluation.md`) |
| Ablation reranker | `bash scripts/eval_with_reranker.sh` → `bge-reranker-v2-m3` nyata: Precision@5 0.22 → 0.867, refusal jebakan 0.00 → **1.00**, Recall@5 tetap 1.00 |
| Observability | `/api/v1/health`, `/api/v1/ready` (per-dependency), `/api/v1/metrics` |
| Docker | `docker-compose.yml` (RAG API + Qdrant) + `Dockerfile` — **ditulis & divalidasi YAML-nya, belum pernah dijalankan** (Docker Hub tak terjangkau dari WSL di mesin ini) |
| Uji live end-to-end | `python scripts/live_verify.py` → **19/19 check lulus** (PDF 16 halaman + LLM nyata + isolasi tenant) — lihat `docs/verification.md` |
| Konsol chat | `GET /ui/` (Material 3, tanpa build step/CDN): upload knowledge terpisah, daftar dokumen, chat bersitasi, setelan Jev + retrieval |
| **Analitik tabel** (xlsx/csv/ods) | Baris disimpan terstruktur (SQLite) lalu pertanyaan agregat ("paling laku", "total") **dihitung kode**, bukan ditebak LLM. Bukti live: xlsx 40 baris → "Ayam Geprek 184" dan "Rp 14.830.000" (sama dengan hitungan `openpyxl`); kolom tak ada → ditolak jujur. Lihat `docs/verification.md` bagian 7 |

## Dokumentasi

Satu sumber di `docs/`, disajikan dalam tiga bentuk:

| Bentuk | Alamat | Cara mengaktifkan |
|---|---|---|
| **Situs dokumentasi** (MkDocs Material) | `/guide/` | `bash scripts/build_docs.sh` → hasil di `site/` |
| Swagger UI / ReDoc (bawaan FastAPI) | `/docs` · `/redoc` | selalu aktif |
| Konsol chat | `/ui/` | selalu aktif |

Referensi API **dihitung dari kode** (`docs/openapi.json` → `docs/api-reference.md`), jadi tidak bisa
menyimpang dari implementasi. Perangkat dokumentasi ini sengaja generik supaya bisa dipakai ulang oleh
aplikasi lain: `mkdocs.yml`, `requirements-docs.txt`, `scripts/{build_docs.sh,export_openapi.py,gen_api_reference.py}` — caranya di `docs/reuse.md`.

Bangun situs + periksa penyajiannya (port terpisah, data terpisah):

```bash
bash scripts/build_docs.sh        # -> site/
bash scripts/check_docs_serve.sh  # uji /guide, /docs, /redoc, /openapi.json, /ui
```

## Menjalankan

> **Panduan operator lengkap** (pasang di server, systemd, nginx + TLS, backup, upgrade, rotasi kunci,
> troubleshooting, checklist go-live): **`docs/deployment.md`**. Ringkasnya ada di bawah ini.

### 1. Tanpa model (paling cepat — untuk uji API)

```bash
cd /e/rag-service
cp .env.example .env            # isi API_KEYS_JSON, KMS_SHARED_SECRET, dll.
EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none LLM_PROVIDER=mock \
  .venv/Scripts/python.exe -m app.main
```

### 2. Profil produksi (PRD)

```bash
# embedding + reranker ONNX (int8, hemat RAM)
EMBEDDING_PROVIDER=fastembed RERANKER_PROVIDER=fastembed \
LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://<vllm>:8000/v1 LLM_MODEL=Qwen/Qwen3-4B \
  python -m app.main
```

Profil PRD-exact (BGE-M3 + BGE reranker v2 m3 via torch, butuh ~4-6 GB RAM):

```bash
EMBEDDING_PROVIDER=sentence_transformers EMBEDDING_MODEL=BAAI/bge-m3 \
RERANKER_PROVIDER=sentence_transformers RERANKER_MODEL=BAAI/bge-reranker-v2-m3 \
  python -m app.main
```

### 3. Docker

```bash
cp .env.example .env            # WAJIB: compose membaca env_file .env
docker compose up -d --build
curl -s localhost:8000/api/v1/health
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/ui/     # konsol uji
```

Status kejujuran: `docker-compose.yml` + `Dockerfile` sudah ditulis dan **divalidasi struktur
YAML-nya**, tetapi **belum pernah dijalankan di mesin ini** karena WSL tidak bisa menjangkau
Docker Hub:

```
failed to resolve reference "docker.io/library/python:3.11-slim":
TLS handshake timeout   (registry-1.docker.io tidak terjangkau dari WSL)
```

Jangan anggap jalur container sudah terbukti. Yang sudah terbukti adalah jalur non-container
(pytest, uji live, evaluasi, konsol `/ui/`).

## Pemakaian API (ringkas)

```bash
KEY=...            # API key tenant (lihat API_KEYS_JSON)

# index (asinkron, 202)
curl -s -XPOST localhost:8000/api/v1/knowledge/index -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{
    "document_id":"doc_123","knowledge_base_id":"kb_hr","document_name":"SOP Cuti.pdf",
    "file_url":"https://storage.example/doc_123.pdf"}'

# status
curl -s localhost:8000/api/v1/knowledge/doc_123 -H "Authorization: Bearer $KEY"

# tanya (grounded + sitasi)
curl -s -XPOST localhost:8000/api/v1/query -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -d '{
    "query":"Bagaimana prosedur cuti tahunan?","knowledge_base_id":"kb_hr",
    "options":{"top_k":5,"strict_grounding":true,"include_sources":true}}'

# pertanyaan agregat atas spreadsheet (dihitung dari tabel, angka dari data)
curl -s -XPOST localhost:8000/api/v1/query -H "Authorization: Bearer ***" \
  -H 'Content-Type: application/json' -d '{
    "query":"produk apa yang paling laku?","knowledge_base_id":"kb_penjualan"}'
# -> { "data": { "answer": "...Ayam Geprek 184...", "computed": {"operation":"top_n",
#      "metric":"Jumlah","group_by":"Produk","rows_matched":40,"rows_total":40,
#      "result":[{"Produk":"Ayam Geprek","Jumlah":184,...}], "explanation":"..." } } }

# daftar tabel terstruktur di scope pemanggil
curl -s localhost:8000/api/v1/tables -H "Authorization: Bearer ***"

# retrieval saja / ekstraksi terstruktur
curl -s -XPOST localhost:8000/api/v1/search  -H "Authorization: Bearer $KEY" -d '{"query":"cuti","knowledge_base_id":"kb_hr","top_k":5}'
curl -s -XPOST localhost:8000/api/v1/extract -H "Authorization: Bearer $KEY" -d '{"query":"data penjualan bulanan","knowledge_base_id":"kb_hr","output_schema":{"type":"array"}}'
```

## Jev: dua transport, satu kontrak

Jev adalah *lapisan keputusan* (memilih capability, bukan menulis jawaban) dan tidak pernah
menentukan tenant. Ada dua cara memanggilnya, dipilih lewat `JEV_PROVIDER`:

| `JEV_PROVIDER` | Endpoint | Protokol |
|---|---|---|
| `mcp` (default) | `JEV_MCP_URL`, mis. `https://www.jevai.org/api/mcp` | JSON-RPC MCP, `tools/call` (`jev_route_task`, ...) |
| `systemone` | `JEV_SYSTEMONE_URL` + `JEV_MODEL` | `POST {state, model, questions}` - Jev native, **bukan** chat-completions |

Contoh System One di instance 9Router lokal:

```bash
JEV_ENABLED=true JEV_MODE=live JEV_PROVIDER=systemone \
JEV_SYSTEMONE_URL=http://localhost:20128/v1/systemone \
JEV_MODEL=oc/jev-1.13-free JEV_API_KEY="$HERMES_CUSTOM_LOCALHOST_20128_API_KEY" \
  .venv/Scripts/python.exe -m app.main
```

Bentuk wire-nya (direkam dari endpoint, bukan dari prosa dokumentasi):

```json
POST {"state": {"request": {"query": "berapa kuota cuti"}},
      "model": "oc/jev-1.13-free",
      "questions": {"decision": {"type": "choice",
                                 "instructions": "Which capability should serve this request?",
                                 "criteria": {"knowledge_query": "...", "knowledge_search": "..."}}}}
200  {"answers": {"decision": {"type": "choice", "choice": "knowledge_query",
                              "confidence": 0.93,
                              "probabilities": {"knowledge_query": 0.95, "knowledge_search": 0.03}}},
      "usage": {"input_tokens": 293, "output_tokens": 22}, "cost": "0"}
```

Tiga jebakan yang terukur pada lane `oc/jev-1.13-free` (free tier 9Router):

1. **`score` tidak didukung.** Pertanyaan `choice` atau `noul` dijawab 200 dalam ~0,8-1,2 s, tetapi
   satu pertanyaan `score` yang ikut dalam permintaan membuat **seluruh** permintaan dijawab
   `422 Endpoint is unavailable`. Klien karena itu menolak tipe selain `choice`/`noul` sebelum
   mengirim, dan menanyakan satu pertanyaan per panggilan - jawaban `choice` sudah membawa
   `confidence` sendiri.
2. **Katalog model tidak sama dengan ketersediaan.** `nov/qwen/qwen3-4b-fp8` dan
   `qwen3-8b-fp8/30b/32b` terdaftar di `/v1/models` tetapi upstream menjawab `404 MODEL_NOT_FOUND`;
   `cl/typesafe/jev-router` ada di katalog tetapi ditolak `Provider 'cline' does not support System One`.
3. **`/v1/systemone` bukan `/v1/chat/completions`.** Menaruhnya sebagai base URL provider chat
   gagal; ia butuh `state` + `questions`.

Kegagalan Jev tidak pernah memblokir permintaan: `/query` tetap 200 dengan `source="fallback"`
(heuristik), dan `/ready` melaporkan `jev: disabled|error` apa adanya. Probe liveness di `/ready`
di-cache `JEV_HEALTH_CACHE_SECONDS` (default 300) karena setiap probe memakai token.

## Model LLM

Target PRD (8.1) adalah **Qwen3 4B** yang di-self-host, dan itu memang default di kode:
`app/core/config.py` -> `llm_model = "Qwen/Qwen3-4B"`, sama seperti `.env.example`. Ganti model
tidak butuh perubahan kode - cukup `LLM_PROVIDER` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`,
karena generator hanya berbicara protokol chat-completions.

Yang benar-benar tersedia saat pengujian (diukur langsung, bukan asumsi):

| Model | Hasil panggilan |
|---|---|
| `Qwen/Qwen3-4B` lokal (vLLM/llama.cpp) | **belum ada di mesin ini**: `ollama`, `llama-server`, `vllm` tidak terpasang. Mesin punya RTX 3050 6 GB, cukup untuk Qwen3-4B kuantisasi 4-bit (~2,5 GB) |
| `nov/qwen/qwen3-4b-fp8` (gateway) | terdaftar di `/v1/models`, tetapi upstream menjawab `404 MODEL_NOT_FOUND` - katalog tidak sama dengan ketersediaan |
| `nov/qwen/qwen3-8b-fp8`, `qwen3-30b-a3b-fp8`, `qwen3-32b-fp8` | `404 MODEL_NOT_FOUND` |
| `ollama/qwen3.5` | `410` - model sudah di-retire 2026-09-25 |
| `cmc/Qwen/Qwen3.6-Plus` (gateway) | berhasil; jawaban grounded, `generation_ms` ~25.000 pada 2.665 token konteks |
| `cmc/deepseek/deepseek-v4.1-flash` (gateway) | berhasil; `generation_ms` ~2.400 |

Jadi server contoh yang dipakai untuk menguji UI memakai model dari gateway karena **satu-satunya
LLM yang bisa dipanggil dari mesin ini** adalah gateway tersebut, bukan karena desainnya berubah.
Untuk setup sesuai PRD:

```bash
# 1) jalankan Qwen3-4B lokal (pilih salah satu)
llama-server -hf Qwen/Qwen3-4B-GGUF:Q4_K_M --port 8000      # llama.cpp
ollama run qwen3:4b                                          # Ollama

# 2) arahkan service ke sana
LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://127.0.0.1:8000/v1 \
LLM_MODEL=qwen3:4b LLM_API_KEY=local \
  .venv/Scripts/python.exe -m app.main
```

Catatan penting: mengganti LLM **tidak** memperbaiki kualitas jawaban di server contoh ini.
Bottleneck-nya retrieval (`EMBEDDING_PROVIDER=hash` + `RERANKER_PROVIDER=none`), bukan generator -
terbukti dari beberapa pertanyaan yang dijawab `grounded=false` sebelum LLM dipanggil sama sekali
(`generation_ms: 0`). Untuk kualitas nyata pakai `EMBEDDING_PROVIDER=bge-m3` +
`RERANKER_PROVIDER=sentence_transformers` (ablasi di `docs/evaluation.md`: Precision@5 0,22 -> 0,867).

## Konsol chat (UI)

Buka `http://<host>:<port>/ui/` (root `/` dialihkan ke sana). Konsol ini statis, tanpa build
step dan tanpa CDN, dilayani service yang sama sehingga tidak ada masalah CORS.

Ada **dua layar** dan pembagiannya sengaja tegas: halaman utama hanya untuk bekerja, semua
konfigurasi tinggal di layar Pengaturan.

### Layar 1 — chat (halaman utama)

| Bagian | Isi |
|---|---|
| Atas | nama konsol, status koneksi, tombol roda gigi ke Pengaturan |
| Kiri - **Dokumen** | tarik-lepas atau pilih berkas (format mengikuti layar Pengaturan - lihat *Format berkas knowledge*), plus "Tempel teks" dan "Dari URL" - terpisah dari kotak prompt. Status job dipantau sampai `completed`/`failed`. Daftar dokumen menampilkan nama manusia, id, status, jumlah chunk/token, waktu, dengan centang per dokumen (centang = pertanyaan dibatasi ke dokumen itu) dan tombol hapus. |
| Kanan - **Chat** | riwayat percakapan; jawaban datang dengan sitasi `[1]` (dokumen, halaman, skor) dan chip meta: keputusan Jev + sumbernya (`jev`/`fallback`/`request_hint`), model, reranker, `retrieval_ms`, `generation_ms`, `context_tokens`. Jawaban yang ditolak (strict grounding) tampil beda, bukan berpura-pura menjawab. |
| Bawah composer | mode **Jawab** / **Cari saja** (retrieval tanpa memanggil model), indikator batas dokumen, `X-Request-Id` terakhir |

Enter mengirim, Shift+Enter baris baru. Tidak ada field konfigurasi di layar ini.

### Layar 2 — Pengaturan (dialog, `#/settings`)

Pengaturan kini **popup** (dialog di atas halaman chat, tombol roda gigi atau alamat `#/settings`),
dengan navigasi kiri; hanya satu panel tampil sekaligus supaya tidak perlu menggulir panjang.

| Panel | Isi | Sifat |
|---|---|---|
| **Koneksi** | base URL (dari alamat halaman), API key layanan, knowledge base, tombol Uji koneksi | disimpan di `localStorage` browser |
| **Kunci API** | daftar semua kunci yang berlaku (termasuk yang dari `API_KEYS_JSON`, ditandai *dari env*), plus pembuatan kunci baru: label, izin (`read` selalu ikut, `write`, `admin`), masa berlaku opsional, dan tenant opsional (hanya untuk kunci berizin `*`). Kunci baru tampil **sekali** dengan tombol Salin; baris kunci punya tombol **Cabut** | `GET/POST/DELETE /api/v1/settings/api-keys`, **wajib izin `admin`** |
| **Model AI** | provider, base URL, API key, model + tombol **Muat daftar model** (daftar diambil langsung dari `GET {base_url}/models`, 400+ entri pada gateway uji) dengan saringan dan pemilihan klik | `PUT /api/v1/settings`, **wajib izin `admin`** |
| **Jev** | saklar aktif, transport (`systemone` \| `mcp`), endpoint, model, API key, tombol Uji Jev (mengirim satu pertanyaan `noul` sungguhan dan menampilkan latensinya) | idem |
| **Format berkas** | daftar centang jenis berkas per grup (Dokumen, Presentasi, Spreadsheet, Teks, Gambar) yang boleh jadi knowledge, plus batas ukuran berkas (MB). Format yang belum didukung mesin ini tampil nonaktif beserta alasannya (mis. `program tesseract belum terpasang`). Tombol *Pilih semua yang tersedia* dan *Simpan format* | `PUT /api/v1/settings`, **wajib izin `admin`** |
| **Retrieval** | `top_k`, `threshold`, keputusan Jev (otomatis/`knowledge_query`/`knowledge_search`/`knowledge_summary`/`knowledge_extract`), `strict_grounding`, `hybrid`, `reranker` | pilihan per permintaan, hanya di browser |

Aturan yang berlaku di layar ini:

- Konfigurasi LLM/Jev bersifat **global** (mengubah generasi/routing semua tenant) sehingga
  hanya kunci berizin `admin` yang bisa membaca/mengubah. Kunci lain tetap bisa chat, dan
  layar Pengaturan menjelaskannya, bukan sekadar gagal diam-diam.
- Kunci baru dari panel **Kunci API** disimpan sebagai `sha256` (plus awalan untuk ditampilkan,
  berkas mode `0600`), konteks tenant-nya mewarisi pembuatnya, dan pencabutannya berlaku
  **segera** — tanpa deploy ulang. Yang dicabut tetap tampil sebagai `revoked` supaya bisa diaudit.
- Kunci yang sedang dipakai tidak bisa mencabut dirinya sendiri; buat kunci pengganti dulu.
- API key model/Jev **write-only**: yang pernah dikirim tidak pernah dikembalikan lagi oleh
  server, hanya `api_key_set` + petunjuk tersamar (`sk-d...15e8`). Kosongkan field = pakai yang
  tersimpan; isi = ganti.
- Perubahan berlaku **tanpa restart**: berkas override dibaca per panggilan, klien LLM/Jev
  dibangun ulang saat disimpan. Yang diuji di layar (Uji koneksi / Uji Jev / Muat daftar model)
  memakai nilai yang sedang tampil, jadi URL + key + model bisa dicoba dulu sebelum disimpan.
- URL probe divalidasi lebih dulu (`http`/`https`, tanpa kredensial di URL, tanpa alamat
  link-local/metadata, tanpa redirect) karena berasal dari operator.

### Format berkas knowledge

Jenis berkas yang boleh menjadi knowledge adalah **kebijakan layanan** (berlaku semua tenant),
jadi kontrolnya ada di Pengaturan bersama LLM/Jev dan ikut izin `admin`. Satu katalog menjadi
sumber kebenaran tunggal (`app/parsing/formats.py`): dipakai validasi unggahan, laporan
`GET /ready`, layar Pengaturan, dan pemilihan parser - bukan konstanta yang tersebar.

| Grup | Format | Cara teksnya dibaca |
|---|---|---|
| Dokumen | `.pdf` `.docx` `.docm` `.odt` `.rtf` `.epub` `.doc` | PDF lewat PyMuPDF; DOCX/ODT/RTF/EPUB dibaca langsung dari struktur arsipnya; **`.doc` Word 97-2003 dibaca dari format binernya sendiri** (`app/parsing/doc_binary.py`, tanpa `textract`/LibreOffice) |
| Spreadsheet | `.xlsx` `.xlsm` `.xls` `.csv` `.tsv` `.ods` | setiap sheet jadi satu bagian, `sheet` & nomor baris disertakan, sel kosong tidak menyisakan pemisah; **`.xls` Excel 97-2003 dibaca dari rekaman BIFF8** (`app/parsing/xls_binary.py`), sel bertipe tanggal ditulis sebagai tanggal ISO |
| Presentasi | `.pptx` `.pptm` `.odp` `.ppt` | teks per slide + **catatan pembicara** + master/tata letak, satu slide satu bagian; **`.ppt` PowerPoint 97-2003 dibaca dari rekaman binernya** (`app/parsing/ppt_binary.py`) |
| Teks | `.txt` `.md` `.markdown` `.html` `.htm` `.xhtml` `.xml` `.json` `.jsonl` `.ndjson` `.yaml` `.yml` `.rst` `.tex` `.log` `.sql` `.ini` `.conf` `.cfg` `.env` | teks polos; HTML/XML dibaca tanpa tag (judul, tabel jadi `a \| b`); berkas non-UTF-8 (mis. latin-1) dideteksi otomatis, jadi "café" tidak rusak |
| Gambar | `.png` `.jpg` `.jpeg` `.tif` `.tiff` `.bmp` `.webp`* | OCR (butuh `tesseract` + `pytesseract`) |

\* hanya muncul aktif kalau pustaka/programnya ada; kalau tidak, kotaknya nonaktif dan alasannya
ditulis di layar. Saat ini hanya OCR gambar yang nonaktif di mesin ini (`tesseract` belum
terpasang); seluruh format Office - modern maupun lama - aktif tanpa pustaka pihak ketiga.
Isi daftar aktif bisa dipersempit lagi dari layar Pengaturan; minimal satu format
yang benar-benar bisa dibaca harus tersisa, kalau tidak `PUT /settings` menolak dengan `422`
(**sebelum** apa pun ditulis).

Efek setelan ini langsung terlihat di halaman utama: ringkasan format di area tarik-lepas,
`accept` pada pemilih berkas, dan penolakan di browser untuk berkas berformat yang tidak
diizinkan - berkasnya tidak pernah dikirim ke server.

Bukti bahwa *semua teksnya bisa dibaca* ada di `docs/verification.md` bagian 6 dan bisa diulang
sendiri dengan `scripts/make_format_samples.py` + `scripts/verify_format_samples_live.py`.

### Menjalankan konsol dengan konfigurasi nyata

```bash
bash scripts/run_live.sh          # port 8099
PORT=8100 bash scripts/run_live.sh
```

Skrip itu mengisi seluruh env (penyimpanan lokal `data/live/`, generator, Jev, kunci uji) dan
mengambil kunci gateway dari `.env` Hermes; nilai kunci tidak pernah ditulis di skrip. Tanpa
kunci tersebut layanan tetap jalan (`LLM_PROVIDER=mock`, Jev mati) supaya tampilan masih bisa
diperiksa.

### Kunci uji

| Kunci | Tenant | Izin |
|---|---|---|
| `live-key-org-a` | `org_a` | `read`, `write`, **`admin`** (boleh menyimpan setelan model/Jev) |
| `live-key-org-b` | `org_b` | `read`, `write` (tenant terpisah, untuk membuktikan isolasi) |

Kunci didefinisikan oleh env `API_KEYS_JSON` saat server dijalankan; ganti isinya untuk
menambah/mengganti kunci. Kunci yang tidak dikenal dijawab `401 AUTH_INVALID` sebelum pekerjaan apa pun.

Selain itu, kunci bisa dibuat **tanpa deploy ulang** dari panel **Kunci API** di layar Pengaturan
(kunci `rag_...`, disimpan sebagai `sha256` di `API_KEYS_PATH`, default `data/api_keys.json`, mode
0600). Nilai kuncinya hanya muncul sekali saat dibuat; pencabutannya berlaku segera. Kunci env tetap
dikelola lewat env - panel itu menandainya *dari env* dan menolak mencabutnya dari UI.

Catatan jujur: konsol mengirim API key layanan dari browser, jadi pakai untuk jaringan/kredensial uji,
bukan dibuka ke publik. `CORS_ORIGINS` sengaja kosong (hanya same-origin). Berkas override
`data/live/settings.json` ditulis mode 0600, tetapi pada Windows/macOS mode POSIX itu tidak
benar-benar mengikat - lindungi berkasnya lewat izin folder.

Detail kontrak lengkap: `docs/api.md`.

## Verifikasi end-to-end (live)

```bash
# 1) jalankan layanan (contoh: profil cepat, LLM nyata lewat gateway OpenAI-compatible)
EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none LLM_PROVIDER=openai_compatible \
LLM_BASE_URL=http://<gateway>/v1 LLM_MODEL=<model> .venv/Scripts/python.exe -m app.main

# 2) jalankan seluruh pemeriksaan terhadap layanan yang hidup
.venv/Scripts/python.exe scripts/live_verify.py
```

`scripts/live_verify.py` meng-index PDF asli (`file://`), menambah dokumen kedua di
knowledge base yang sama, lalu memeriksa: liveness/readiness, `/search`, `/query` dengan
LLM nyata, `/extract`, penolakan lintas-tenant, penolakan field tenant di body, dan
penghapusan dokumen. Hasil lengkap: `docs/verification.md`.

## Konfigurasi penting

Semua perilaku model/provider dipilih lewat env var (`.env.example` memuat semuanya):

| Variabel | Arti |
|---|---|
| `EMBEDDING_PROVIDER` | `sentence_transformers` (BGE-M3, PRD) / `fastembed` (ONNX int8) / `http` / `hash` |
| `RERANKER_PROVIDER` | `sentence_transformers` (bge-reranker-v2-m3, PRD) / `fastembed` (jina-reranker-v2 multilingual) / `none` |
| `LLM_PROVIDER` | `openai_compatible` / `ollama` / `mock` (tanpa model) |
| `LLM_BASE_URL`, `LLM_MODEL` | endpoint chat-completions + model (default PRD `Qwen/Qwen3-4B`, lihat "Model LLM" di bawah) |
| `JEV_ENABLED`, `JEV_MCP_URL`, `JEV_API_KEY` | orkestrasi Jev; jika gagal → fallback heuristik, bukan error |
| `SETTINGS_OVERRIDE_PATH` | berkas override setelan LLM/Jev/**format berkas** dari layar Pengaturan (default `data/settings.json`) |
| `UPLOAD_EXTENSIONS`, `MAX_UPLOAD_MB` | titik awal kebijakan format berkas; kosong = seluruh katalog yang didukung mesin ini (dipersempit kapan saja lewat Pengaturan) |
| `API_KEYS_JSON`, `KMS_SHARED_SECRET` | sumber tunggal `organization_id` yang tepercaya |
| `RELEVANCE_THRESHOLD`, `STRICT_GROUNDING` | gerbang no-answer |

## Struktur

```
app/
  api/      routes (query, search, extract, knowledge, system) + middleware (auth, tenant, ratelimit)
  core/     config, errors (PRD 29), security, tenant, logging, metrics
  parsing/  formats.py (katalog format) + parser.py (12 parser + router berkas)
  rag/      chunker, embedder, sparse(BM25), retriever(RRF), reranker, context, generator, pipeline
  qdrant/   client, collections, repository (tenant filter wajib)
  jev/      policies (capability), router, tools (MCP JSON-RPC)
  workers/  indexing (queue + pipeline + job store)
tests/      unit/ integration/ evaluation/
docs/       api, architecture, rag-pipeline, tenant-isolation, evaluation, verification, deployment
deploy/     rag-service.service (systemd) + nginx-rag.conf (reverse proxy + TLS)
mkdocs.yml  + requirements-docs.txt  situs dokumentasi (build: bash scripts/build_docs.sh -> site/ disajikan di /guide)
```

## Batasan yang diketahui (jujur)

- **Jev live belum bisa dipakai untuk routing**: akun jevai.org menolak `tools/call`
  (`The request credentials or model access were rejected.`), jadi router otomatis
  memakai fallback heuristik dan melaporkan `source="heuristic"` + `jev_mode` di `/ready`.
  Kode live-nya sudah ada dan teruji lewat stub — tinggal kredensial yang benar.
- Reranker profil `fastembed` memakai `jinaai/jina-reranker-v2-base-multilingual`
  karena `BAAI/bge-reranker-v2-m3` tidak punya port ONNX. Profil PRD-exact tetap
  tersedia lewat `sentence_transformers`.
- Rate limiter in-process: satu replika. Untuk multi-replika pindahkan ke Redis.
- Unduhan model ONNX `fastembed` (e5-large) macet di jaringan ini (5/6 file, lalu hang),
  sehingga laporan evaluasi embedding-nyata memakai jalur `sentence_transformers`
  (`scripts/eval_real_models.sh`) — lihat `docs/evaluation.md`.
- **Format Office lama sudah didukung** (`.doc`, `.ppt`, `.xls`) tanpa `textract`/`xlrd`/LibreOffice:
  kontainer OLE2 dibaca sendiri (`app/parsing/ole.py`) lalu isinya diurai per format. Yang masih
  nonaktif hanya OCR gambar (`tesseract` + `pytesseract`).
- Teks diekstrak, bukan ditata ulang: tabel dibaca berurutan sebagai `sel | sel`, dan isi grafik tidak
  dibaca. Berkas asli maupun teks hasil parse tidak disimpan di server.
- **Tidak ada bagian yang hilang diam-diam**: seluruh bagian berkas ikut dibaca - header/footer,
  catatan kaki/akhir, komentar, properti dokumen, textbox, catatan pembicara, master presentasi,
  header/footer cetak spreadsheet, dan komentar sel. Bila ada yang tetap tidak bisa dibaca, alasannya
  ditulis (mis. `notes` pada tabel), bukan berkasnya diterima dengan isi yang berkurang.
- `organization_id` di body `/knowledge/index` diterima untuk kompatibilitas KMS,
  tetapi **wajib sama** dengan trusted context; kalau berbeda → 403.
