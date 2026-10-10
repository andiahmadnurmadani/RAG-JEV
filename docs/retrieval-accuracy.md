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

**Pertanyaan tak terjawab** (`/ui/#/unanswered`): setiap `/query` yang berakhir "tidak ditemukan"
dicatat per organisasi + KB. Pertanyaan yang sama (huruf besar/kecil, spasi, tanda baca diabaikan)
hanya menaikkan hitungan "ditanya N×". Operator bisa memfilter (KB, status, cari, terbaru/tersering),
menulis catatan, menandai selesai (terbuka lagi otomatis bila ditanya lagi dan masih tak terjawab),
"Tanya ulang" di KB-nya, menghapus satu/terpilih/semua sesuai filter. Bisa diatur di
**Pengaturan → Tak terjawab**: nyala/mati, alasan yang dicatat, lama simpan (bawaan 90 hari), batas
catatan per organisasi (bawaan 5000). API: `GET/PATCH /api/v1/unanswered`,
`POST /api/v1/unanswered/delete`, `DELETE /api/v1/unanswered?confirm=hapus`. Disimpan di
`unanswered.sqlite` di samping `tables.sqlite` (volume data). Menghapus KB ikut menghapus catatannya.

**Tampilan HP**: navigasi bawah mengambang (kaca buram, item aktif disorot, badge jumlah pertanyaan
tak terjawab), ruang kerja KB memakai tab Chat / Dokumen, input 16px (tidak memicu zoom iOS).

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

## Embedding semantik (mpnet) + penilaian gabungan

Embedding `hash` hanya menghitung kata, jadi pertanyaan dengan istilah lain dari dokumen
("jatah libur pegawai" vs "kuota cuti karyawan") tidak ditemukan. Image kini memuat
**`sentence-transformers/paraphrase-multilingual-mpnet-base-v2`** (ONNX via fastembed, diunduh saat
build ke `/models`, tanpa torch).

Hasil benchmark (32 dokumen campur pengalih; 16 pertanyaan sinonim tanpa kata yang sama, 23 fakta,
8 pertanyaan di luar knowledge):

| | hash | MiniLM | **mpnet + gabungan** | mpnet + reranker jina |
|---|---|---|---|---|
| Sinonim peringkat 1 | 3/16 | 12/16 | **15/16** | 13/16 |
| Fakta sampai ke konteks | 23/23 | – | **23/23 (semua di blok pertama)** | – |
| Di luar knowledge ditolak | 6/8 | 5/8 | **7/8** | – |
| Waktu per pertanyaan | 0,02 dtk | – | **0,3 dtk** | 26 dtk |

Reranker neural (jina-reranker-v2) diuji dan **tidak dipakai**: tidak lebih akurat, 80x lebih lambat.

**Penilaian gabungan** (`semantic_weight` 0,6, kalibrasi kosinus `semantic_floor` 0,25 →
`semantic_ceil` 0,80, `min_relevance` 0,28): skor akhir = makna + kata kunci. Reranker leksikal saja
membuang kandidat yang cocok maknanya tetapi tidak berbagi kata.

**Mengganti model** (Pengaturan → Kualitas jawaban → Mesin pencarian makna, atau
`PUT /settings {"embedding": {"provider": "fastembed", "model": ...}}`): setiap model punya koleksi
Qdrant sendiri (`knowledge_chunks__<model>`); seluruh potongan koleksi aktif sebelumnya di-embed ulang
di latar belakang dari isi yang tersimpan, tanpa unggah ulang. Progres di `GET /settings`
(`embedding_status`) dan `/ready`. Koleksi lama tidak dihapus. Setelan dari layar mengalahkan env
`EMBEDDING_PROVIDER`.

## Siap untuk ribuan dokumen: fokus dokumen

Diuji dengan **1032 dokumen** dalam satu knowledge base: 32 dokumen asli + 1000 dokumen pengalih
yang sengaja dibuat mirip (cuti melahirkan/cuti besar vs cuti tahunan, tunjangan shift vs lembur,
kunjungan tamu vs absensi, dst.), embedder mpnet, reranker leksikal.

| Ukuran | Sebelum | Sesudah |
|---|---|---|
| Dokumen berbeda di konteks (rata-rata) | 9,91 | **1,4** (maks 3) |
| Porsi konteks dari dokumen yang benar | 0,16 | **0,85** |
| Dokumen benar ada di konteks | 44/45 | 44/45 |
| Dokumen benar di peringkat 1 | 41/45 | **42/45** |
| Fakta jawaban sampai ke konteks / di blok pertama | 23/23 / 23/23 | 23/23 / 23/23 |
| Pertanyaan yang datanya ada tapi ditolak | 0 | 0 |
| Waktu per pertanyaan (tanpa LLM) | 0,46 dtk | 0,41 dtk |

Masalah sebelumnya: setiap potongan dinilai sendiri-sendiri, sehingga dengan ribuan dokumen
banyak potongan "agak mirip" lolos dan model membaca ~10 dokumen sekaligus, hanya 16% darinya
dokumen yang benar - jawaban rawan bercampur.

Perubahan:

- **Fokus dokumen** (`document_focus_ratio` 0,8, `max_context_documents` 3): dokumen lain hanya
  ikut dibaca bila skor terbaiknya >= 80% dokumen teratas, paling banyak 3 dokumen. Pertanyaan
  perbandingan ("perbedaan", "bandingkan", "setiap cabang", ...) otomatis dilonggarkan
  (rasio x 0,6, sampai 6 dokumen). Bisa disetel di Pengaturan -> Kualitas jawaban.
- **Kolam kandidat lebih dalam**: pencarian vektor & kata kunci masing-masing 60 (dulu 30),
  reranker menilai 120 kandidat. Dengan 1000 dokumen pengalih, dokumen SOP cuti untuk
  "siapa yang menyetujui izin cuti" ada di peringkat 56 (vektor) / 64 (kata kunci) dan dulu
  tidak pernah sampai ke reranker.
- **Aturan prompt**: model dilarang menggabungkan fakta dari dokumen berbeda kecuali diminta
  membandingkan; bila nilai berbeda antar cabang/unit/tahun, jawab dari dokumen yang sesuai
  pertanyaan atau sebutkan masing-masing beserta nama dokumennya.

Batas yang tersisa: 3 dari 8 pertanyaan di luar knowledge lolos gerbang skor pada korpus 1000
dokumen (topiknya mirip pengalih, mis. "kendaraan dinas"). Itu tetap dijawab "tidak ditemukan"
oleh model karena aturan strict grounding, tetapi memakai satu panggilan LLM.

## Audit production (putaran 3)

Tiga tinjauan kode independen (jalur jawaban, indexing/penyimpanan, keamanan) + pengukuran.
Setiap temuan diverifikasi di kode sebelum diperbaiki; semuanya punya tes regresi.

**Keamanan / multi-tenant**

- Pengaturan global (model, URL LLM, kunci, kode akses) kini hanya untuk kunci *tulis* atau
  admin milik organisasi operator, atau sesi konsol. Kunci yang diikat ke KB, kunci hanya-baca
  (yang dipasang di aplikasi chat), dan kunci admin tenant lain ditolak. Dulu kunci hanya-baca
  organisasi operator bisa mengganti URL LLM - dan konteks semua tenant ikut terkirim ke sana.
- `GET /tables` menghormati ikatan KB; `document_id` yang dipakai KB lain tidak bisa ditimpa.
- `/metrics` butuh kredensial operator (memuat nama org/KB semua tenant); cek LLM di `/ready`
  di-cache 60 dtk; `/auth/gate` tidak lagi memberi potongan kode akses; IP klien untuk pembatas
  login dari `CF-Connecting-IP`; tag pembatas konteks di isi dokumen dijinakkan; header
  `nosniff`, `X-Frame-Options`, `Referrer-Policy`.

**Data / indexing**

- Dokumen yang dihapus saat job-nya masih antre tidak lagi hidup kembali; tabelnya ikut dibuang.
- Migrasi embedding: hapus selama migrasi diterapkan juga ke koleksi sumber, potongan yang
  sudah ditulis ulang tidak ditimpa isi lama, migrasi gagal dilanjutkan (dulu dianggap selesai),
  koleksi target lama yang basi dibangun ulang, scroll 512 per halaman (dulu 16 = kuadratik).
- Bom zip (docx/xlsx/pptx/odt) ditolak sebelum dibuka; antrean pengindeksan dibatasi
  (`INDEXING_MAX_QUEUED`, `INDEXING_MAX_QUEUED_MB`) - lewat batas dijawab 429.
- Setelan ngawur (model embedding kosong, angka sampah) ditolak 422, bukan disimpan.

**Jawaban**

- Kueri tanpa kata bermakna ("Apa itu?") tidak lagi mendapat skor palsu 1,0 dan lolos gerbang.
- Jawaban sebagian ("12 hari [1]. Info cuti besar tidak ditemukan.") tidak lagi dibuang
  sebagai penolakan; sitasi jawaban hasil perbaikan ikut dibersihkan; alamat situs tidak lagi
  dianggap "kata rusak" (panggilan LLM sia-sia).
- Fokus dokumen hanya di jalur jawaban: `/search` dan dokumen yang dipilih eksplisit tidak dipangkas.

**Skala (KB 30 ribu potongan)**

| | Sebelum | Sesudah |
|---|---|---|
| Pertanyaan pertama setelah restart/deploy | 13,7 dtk | 0,02 dtk (indeks dipanaskan di latar belakang) |
| Simpan indeks kata kunci per dokumen baru | 0,58 dtk (tulis ulang 50 MB) | 0,001 dtk (jurnal) |
| Daftar 500 dokumen | 500 pemindaian Qdrant | 1 pemindaian |

**Batas yang tersisa: Qdrant tertanam.** Pencarian vektor pada 30 ribu potongan = 0,6 dtk dan
dikunci satu per satu (Qdrant sendiri menyarankan server di atas 20 ribu titik). Production saat
ini 1.683 potongan (~0,03 dtk). Sebelum mencapai ribuan dokumen, jalankan Qdrant sebagai layanan
terpisah dan set `QDRANT_URL`. Setel juga `stop_grace_period` layanan ke >= 60 dtk agar job yang
sedang berjalan selesai saat redeploy.
