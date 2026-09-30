# RAG Pipeline

## 1. Chunking (PRD 11)

Dokumen dipecah per halaman, lalu per blok (heading / tabel / paragraf) dengan offset
karakter yang presisi:

- **heading** (`#`, `##`, `BAB`, `Pasal`, `1.2 Judul`) → membentuk `heading path`
  yang disimpan di setiap chunk (`"SOP Cuti Tahunan / Pengajuan"`);
- **tabel** dikenali bila ≥60% baris adalah baris pipe/tab → tabel tidak dipecah selama
  masih dalam budget, dan diberi flag `is_table`;
- **paragraf** yang lebih besar dari budget dipotong di batas kalimat;
- potongan kecil (< `MIN_CHUNK_TOKENS`) digabung ke chunk sebelumnya → tidak ada chunk sampah,
  kecuali blok itu punya heading sendiri (itu section, bukan ekor);
- `CHUNK_OVERLAP` menambahkan ekor chunk sebelumnya agar batas tidak menjadi potongan keras.

Saat di-index, teks diberi header kontekstual (`section | page N`) supaya chunk yang
berdiri sendiri tetap bisa dipahami model embedding.

## 2. Embedding (PRD 8.2)

| Provider | Model default | Kapan dipakai |
|---|---|---|
| `sentence_transformers` | `BAAI/bge-m3` | profil PRD-exact (torch, ~2.3 GB RAM) |
| `fastembed` | `intfloat/multilingual-e5-large` | profil hemat RAM (ONNX int8) |
| `http` | apa pun | endpoint `/embeddings` OpenAI-compatible |
| `hash` | — | test/demo deterministik, tanpa unduhan |

Semua keluaran dinormalisasi L2 → metrik cosine. `EmbedderService.unload()` membebaskan
memori saat mesin kecil perlu ruang untuk reranker.

## 3. Hybrid retrieval + fusion (PRD 14)

```
query ──embed──> Qdrant (filter org+kb)  -> dense top 30 ─┐
      └─tokenize─> BM25 per (org,kb)     -> sparse top 30 ─┴─> RRF (k=60) -> dedupe
```

- **RRF** (reciprocal rank fusion) dipakai, bukan pencampuran skor: cosine dan BM25 tidak
  sebanding. Kandidat yang muncul di kedua daftar naik peringkatnya.
- **Identitas chunk = `"{document_id}::{chunk_id}"`** (`rag.retriever.fused_key`). Setiap
  dokumen menomori chunk-nya dari `chunk_0001`, jadi peta skor yang di-kunci `chunk_id`
  saja akan membuat dokumen kedua menghapus/menimpa dokumen pertama — baik di peta skor
  maupun di index BM25 (`SparseIndex.upsert` mencocokkan pasangan `(document_id, chunk_id)`).
  Regresi ini dijaga test `test_two_documents_do_not_overwrite_each_others_vectors`,
  `test_bm25_keys_are_document_qualified` dan `test_dense_only_search_returns_every_matching_document`.
  Karena itu `repository.search_dense()` **wajib** mengembalikan `document_id` di level atas —
  tanpa itu semua hit dense runtuh ke satu kunci dan jalur vektor diam-diam tidak menyumbang
  apa pun (dulu tersembunyi di mode hybrid karena BM25 menutupinya).
- `MAX_CHUNKS_PER_DOCUMENT` (default 3) mencegah satu dokumen memonopoli konteks.
- Setiap kandidat **dibaca ulang** dari Qdrant dengan filter tenant sebelum masuk tahap
  berikutnya (pertahanan berlapis).
- BM25 disimpan per `(organization_id, knowledge_base_id)` sebagai JSON: kueri lexikal
  org A secara struktural tidak bisa melihat entri org B.

## 4. Reranking + threshold (PRD 15, 16)

Cross-encoder menilai ulang pasangan (query, chunk); skor di-`sigmoid` ke [0,1].
Hanya chunk dengan skor ≥ `RELEVANCE_THRESHOLD` (default 0.35) yang dikirim ke LLM.

Bila `RERANKER_PROVIDER=none`, rerank menjadi no-op **dan gerbang threshold dilewati**
(skor pseudo tidak boleh men-drop chunk relevan) — sistem tetap menjawab, tetapi
kemampuan menolak berkurang; ini terlihat di laporan evaluasi.

## 5. Context & prompt (PRD 17, 35)

Setiap chunk dirender sebagai data inert yang dibatasi penanda:

```
<<<RETRIEVED_CONTEXT
[1]
<retrieved_document>
[DOCUMENT]
Name: SOP Cuti.pdf
Page: 12
Chunk: chunk_0004
[CONTENT]
...
</retrieved_document>
RETRIEVED_CONTEXT>>>
```

Ditambah system prompt PRD 17 (jangan mengarang, jangan pakai pengetahuan luar, sertakan
sitasi) dan pengingat PRD 35 (dokumen = data tak tepercaya). Marker prompt-injection yang
ditemukan di dokumen dihitung (`prompt_injection_flags`) dan **tidak pernah** diikuti.

Budget konteks = `LLM_CONTEXT_CHARS / 4` token (≈4 karakter/token); chunk yang tidak muat
dibuang dan dilaporkan sebagai `dropped`, bukan diam-diam dipotong.

## 6. Generation dan no-answer (PRD 16)

| Kondisi | Hasil |
|---|---|
| tidak ada kandidat | `no_candidates`, LLM tidak dipanggil |
| best score < threshold | `below_threshold`, LLM tidak dipanggil |
| konteks kosong setelah budget | `context_empty` |
| LLM bilang konteks kurang & `strict_grounding=true` | `strict_grounding`, `sources=[]` |

Sitasi hanya memuat chunk yang benar-benar ditampilkan ke model (`BuiltContext.used`),
sehingga `sources` tidak bisa memuat dokumen yang tidak dipakai menjawab.

## 7. Indexing asinkron (PRD 33, 32)

```
POST /knowledge/index -> job (queued) -> asyncio.Queue -> worker thread
   parse -> validate -> chunk -> embed -> hapus vektor lama (replace) -> upsert -> completed
```

- Job store dipersist ke `JOB_STORE_PATH`, jadi `GET /knowledge/{id}` tetap benar
  setelah restart.
- Update = `replace`: vektor + entri lexikal lama dihapus lebih dulu, sehingga tidak ada
  duplikat atau sisa kebijakan lama.
- Gagal di tengah jalan → status `failed` + kode error (`UNSUPPORTED_MEDIA_TYPE`,
  `INDEXING_FAILED`, ...), tanpa mengubah isi index secara setengah jalan.

## 8. Metrik (PRD 37)

`/api/v1/metrics`: `documents_indexed`, `documents_failed`, `chunks_created`,
`retrieval_requests`, `generation_requests`, `no_answer_total`, `prompt_injection_flags`,
`http_requests_total`, `http_errors_total`; latensi `retrieval_latency`, `generation_latency`,
`embedding_latency`, `http_request_latency` (avg/p95/p99/max); plus status worker, jumlah
entri lexikal per scope, info koleksi Qdrant.
