# API Contract

> Konsol uji manual tersedia di `GET /ui/` (root `/` dialihkan ke sana). Konsol itu memanggil
> endpoint di dokumen ini apa adanya, jadi perilaku yang terlihat di layar = kontrak di bawah.

Base URL: `/api/v1`. Semua respons memakai envelope (PRD 22–29):

```json
// sukses
{"success": true, "data": { ... }}
// gagal
{"success": false, "error": {"code": "VALIDATION_ERROR", "message": "...", "request_id": "req_ab12..."}}
```

Setiap respons memuat header `X-Request-Id` (juga muncul di log JSON dan di error envelope).

## Autentikasi

```http
Authorization: Bearer <API_KEY>          # dipetakan ke API_KEYS_JSON
X-Tenant-Context: <KMS HS256 token>      # alternatif; atau Bearer token bila REQUIRE_TENANT_CONTEXT_TOKEN=true
```

Trusted context yang dihasilkan: `user_id`, `organization_id`, `application_id`, `permissions`.

- Endpoint baca (`/query`, `/search`, `/extract`, `GET /knowledge/{id}`) butuh permission `read`.
- Endpoint tulis (`POST/PUT/DELETE /knowledge/...`) butuh `write`.
- `organization_id` **tidak boleh** datang dari body/prompt pada endpoint query. Body yang
  memuat `organization_id` / `org_id` / `tenant_id` ditolak **422** sebelum kerja apa pun
  (model request memakai `extra="forbid"` + guard eksplisit).

Kode error (PRD 29): `AUTH_INVALID` 401, `AUTH_FORBIDDEN` 403, `TENANT_CONTEXT_MISSING` 401,
`KNOWLEDGE_NOT_FOUND` 404, `DOCUMENT_NOT_FOUND` 404, `INDEXING_FAILED` 500, `EMBEDDING_FAILED` 500,
`RETRIEVAL_FAILED` 500, `RERANK_FAILED` 500, `LLM_FAILED` 502, `JEV_FAILED` 502,
`VALIDATION_ERROR` 422, `PAYLOAD_TOO_LARGE` 413, `UNSUPPORTED_MEDIA_TYPE` 415,
`RATE_LIMITED` 429, `INTERNAL_ERROR` 500.

---

## POST /knowledge/index - 202 Accepted

Indexing **asinkron** (PRD 33): respons langsung, pekerjaan dijalankan worker.

`document_name` adalah **label manusia** untuk dokumen ini. Ekstensinya opsional -
`SOP Cuti 2026` sah untuk teks yang ditempel - tetapi label yang mengaku tipe yang tidak kita
terima (`payload.exe`, `script.js`) ditolak **415 `UNSUPPORTED_MEDIA_TYPE` sebelum job dibuat**
(bukan 202 lalu gagal belakangan). Untuk sumber `file_url`, nama asli baru diketahui setelah
diunduh, jadi pemeriksaan yang sama juga dijalankan worker dan job-nya berakhir `failed`.

```json
{
  "document_id": "doc_123",
  "knowledge_base_id": "kb_hr",
  "organization_id": "org_001",        // opsional; harus == trusted context
  "document_name": "SOP Cuti.pdf",
  "file_url": "https://storage.example/doc_123.pdf",
  "metadata": {"kategori": "hr"},
  "language": "id",
  "replace": true
}
```

Selain `file_url`, tersedia `content_base64` (byte langsung) dan `text` (untuk pipeline
yang sudah mengekstrak teks). Persis satu dari ketiganya wajib ada.

Respons:

```json
{"success": true, "data": {"document_id": "doc_123", "status": "queued", "job_id": "job_ab12cd34ef56"}}
```

Header tambahan: `X-Job-Id`. Status: `queued` → `processing` → `completed` | `failed`.

Validasi file (PRD 34) dijalankan **sebelum** bytes apa pun diparsing: MIME/ekstensi
(`.pdf .txt .md .html .docx .json`), batas `MAX_UPLOAD_MB`, sanitasi nama file, tidak
ada eksekusi file.

## PUT /knowledge/{document_id} — 202 Accepted

Sama seperti index dengan `replace=true`: vektor/lexical lama dihapus, lalu ditulis ulang.
`document_id` di path dan body harus sama.

## DELETE /knowledge/{document_id}

```json
{"success": true, "data": {"document_id": "doc_123", "status": "deleted", "deleted_chunks": 17}}
```

Menghapus titik Qdrant **dan** entri lexikal tenant tersebut. Dokumen tenant lain → 404
`DOCUMENT_NOT_FOUND` (tanpa membocorkan keberadaannya).

## GET /knowledge/{document_id}

```json
{
  "success": true,
  "data": {
    "document_id": "doc_123", "status": "completed", "stage": "completed",
    "knowledge_base_id": "kb_hr", "chunks": 17, "tokens": 8412, "pages": 12,
    "error": null, "created_at": "...", "updated_at": "...", "duration_ms": 5211.4,
    "vectors_in_store": 17
  }
}
```

`vectors_in_store` dihitung langsung dari Qdrant, jadi status tidak bisa "berbohong".

## POST /query

```json
{
  "query": "Bagaimana prosedur cuti tahunan?",
  "knowledge_base_id": "kb_hr",
  "options": {"top_k": 5, "strict_grounding": true, "include_sources": true,
              "use_hybrid": true, "use_reranker": true, "threshold": 0.35, "route": null,
              "table_analytics": true}
}
```

`knowledge_base_id` wajib: retrieval selalu dibatasi satu knowledge base di dalam tenant.

```json
{
  "success": true,
  "data": {
    "answer": "Berdasarkan SOP Cuti [1], pengajuan dilakukan melalui sistem HR ...",
    "grounded": true,
    "sources": [{"document_id": "doc_123", "document_name": "SOP Cuti.pdf", "chunk_id": "chunk_0004",
                 "page": 12, "section": "SOP Cuti Tahunan / Pengajuan", "source_url": "...", "score": 0.94}],
    "usage": {"retrieved_chunks": 5, "reranked_chunks": 5, "reranker": "sentence_transformers:BAAI/bge-reranker-v2-m3",
              "context_tokens": 486, "input_tokens": 812, "output_tokens": 143,
              "retrieval_ms": 18.2, "rerank_ms": 240.7, "generation_ms": 1420.5},
    "route": {"capability": "knowledge_query", "source": "jev|heuristic|request_hint",
              "confidence": 0.82, "reason": "...", "stripped_tenant_fields": []},
    "model": "Qwen/Qwen3-4B",
    "no_answer_reason": null,
    "computed": null,
    "table_note": null
  }
}
```

### Pertanyaan atas data spreadsheet (xlsx / csv / ods / tsv)

Bila knowledge base memuat berkas tabel, pertanyaan agregat **tidak dijawab LLM dari ingatan** —
angkanya dihitung kode dari seluruh baris tabel, dan LLM hanya memilih operasi + nama kolom lalu
menarasikan hasil hitung. Tambahkan `"table_analytics": true` (default `true`, matikan per-request
bila ingin retrieval teks murni).

```json
{"query": "produk apa yang paling laku?", "knowledge_base_id": "kb_penjualan",
 "options": {"table_analytics": true}}
```

```json
{
  "success": true,
  "data": {
    "answer": "Produk paling laku adalah **Ayam Geprek** dengan total 184 unit, disusul ...",
    "grounded": true,
    "computed": {
      "operation": "top_n",
      "metric": "Jumlah",
      "group_by": "Produk",
      "rows_matched": 40,
      "rows_total": 40,
      "rows_skipped": 0,
      "result": [{"Produk": "Ayam Geprek", "Jumlah": 184, "teks": "184"},
                   {"Produk": "Air Mineral 600ml", "Jumlah": 141, "teks": "141"}],
      "explanation": "sum(Jumlah) per Produk atas 40 baris tabel Penjualan, diurutkan menurun."
    },
    "table_note": null
  }
}
```

Makna `computed`: `rows_matched`/`rows_total` memberi tahu **berapa baris yang benar-benar ikut
dihitung** (kalau ada baris yang nilainya tidak bisa dibaca sebagai angka, `rows_skipped` > 0 —
angka tetap jujur, tidak diperkirakan). `result` sudah berisi angka hasil hitung; LLM tidak pernah
menghitung ulang. `explanation` ditulis kode, bukan model.

Bila permintaannya tidak bisa dipetakan ke kolom nyata (mis. "berapa total kolom diskon?" padahal
kolom `Diskon` tidak ada), sistem **menolak** dan mengisi `table_note` dengan alasannya; tidak ada
angka karangan.

`table_note` juga diisi (dan kalimatnya **ditambahkan ke `answer`**) bila tabel yang dipakai tidak
utuh: barisnya dipotong batas aman, ada baris judul lembar yang dilewati, atau ada kolom tanpa isi.
Pemotongan data tidak pernah senyap - pengguna melihat cakupan angkanya, bukan hanya percaya.

### `GET /tables`

Daftar tabel terstruktur yang tersimpan untuk pemanggil (tenant + knowledge base + dokumen),
berguna untuk memastikan berkas spreadsheet benar-benar terbaca sebagai tabel.

```json
{"success": true, "data": {"count": 2, "analytics_enabled": true, "tables": [
  {"table_id": 12, "document_id": "doc_xlsx", "document_name": "penjualan_agustus_2026.xlsx",
   "knowledge_base_id": "kb_penjualan", "sheet": "Penjualan",
   "headers": ["Tanggal", "Kode", "Produk", "Kategori", "Jumlah", "Harga Satuan", "Total"],
   "row_count": 40, "truncated": false, "notes": []},
  {"table_id": 13, "document_id": "doc_xlsx", "document_name": "penjualan_agustus_2026.xlsx",
   "knowledge_base_id": "kb_penjualan", "sheet": "Ringkasan", "headers": ["Keterangan", "Nilai"],
   "row_count": 3, "truncated": false,
   "notes": ["judul lembar: RINGKASAN DATA PENJUALAN", "kolom tanpa isi: Catatan"]}]}}
```

`notes` berasal dari pembaca tabel: baris judul lembar yang dilewati, baris yang dipotong batas aman
(`truncated: true`), kolom kosong yang dibuang, dan sel tanggal yang diterjemahkan. Semuanya ikut
tersimpan di basis data tabel (`table_store`), jadi laporan ini tetap benar setelah layanan restart.
Ekstensi tabel yang didukung: `.xlsx`, `.xlsm`, `.xls` (BIFF8 biner), `.ods`, `.csv`, `.tsv`.

Butuh permission `read`; `organization_id` tidak pernah dikirim klien — selalu dari kunci API.

Catatan `usage` (supaya angkanya tidak disalahartikan):

- `reranked_chunks` bernilai 0 dan `reranker` bernilai `none` bila reranker memang tidak
  dijalankan (`RERANKER_PROVIDER=none`); layanan tidak pernah menghitung rerank yang tidak terjadi.
- `rerank_ms` adalah waktu tahap rerank saja, `retrieval_ms` tahap pencarian (dense + BM25 + fusi),
  dan `generation_ms` panggilan LLM. Ketiganya diukur, bukan placeholder.

Percabangan no-answer (PRD 16) — `grounded=false`, `sources=[]`:

| `no_answer_reason` | Kapan |
|---|---|
| `no_candidates` | tidak ada kandidat sama sekali di tenant/knowledge base itu |
| `below_threshold` | best score < threshold → LLM **tidak dipanggil** |
| `strict_grounding` | LLM menyatakan konteks tidak cukup, dan `strict_grounding=true` |
| `context_empty` | kandidat ada tapi tidak ada yang masuk budget konteks |

Jawaban kanonik: `Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia.`

## POST /search

```json
{"query": "prosedur cuti tahunan", "knowledge_base_id": "kb_hr", "top_k": 10}
```

```json
{"success": true, "data": {"results": [
  {"document_id": "doc_123", "chunk_id": "chunk_0004", "content": "Pengajuan cuti...",
   "score": 0.94, "page": 12, "document_name": "SOP Cuti.pdf", "section": "SOP Cuti Tahunan / Pengajuan",
   "source_url": "..."}], "route": {"capability": "knowledge_search", "...": "..."}}}
```

## POST /extract

```json
{"query": "Ambil data penjualan bulanan Q1", "knowledge_base_id": "kb_sales",
 "output_schema": {"type": "array", "items": {"type": "object"}},
 "top_k": 10}
```

```json
{"success": true, "data": {"items": [{"month": "January", "sales": 100000000}],
  "sources": [{"document_id": "doc_sales_q1", "page": 3, "...": "..."}],
  "not_found": false, "route": {"capability": "knowledge_extract", "...": "..."}}}
```

LLM diminta `response_format=json_object`; bila keluar dari JSON, ada satu percobaan
perbaikan sebelum menyerah (`not_found=true`).

## GET /health, GET /ready, GET /metrics

- `/health` → `{"status": "ok"}` — liveness, tidak menyentuh dependensi.
- `/ready` → `{"status": "ready|degraded|not_ready", "dependencies": {"qdrant","embedding","reranker","llm","jev"}, "detail": {...}}`.
  `qdrant`/`embedding` = kritis (gagal → **503**). `detail` menyebut provider+model yang
  benar-benar dipakai, jumlah worker, status Jev, plus kebijakan unggahan yang sedang berlaku:
  `max_upload_mb`, `allowed_mime`, dan `allowed_extensions` (daftar ekstensi efektif - UI
  memakainya untuk ringkasan format di area tarik-lepas dan penolakan dini di browser).
- `/metrics` → counters (`http_requests_total`, `documents_indexed`, `chunks_created`,
  `no_answer_total`, `prompt_injection_flags`, ...), latency p95/p99 (`retrieval_latency`,
  `generation_latency`, `http_request_latency`), plus keadaan worker/collection (PRD 37).

## Pengaturan runtime - GET/PUT /settings, POST /settings/llm/models, POST /settings/jev/probe

Konfigurasi generator dan Jev bisa diubah tanpa restart lewat berkas override
(`SETTINGS_OVERRIDE_PATH`, default `data/settings.json`). Env tetap sumber kebenaran saat boot;
setelah itu berkas menang untuk field yang diisi.

**Semua endpoint di bawah wajib izin `admin`** - konfigurasi ini global (mengubah generasi dan
routing untuk semua tenant), jadi kunci read/write biasa dijawab `403 AUTH_FORBIDDEN`.

| Endpoint | Isi |
|---|---|
| `GET /settings` | `{sections: {llm: {provider, base_url, model, api_key_set, api_key_hint}, jev: {enabled, mode, provider, systemone_url, model, mcp_url, api_key_set, api_key_hint}, uploads: {extensions: [...efektif], max_upload_mb}}, catalog: [...], source}` |
| `PUT /settings` | body `{llm?: {...}, jev?: {...}, uploads?: {extensions?: [".pdf", ...], max_upload_mb?: 25}}`; field yang tidak dikirim tidak diubah, `""` = hapus, `applied` mengembalikan daftar field yang benar-benar tertulis |
| `POST /settings/llm/models` | body `{base_url?, api_key?}` → `{base_url, count, latency_ms, models: [{id, owned_by}]}` dibaca dari `GET {base_url}/models` |
| `POST /settings/jev/probe` | body `{provider?, url?, model?, api_key?}` → menjalankan satu pertanyaan `noul` sungguhan, balas `{model, latency_ms, answer: {type, ...}}` |

Catatan kontrak:

- **API key tidak pernah dikembalikan.** `api_key_set` (bool) dan `api_key_hint` (mis. `sk-d...15e8`)
  saja. Body `PUT` tanpa `api_key` = kunci tersimpan dipakai apa adanya; `""` = kunci dihapus.
- `POST /settings/llm/models` dan `/settings/jev/probe` memakai nilai dari body bila ada, kalau
  tidak dari setelan efektif - jadi URL + key + model bisa diuji **sebelum** disimpan.
- URL probe divalidasi lebih dulu (`validate_probe_url`): hanya `http`/`https`, tanpa kredensial
  di URL, tanpa alamat link-local/metadata (`169.254.0.0/16`, `fd00:ec2::254`) dan tanpa redirect.
- Kegagalan provider diteruskan sebagai `502 LLM_FAILED` / `502 JEV_FAILED`, bukan 500 generik.

### Format berkas (`uploads`)

| Bagian | Isi |
|---|---|
| `sections.uploads.extensions` | daftar ekstensi **efektif** yang diterima, sudah disaring ke format yang benar-benar bisa dibaca mesin ini (urut abjad) |
| `sections.uploads.max_upload_mb` | batas ukuran berkas; dipakai pre-flight (413) dan dilaporkan di `GET /ready` |
| `catalog` | satu baris per format: `{key, label, group, extensions, mime, parser, available, note}` (`available=false` + `note` bila pustaka/program pendukungnya tidak ada) |

Aturan `PUT`:

- Nilai `extensions` menerima list atau string dipisah koma, dengan atau tanpa titik (`.CSV`, `csv`
  sama saja) dan dinormalkan ke huruf kecil; ekstensi di luar katalog → `422 VALIDATION_ERROR`
  (daftar yang dikenal dikembalikan di `details.available`).
- Memilih **hanya** format yang tidak didukung mesin ini → `422` dengan `details.unavailable`;
  `extensions: []` → `422`. Penolakan terjadi sebelum berkas setelan ditulis.
- `max_upload_mb` di luar 1..512 (atau bukan angka) → `422`.
- Perubahan berlaku untuk permintaan berikutnya tanpa restart: validasi unggahan
  (`415 UNSUPPORTED_MEDIA_TYPE` / `413 PAYLOAD_TOO_LARGE`) dan `GET /ready.detail.allowed_extensions`
  membaca setelan yang sama, dan `PUT` sudah mengembalikan `catalog` + daftar efektif terbaru
  supaya UI tidak perlu menebak.
- Batas ukuran diperiksa **sebelum** job dibuat untuk sumber `text`/`content_base64`
  (panjang base64 dihitung, tidak didekode); untuk `file_url` pemeriksaan tetap di worker karena
  ukuran hanya diketahui setelah unduhan.
- Perubahan langsung berlaku: klien LLM dibangun ulang (`Generator.rebind_llm_client`) dan klien
  Jev membaca setelan per panggilan. `GET /ready` melaporkan model/provider efektif setelahnya.
