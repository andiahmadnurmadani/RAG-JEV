# Evaluasi RAG (PRD 38, 39)

Harness: `tests/evaluation/run_eval.py` — menjalankan pipeline **nyata**
(parse → chunk → index → hybrid retrieval → rerank → ground) terhadap korpus
`tests/evaluation/corpus/` (7 dokumen SOP Indonesia) dan `dataset.json`
(9 pertanyaan yang bisa dijawab + 3 pertanyaan jebakan yang **tidak** ada jawabannya).

```bash
python -m tests.evaluation.run_eval                                  # profil cepat (hash, tanpa unduhan)
EMBEDDING_PROVIDER=fastembed RERANKER_PROVIDER=fastembed python -m tests.evaluation.run_eval
EMBEDDING_PROVIDER=sentence_transformers RERANKER_PROVIDER=sentence_transformers python -m tests.evaluation.run_eval
python -m tests.evaluation.run_eval --no-hybrid                      # ablasi: dense saja
```

Laporan JSON ditulis ke `tests/evaluation/report.json` (atau `--report <path>`), memuat
konfigurasi, korpus, metrik, dan tiap kasus (`retrieved` vs `expected`) supaya angka bisa
ditelusuri, bukan sekadar ringkasan.

## Metrik yang diukur

| Kelompok | Metrik | Definisi di sini |
|---|---|---|
| Retrieval | Recall@K | proporsi dokumen relevan yang muncul di `top_k` |
| Retrieval | Precision@K | proporsi hasil di `top_k` yang memang relevan |
| Retrieval | MRR | 1/rank dokumen relevan pertama |
| Retrieval | Hit rate | ada ≥1 dokumen relevan di `top_k` |
| Generation | grounded rate | jawaban memakai konteks (tidak menolak) pada kasus yang bisa dijawab |
| Generation | citation present rate | jawaban memuat ≥1 sitasi |
| No-answer | refusal rate | pada pertanyaan jebakan, sistem menolak menjawab |
| Operasional | index_seconds | waktu index seluruh korpus |

## Hasil terukur

Korpus: **10 dokumen** (7 SOP pendek + 3 dokumen panjang multi-pasal sebagai pengecoh),
**11 chunk**, **15 pertanyaan terjawab**, **3 pertanyaan jebakan**. Semua angka dihasilkan
mesin ini; satu laporan JSON per konfigurasi di `data/eval/report-*.json`, ringkasan teks di
`data/eval/matrix.txt` (dibuat oleh `scripts/eval_matrix.sh`).

| Konfigurasi | Recall@5 | Precision@5 | MRR | Hit rate | grounded | sitasi | refusal (jebakan) |
|---|---|---|---|---|---|---|---|
| `hash` + hybrid | 1.00 | 0.217 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 |
| `hash` + dense saja | 1.00 | 0.217 | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 |
| `bge-m3` + hybrid | 1.00 | 0.220 | 0.967 | 1.00 | 1.00 | 1.00 | 0.00 |
| `bge-m3` + dense saja | 1.00 | 0.213 | 0.967 | 1.00 | 1.00 | 1.00 | 0.00 |
| **`bge-m3` + hybrid + `bge-reranker-v2-m3`** | **1.00** | **0.867** | **1.00** | **1.00** | **1.00** | **1.00** | **1.00** |

Cara membacanya — jangan lebih dari yang dibuktikan:

- Dengan `final_top_k=5` dan korpus 10 dokumen, hampir semua konfigurasi menemukan dokumen
  yang benar; **Recall@5 = 1.00 di keempat baris membuktikan harness-nya bekerja, bukan bahwa
  keempat konfigurasi setara kualitasnya.** Precision@5 ≈ 0.21 adalah konsekuensi aritmetika
  (5 hasil untuk 1 dokumen yang diharapkan), bukan tanda kualitas rendah.
- Korpus ini masih terlalu kecil/homogen untuk memisahkan `hash` dari `bge-m3`. Untuk klaim
  kualitas semantik dibutuhkan korpus lebih besar, pertanyaan parafrase tanpa kata kunci yang
  sama, dan reranker aktif. Yang **sudah** terbukti berbeda antara keduanya adalah biaya:
  `hash` meng-index 11 chunk dalam 0,18 s, `bge-m3` butuh 26-31 s di CPU.
- `refusal rate = 0.00` karena `RERANKER_PROVIDER=none` → gerbang threshold sengaja dilewati
  (skor pseudo tidak boleh memfilter). Tiga pertanyaan jebakan tetap dijawab; itu sebabnya
  reranker bukan sekadar "nice to have".
### Efek reranker nyata: satu baris yang mengubah gambarannya

Baris terakhir tabel adalah ablation yang paling penting: `BAAI/bge-reranker-v2-m3`
(CrossEncoder, CPU) dengan `rerank_threshold=0.35` benar-benar mengikat.

- **Refusal atas 3 pertanyaan jebakan naik 0.00 → 1.00** (`false_grounded` kosong). Sebelumnya
  ketiga jebakan dijawab dengan percaya diri — persis mode kegagalan yang paling berbahaya.
- **Precision@5 naik 0.22 → 0.867**: gerbang threshold membuang kandidat yang skornya di bawah
  ambang, sehingga yang tersisa hampir semuanya relevan.
- Recall@5/MRR/Hit rate tetap 1.00 → tidak ada dokumen benar yang hilang karena difilter.

Angka ini menjelaskan kenapa `RERANKER_PROVIDER=none` bukan mode produksi: selama gerbang
threshold tidak dijalankan, "strict grounding" hanya sekuat prompt, bukan sekuat skor.

Laporan: `data/eval/report-reranked-bge-m3.json` (dan `report-reranked-hash.json` untuk
varian `hash`, yang memisahkan kualitas reranker dari kualitas embedding).

- `grounded rate = 1.0` tidak berarti jawabannya benar — lihat catatan "grounded ≠ benar"
  di bagian metodologi.

### Dua bug yang ditemukan justru karena menjalankan ablation

Keduanya lolos dari seluruh test satuan/integrasi sebelum ada ablation:

1. **Jalur dense praktis mati.** `repository.search_dense()` mengembalikan `chunk_id` dan
   `payload`, tetapi tidak `document_id` di level atas. Kunci fusi dibangun dari
   `(document_id, chunk_id)`, jadi setiap hit dense dari setiap dokumen runtuh ke kunci yang
   sama (`"::chunk_0001"`) dan hanya menyisakan satu kandidat.
   Bukti sebelum/sesudah pada kueri yang sama, `use_hybrid=False`:

   ```
   sebelum : dense_hits=7  fused=1   candidates=0  best=0.0   -> no_candidates
   sesudah : dense_hits=11 fused=11  candidates=5  best=1.0   -> doc_sop_cuti di peringkat 1
   ```

   Efeknya tersembunyi total di mode hybrid: BM25 memasok kunci yang benar, sehingga hasil
   akhir tetap tampak sempurna sementara komponen vektor tidak menyumbang apa pun. Regresi
   dijaga test `test_dense_only_search_returns_every_matching_document`.
2. **Definisi Precision@K salah.** Penyebutnya `min(k, len(expected))`, sehingga saat korpus
   lebih kecil dari K, Precision@K selalu sama dengan Recall@K (angka 1.00 yang tidak
   mengukur apa pun). Sekarang memakai definisi standar: hasil relevan / hasil yang
   dikembalikan.

Juga ditambahkan penjagaan: `upsert_chunks(dim<=0)` kini gagal `EMBEDDING_FAILED`, bukan
membuat koleksi berukuran 0 yang diam-diam menolak semua vektor (terjadi bila embedder belum
memuat model saat index pertama).

## Metodologi & batasan

- LLM memakai `MockLLMClient` kecuali `LLM_PROVIDER` diset: profil mock menjawab dengan
  menyalin konteks + penanda sitasi, sehingga yang diukur adalah retrieval & kebijakan
  grounding, bukan kualitas bahasa model.
- `grounded` untuk no-answer ditentukan oleh (a) tidak ada kandidat, (b) skor < threshold,
  (c) model menyatakan konteks kurang + `strict_grounding`. Ketiganya teruji unit
  (`tests/unit/test_pipeline_no_answer.py`).
- Korpus eval kecil (7 dokumen) dan sintetis: cukup untuk regresi dan perbandingan model,
  **tidak** cukup untuk mengklaim kualitas produksi. PRD 39 meminta dataset internal —
  ganti `corpus/` dan `dataset.json` dengan data nyata saat tersedia, lalu jalankan ulang
  perintah yang sama.
- Belum diukur: latency benchmark per tahap pada model sungguhan (bge-m3/reranker) dan
  faithfulness berbasis LLM-judge. Kerangka kerjanya sudah ada (jalankan dengan `--report`
  berbeda per konfigurasi), hasilnya belum dikumpulkan.
- `EVAL_WORKSPACE=<dir>` memisahkan workspace Qdrant/sparse antar-run, sehingga dua
  konfigurasi bisa dievaluasi paralel tanpa saling mengunci (Qdrant embedded mengunci file).
