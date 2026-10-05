# Arsitektur

## Alur permintaan (PRD 12)

```
Client Application
      │  Authorization: Bearer <key>  |  X-Tenant-Context: <KMS token>
      ▼
RAG API (FastAPI)
      │  middleware: request_id, log JSON, metrik     (main.py)
      ▼
Authentication → TrustedContext                      (api/middleware/auth.py)
      │  ApiError AUTH_INVALID / AUTH_FORBIDDEN
      ▼
Tenant guard                                          (api/middleware/tenant.py)
      │  tolak organization_id dari body (422) SEBELUM kerja
      ▼
Jev route → capability                                (jev/router.py, jev/policies.py)
      │  transport: MCP tools/call (JEV_PROVIDER=mcp) ATAU System One POST {state,questions}
      │  gagal? → fallback heuristik, source="fallback"
      ▼
Hybrid retrieval (tenant-filtered)                    (rag/retriever.py)
      │  ├─ dense: embedder → Qdrant + filter org/kb
      │  └─ BM25 : sparse index per (org, kb)
      │  RRF fusion → dedupe → re-read payload (filter tenant lagi)
      ▼
Reranker → threshold                                  (rag/reranker.py)
      │  di bawah threshold → no-answer (LLM tidak dipanggil)
      ▼
Context builder (fenced, cited, budgeted)              (rag/context.py)
      ▼
LLM (endpoint OpenAI-compatible; model dari setelan)   (rag/generator.py)
      ▼
Answer + sources, atau no-answer
```

Model jawaban **tidak diikat ke satu model**: `rag/generator.py` memanggil endpoint chat-completions apa pun
yang cocok OpenAI (`LLM_BASE_URL`), dan nama modelnya dibaca per panggilan dari setelan (`llm.model`, diubah
dari layar Pengaturan atau env). `Qwen/Qwen3-4B` hanyalah nilai default di kode (sisa target PRD untuk server
lokal vLLM/Ollama) - bukan model yang wajib dipakai. Rincian model yang sudah diuji: README, bagian
"Model LLM".

## Modul dan tanggung jawab

| Modul | Tanggung jawab | Catatan desain |
|---|---|---|
| `core/config.py` | semua knob lewat env | profil low-RAM vs GPU tanpa ubah kode |
| `core/errors.py` | taksonomi + envelope PRD 29 | satu bentuk error untuk semua rute |
| `core/security.py` | API key, token KMS (HS256), validasi file (label manusia, MIME, ukuran pre-flight), redaksi log, marker prompt-injection | token tenant hanya dari sini; daftar ekstensi mengikuti setelan runtime, dan daftar eksplisit yang kosong menolak semua (tidak diam-diam kembali ke bawaan) |
| `core/tenant.py` | `TrustedContext` | di `core`, bukan `api`, agar pipeline tidak mengimpor FastAPI (mencegah import melingkar yang membuat tenant opsional) |
| `parsing/formats.py` | katalog format: label, grup, ekstensi, MIME, parser, syarat pustaka; `availability()` + `enabled_extensions(settings)` | **satu sumber kebenaran** untuk validasi unggahan, laporan `/ready`, layar Pengaturan, dan pemilihan parser |
| `parsing/parser.py` | 16 parser (PDF, DOCX, XLSX, PPTX, ODF, RTF, EPUB, CSV, XML, HTML, JSON/teks, OCR, DOC/XLS/PPT biner, EML) → `ParsedDocument` per halaman; `parser_for()` memilih **dari isi berkas** lebih dulu, baru suffix/MIME | satu halaman rusak tidak mematikan dokumen; berkas non-UTF-8 didekode bertingkat; **seluruh bagian** ikut dibaca (header/footer, catatan kaki, komentar, properti, catatan pembicara, master, header/footer cetak, komentar sel) |
| `parsing/ole.py` | pembaca kontainer OLE2/CFB (header 512 B, entri direktori, rantai FAT + mini-FAT, aliran kecil) | pustaka standar saja: `.doc`/`.xls`/`.ppt` bisa dibaca di mesin yang tidak boleh memasang `olefile` |
| `parsing/doc_binary.py` | Word 97-2003: FIB, **piece table** (CLX) di `0Table`/`1Table`, potongan ANSI + Unicode, kode kontrol → paragraf/tab/batas halaman | teks utama, catatan kaki, header/footer, kotak teks dibaca terpisah-bagian; instruksi field dibuang, isinya tidak |
| `parsing/xls_binary.py` | Excel 97-2003 (BIFF8): `BOUNDSHEET`, `SST`+`CONTINUE`, `LABELSST/LABEL/RK/MULRK/NUMBER/BOOLERR/FORMULA+STRING`, `XF`+`FORMAT` | sel tanggal dikenali dari gaya selnya → tanggal ISO, bukan serial 46075; baris menyimpan posisi kolom apa adanya |
| `parsing/ppt_binary.py` | PowerPoint 97-2003: rekaman berjenjang (`Document`/`Slide`/`Notes`/`Master`), atom `TextCharsAtom`/`TextBytesAtom`/`CString` | satu slide satu bagian, seluruh atom teksnya dibaca berurutan |
| `parsing/sheet_dates.py` | `styles.xml` + `date1904` → sel bertipe tanggal ditulis sebagai tanggal ISO; `looks_like_date_column()` untuk berkas tanpa gaya tanggal | dipakai bersama `parser.py` (teks) dan `tables.py` (perhitungan) supaya perilakunya sama |
| `parsing/tables.py` | ekstraksi **tabel terstruktur** (xlsx/xlsm/xls/ods/csv/tsv): `TableData`, `extract_tables()`, `supports_tables()` | sel kosong dipertahankan posisinya, nilai disimpan apa adanya; **lebar tabel dari seluruh baris** (baris judul lembar tidak lagi memotong kolom) dan baris judul/pemotongan/kolom kosong dicatat di `notes`; berkas non-tabel mengembalikan daftar kosong (jujur, bukan tebakan) |
| `tables/store.py` | `TableStore` (SQLite `tables` + `table_rows`) | setiap kueri wajib membawa `organization_id`; tidak ada metode lintas tenant; hapus dokumen = hapus tabelnya |
| `tables/analytics.py` | `build_plan` → `validate_plan` → `execute_plan` → `narrate` | **LLM hanya memilih operasi + nama kolom**, angkanya dihitung kode dari seluruh baris; rencana tak valid ditolak dengan alasan (`PlanError`) |
| `rag/chunker.py` | chunk sadar heading/tabel + offset | tabel utuh, heading path disimpan |
| `rag/embedder.py` | provider embedding + normalisasi L2 | `unload()` untuk mesin RAM kecil |
| `rag/sparse.py` | BM25 per (org, kb), persist JSON | BM25**Plus** (idf tetap positif untuk korpus kecil) |
| `rag/retriever.py` | dense + BM25 + RRF + rerank + threshold | payload dibaca ulang lewat filter tenant |
| `rag/reranker.py` | cross-encoder / noop | `none` = jangan filter skor semu |
| `rag/context.py` | blok konteks bernomor sitasi + budget token | `fence_document` + `scan_injection` |
| `rag/generator.py` | prompt PRD 17/35, jawaban + ekstraksi JSON | `MockLLMClient` untuk tanpa model |
| `rag/pipeline.py` | orkestrasi end-to-end | rute HTTP tetap tipis |
| `qdrant/*` | klien, koleksi, repository | `tenant_filter()` adalah satu-satunya pembuat filter |
| `jev/*` | capability, router, klien MCP JSON-RPC + klien System One native | Jev tidak pernah menentukan tenant; dua transport, satu kontrak, selalu fail-open ke heuristik |
| `workers/indexing.py` | queue asinkron + pipeline + job store | status tahan restart |

## Tempat data duduk di disk

Tidak ada RDBMS di jalur ini (tidak ada MySQL/Postgres/Redis). Semua keadaan runtime ada di bawah
`data/` (di mesin ini: `data/live/`), dengan empat penyimpanan berbeda plus satu berkas setelan.
Berkas spreadsheet disimpan **dua kali dengan peran berbeda**: potongan teks masuk ke jalur vektor
(dicari seperti dokumen biasa), sedangkan barisnya masuk `TableStore` supaya agregat
("total", "paling laku") bisa dihitung tanpa memuat ulang berkasnya:

| Apa | Di mana | Bentuk | Contoh nyata |
|---|---|---|---|
| Baris tabel terstruktur (xlsx/csv/ods) | `data/live/tables.sqlite` (atau `TABLE_STORE_PATH`) | SQLite: `tables` (nama kolom, jumlah baris) + `table_rows` | satu baris per dokumen; dasar perhitungan agregat yang deterministik |
| Chunk + vektor (dense) | `data/live/qdrant/collection/<koleksi>/storage.sqlite` | Qdrant embedded menulis satu berkas SQLite: tabel `points(id TEXT, point BLOB)`; tiap BLOB adalah `PointStruct` ter-pickle berisi vektor + payload | koleksi `live_chunks`: 57 titik, vektor 384 dimensi |
| Token untuk BM25 (sparse) | `data/live/sparse/<org>__<kb>.json` | satu berkas per (tenant, knowledge base): `{organization_id, knowledge_base_id, entries: [{chunk_id, document_id, tokens: [...]}]}`; IDF dihitung saat pencarian, bukan disimpan | `org_a__kb_chat.json`, 37 entri = 37 chunk |
| Catatan dokumen + job | `data/live/jobs.json` | `{"jobs": [ {job_id, document_id, knowledge_base_id, document_name, status, stage, chunks, tokens, pages, attempts, duration_ms, source_url, created_at, updated_at} ]}` | 22 job; daftar dokumen di UI dibaca dari sini, bukan dari Qdrant |
| Setelan runtime | `data/live/settings.json` | override LLM/Jev dari layar Pengaturan (lihat `docs/api.md`) | hanya field yang diubah |

Isi satu payload titik (contoh sungguhan, dokumen `Modul_Hosting_Kroombox_v2.2.pdf`):

```
chunk_id=chunk_0002  chunk_index=2  page=5  section=''  token_count=530
document_id=doc_kroombox  document_name=Modul_Hosting_Kroombox_v2.2.pdf
organization_id=org_a  knowledge_base_id=kb_docs  language=id  is_table=False
extra={}  created_at=2026-09-29T19:24:31Z
source_url=file:///C:/Users/.../Modul_Hosting_Kroombox_v2.2.pdf
content='apt upgrade -y Tools umum sudo apt install -y git curl wget unzip zip ca-certificates …'
```

Jadi **teks chunk disimpan di dalam payload Qdrant** (bukan di berkas terpisah dan bukan di tabel
SQL). Yang tidak disimpan: **berkas asli dan teks hasil parse**. `STORAGE_DIR` dan `REGISTRY_PATH`
ada di konfigurasi tetapi hanya dipakai untuk membuat direktorinya - tidak ada modul yang menulis
ke sana. Konsekuensinya: mengubah setelan chunk berarti mengunggah ulang dokumen (atau memakai
`file_url` yang sumbernya masih ada). Kalau nanti perlu re-chunk tanpa unggah ulang, tempatnya
adalah `STORAGE_DIR` (simpan byte/teks yang sudah diparse) + registry dokumen yang sungguhan.

Point id sengaja bukan `chunk_id`: `uuid5(NAMESPACE_URL, "rag-service:<org>|<kb>|<document>|<chunk>")`,
sehingga `chunk_0001` dari dokumen berbeda tidak saling menimpa.

## Keputusan arsitektur penting

1. **Qdrant server atau embedded.** `QDRANT_URL` kosong → mode lokal (`path=`), API
   klien sama. Mesin dev tanpa Docker tetap bisa menjalankan seluruh pipeline.
2. **Point id ≠ chunk id.** `chunk_id` hanya unik di dalam satu dokumen (`chunk_0001`),
   jadi point id = UUID5 dari `org|kb|document|chunk`. Tanpa ini, dokumen kedua
   menimpa vektor dokumen pertama (bug yang benar-benar terjadi dan sekarang dijaga
   oleh test regresi).
3. **Re-read payload sebelum LLM.** Hasil pencarian belum dipercaya; kandidat diambil
   ulang lewat filter tenant, sehingga chunk lintas organisasi tidak mungkin lolos
   walaupun index tercemar.
4. **Dua sumbu kegagalan independen.** `reranker_provider=none` membuat rerank menjadi
   no-op dan **melewatkan** gerbang threshold (skor pseudo tidak boleh memfilter);
   gerbang tetap aktif ketika reranker asli dipakai. Bawaan sekarang `lexical` - reranker
   lintas-encoder tanpa dependensi yang menilai ulang lewat IDF kueri, frasa utuh, kedekatan
   kata, dan cakupan kueri, lalu menormalkan skor terbaik ke 1.0. Provider neural yang
   pustakanya tidak terpasang turun ke `lexical` **dan mencatatnya di log**, bukan diam-diam
   berakhir `none` (yang bukan reranker sama sekali).
5. **Jev adalah decision layer.** Kegagalan Jev → heuristik deterministik, bukan 5xx.
   Aplikasi tetap menjawab, dan respons jujur menyebut `source="fallback"`.

## Deployment (PRD 43, 44)

- **Dev**: `app.main` (uvicorn) + Qdrant embedded/Docker. Profil model bisa `hash`/`mock`
  agar tidak perlu mengunduh apa pun.
- **Prod**: RAG API (replika N) + Qdrant server (backup volume) + Jev + LLM service,
  semua di belakang API gateway yang menerbitkan trusted context. Tambahan yang disebut
  PRD tapi **belum** ada di repo ini: TLS terminasi, rotasi API key otomatis,
  centralized logging/monitoring, worker queue eksternal, rate limit terdistribusi.
