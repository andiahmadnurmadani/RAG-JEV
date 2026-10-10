# Akurasi retrieval & multi-tenant (Oktober 2026)

Ringkasan perbaikan yang membuat jawaban lebih tepat, tetap tepat saat knowledge bertambah banyak,
dan aman dipakai banyak proyek/klien dalam satu layanan.

## Hasil terukur

Diukur dengan `python -m tests.evaluation.run_eval` (embedder `hash`, reranker `lexical` -
konfigurasi produksi), dataset 22 pertanyaan sah (termasuk parafrase) + 6 pertanyaan di luar knowledge:

| Metrik | Sebelum | Sesudah |
|---|---|---|
| MRR (dokumen benar di peringkat atas) | 0,955 | **1,000** |
| Precision@12 | 0,546 | **0,586** |
| Pertanyaan di luar knowledge yang ditolak | 0% | **83%** |
| Pertanyaan sah yang salah ditolak | 0 | 0 |

Uji "knowledge banyak" (10 dokumen jawaban + 22 dokumen pengalih, 330 potongan): dokumen benar di
peringkat 1 naik dari 80% menjadi **93%**, termasuk untuk pertanyaan parafrase.

## Apa yang berubah

### Pencarian kata kunci (BM25) - `app/rag/sparse.py`, `app/rag/textnorm.py`

* **Bahasa Indonesia dipahami**: kata tugas ("yang", "bagaimana") dibuang; imbuhan dilepas tanpa kamus
  ("pengajuan"/"diajukan"/"mengajukan" bertemu di "aju", "kebijakan" → "bijak"). Bentuk asli tetap
  disimpan, jadi kecocokan persis bernilai lebih tinggi.
* **Hanya potongan yang cocok yang dikembalikan.** BM25Plus lama memberi skor ke SEMUA potongan, sehingga
  daftar hasil selalu penuh potongan yang tidak relevan.
* **Nama dokumen + bagian ikut terindeks** ("SOP cuti" cocok walau potongannya tidak mengulang nama itu).
* **Nominal & kode utuh**: `Rp1.500.000` → `rp1500000`; `SOP-12/2026` → `sop-12`.
* **Penanda ringkasan tidak hilang lagi** setelah restart/deploy.
* Indeks lama (versi 1) **dibangun ulang otomatis** dari Qdrant di latar belakang saat layanan menyala
  (`SPARSE_REBUILD_ON_STARTUP`); pencarian tetap jalan dengan indeks lama sampai versi baru siap.
  Vektor di Qdrant tidak disentuh.

### Penilaian & gerbang "tidak ditemukan" - `app/rag/reranker.py`, `app/rag/retriever.py`

* Skor reranker kini **absolut (0..1)** memakai IDF seluruh knowledge. Dulu skor terbaik selalu 1,0,
  sehingga ambang relevansi tidak pernah berfungsi.
* `min_relevance` (bawaan **0,30**): di bawah ini dijawab "tidak ditemukan" tanpa memanggil LLM.
  `relevance_threshold` (0,35) kini relatif terhadap hasil terbaik (membuang ekor daftar yang lemah).
* Embedder `hash` hanya menghitung kata; bobotnya di fusi diturunkan otomatis (`hash_dense_weight` 0,25)
  supaya tidak mengencerkan BM25. Dengan embedder semantik, kemiripan vektor juga dihitung sebagai bukti.
* Potongan berisi sama persis (boilerplate crawl, unggahan ganda) dibuang; batas per dokumen dipasang
  **sesudah** rerank.

### Konteks ke LLM - `app/rag/pipeline.py`, `app/rag/context.py`

* Setiap hasil ditemani **potongan tetangganya** (`context_neighbor_chunks`), bukan dokumen dari bagian 1.
  Dokumen kecil (`context_full_document_tokens`) dibaca utuh; dokumen utuh juga dibaca bila pertanyaannya
  meminta "seluruh/daftar lengkap/ringkas".
* Konteks disusun **per dokumen, urut posisi asli**; header blok memuat nama, bagian, halaman, posisi.
* `final_top_k` dari Pengaturan kini benar-benar dipakai bila klien tidak mengirim `top_k`.
* **Riwayat percakapan** (`history`, opsional): pertanyaan lanjutan pendek dicari bersama pertanyaan
  sebelumnya.
* Jalur tabel hanya untuk pertanyaan agregat (total, rata-rata, paling, ...), tabel diurutkan menurut
  kecocokan, dan jawabannya kini punya sumber.

### Generator - `app/rag/generator.py`

* Jawaban yang mengutip sumber tidak lagi dianggap penolakan ("Tidak terdapat biaya ... [1]" dulu
  diganti "tidak ditemukan").
* Prompt meminta jawaban parsial + menyebut bagian yang tidak ada; kalimat penolakan baku.
* Sitasi `[n]` ke blok yang tidak ada dibuang; `sources[].index` + `sources[].cited` menandai yang dikutip.
* `frequency_penalty` bawaan 0 (penalti merugikan angka/nama kolom yang memang berulang).

### Ingestion - `app/parsing/parser.py`, `app/workers/indexing.py`

* Tabel DOCX & HTML tetap **satu baris per baris tabel** (dulu seluruh tabel menyatu).
* Judul artikel dalam `<header>` tidak lagi terbuang; banner cookie, breadcrumb, tombol bagikan,
  `<aside>`, `role=navigation/banner` dibuang (blok pendek saja).
* Job yang terputus karena restart ditutup dengan status jelas; dokumen yang dihapus saat diindeks tidak
  hidup lagi; riwayat job tidak lagi menghilangkan dokumen lama dari daftar.

## Multi-tenant

* **Kunci per knowledge base** (`knowledge_base_ids` saat membuat kunci): kunci hanya bisa mencari,
  mengunggah, melihat, dan menghapus di KB miliknya - walau organisasinya sama. Penghapusan dibatasi ke
  KB dokumen itu, sehingga `document_id` yang sama di proyek lain aman. Kunci tanpa ikatan berperilaku
  seperti sebelumnya.
* Kunci baru **tidak boleh punya izin melebihi pembuatnya** (`read` tidak bisa membuat `*`/`admin`).
* Daftar/cabut kunci dibatasi ke organisasi pemanggil kecuali kunci `*`.
* Mode `CONSOLE_API_KEY_ONLY`: Pengaturan global terbuka untuk kunci organisasi operator
  (`UI_SESSION_ORGANIZATION_ID`) atau kunci berizin admin - bukan kunci tenant lain.
* Probe model/Jev hanya mengirim kunci tersimpan ke URL tersimpan.
* Percobaan kode akses dibatasi juga secara global (header IP palsu tidak bisa mengakalinya).

## Konsol (tampilan baru)

Konsol kini aplikasi satu halaman dengan navigasi samping (bilah bawah di ponsel):

| Halaman | Alamat | Isi |
|---|---|---|
| Knowledge base | `/ui/#/kbs` | Semua KB organisasi (jumlah dokumen, potongan, status, aktivitas terakhir); cari, urutkan, buat KB baru |
| Chat | `/ui/#/kb/<id>` | Ruang kerja satu KB: unggah, daftar dokumen, tanya-jawab |
| Uji akurasi | `/ui/#/eval` | Pertanyaan uji per KB |
| Pengaturan | `/ui/#/settings/<panel>` | Halaman biasa (bisa digulir), bukan dialog modal |

**Hapus knowledge base**: tombol tong sampah di kartu KB atau di ruang kerja KB. Dialog menampilkan
jumlah dokumen/potongan dan tombol hapus baru aktif setelah nama KB diketik persis. Lewat API:
`DELETE /api/v1/knowledge-bases/{id}?confirm={id}` (izin `write`; tanpa `confirm` yang cocok → 422).
Yang dihapus: semua vektor KB itu di Qdrant, indeks kata kunci, tabel terstruktur, dan status dokumen
ditandai `deleted` (pengindeksan yang sedang berjalan membuang hasilnya). Hanya KB organisasi pemanggil;
kunci yang diikat ke KB hanya bisa menghapus KB miliknya. Tidak bisa dibatalkan.

Daftar KB diambil dari `GET /api/v1/knowledge-bases` (kunci yang diikat ke KB hanya melihat KB
miliknya). KB lama yang catatan pengindeksannya terpangkas tetap terlihat lewat indeks kata kunci.


* **Pengaturan → Kualitas jawaban**: preset *Akurat / Seimbang / Dokumen lengkap*, ambang "tidak
  ditemukan", tetangga, dokumen utuh, mesin penilai.
* **Pengaturan → Uji akurasi**: daftar pertanyaan uji (`pertanyaan => nama dokumen` atau `=> -` untuk
  yang harus ditolak). Jalankan ulang setiap menambah dokumen.
* Pertanyaan dari konsol kini **ikut setelan layanan** (dulu reranker dipaksa mati), skor terbaik &
  dasar gerbang tampil di setiap jawaban, mode *Cari saja* menampilkan skor per tahap.
* Tombol **Percakapan baru**; pertanyaan lanjutan memakai riwayat.

## Belum diubah (butuh keputusan terpisah)

* **Embedder semantik.** Produksi masih `hash`: sinonim (karyawan/pegawai, jatah/kuota) belum tertangkap.
  Menggantinya berarti **semua potongan di-embed ulang** (operasi pada data produksi).
* Knowledge lama tetap memakai hasil parsing lama sampai diunggah ulang (tabel DOCX/XLSX, HTML).
