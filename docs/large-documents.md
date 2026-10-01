# Dokumen besar: supaya tidak ada isi yang hilang

Dokumen ini menjawab satu keluhan nyata: sebuah PDF 20 halaman ("Struktur Lengkap Database KMS
Telin") sudah diunggah, tetapi jawabannya berbunyi *"tidak lengkap — chunk terpotong, info tidak
ditemukan di konteks"*. Datanya ada di indeks; yang salah adalah jalur dari indeks ke model.

Ada dua tempat isi dokumen bisa hilang, dan keduanya diperbaiki di rilis ini:

1. **Saat masuk (pengindeksan)** — pembaca berkas salah membaca tata letak, atau pemotong teks
   membelah satu baris tabel menjadi dua sehingga barisnya tidak bisa ditemukan lagi.
2. **Saat keluar (konteks ke model)** — hanya sebagian kecil dokumen yang dikirim, jadi model
   memang tidak punya bahan untuk menjawab "seluruh isinya".

## 1. Tidak ada isi yang hilang saat masuk

| Perbaikan | Sebelum | Sesudah |
| --- | --- | --- |
| Pembaca PDF | `pypdf` saja; kolom bertingkat terbaca acak dan tabel tidak dikenali | `PyMuPDF` lebih dulu (urutan sesuai tata letak + tabel dirender `\| kolom \| tipe \|`), `pypdf` tetap jadi cadangan |
| Pemotongan blok tersusun baris | dipotong menurut kalimat; tabel tanpa titik menjadi satu potongan raksasa **atau** terbelah di tengah baris | blok tabel/dump kolom dipotong **hanya di batas baris** (`_split_lines`); baris raksasa dipotong keras sebagai pilihan terakhir |
| Tabel besar | satu tabel = satu potongan raksasa; potongan berikutnya kehilangan baris kepala | dipotong per baris, baris kepala diulang di setiap potongan (`_split_table`) |
| Perkiraan token | hanya jumlah kata, sehingga teks tanpa spasi (blob base64, deretan angka) dihitung beberapa token | diambil yang terbesar antara hitungan kata dan `panjang / 6` (`estimate_tokens`) |
| Teks tanpa kalimat & tanpa baris | tidak terpotong sama sekali | potong keras menurut anggaran (`_split_hard`) |

Ukuran nyatanya pada PDF kasus: **20 halaman, 50.413 karakter, 21 potongan, 0 dari 959 baris
hilang**, dan tidak ada halaman yang kosong.

## 2. Tidak ada isi yang hilang saat jawab

Pencarian kemiripan selalu mengembalikan *sebagian* dokumen. Untuk pertanyaan seperti "jelaskan
struktur lengkap database ini", sebagian itu tidak cukup — model harus menerima dokumennya utuh.

- `context_expand_documents` (bawaan **aktif**): setiap dokumen yang muncul di hasil pencarian
  diikuti **sampai habis**, dalam urutan dokumen, selama anggaran token masih cukup
  (`list_document_chunks()`, tetap disaring per tenant).
- `context_max_tokens` (bawaan **24000**): anggaran konteks. 24000 dipilih dari pengukuran nyata —
  PDF 20 halaman di atas menjadi 19.038 token, jadi dokumen sekelas itu muat utuh.
- `final_top_k` (bawaan **12**) dan `max_chunks_per_document` (bawaan **8**): batas tahap
  pencarian. Bawaan lama (5 dan 3) membuat model hanya melihat 3 dari 20 bagian.
- Hasil jawaban melaporkan apa adanya: `usage.context_chunks`, `usage.context_expanded_chunks`,
  dan `usage.document_coverage` (`{included, total, complete, ordered}`).
- Di dalam konteks ada blok `<<<DOCUMENT_COVERAGE ...>>>` yang menyatakan kelengkapan setiap
  dokumen. Tanpa itu model menebak, dan tebakan yang salah tampak seperti data terpotong.

Contoh hasil pada PDF kasus:

```
potongan dokumen    : 21
potongan di konteks : 21 (+13 pelengkap)
context_tokens      : 19038
kelengkapan         : {included: 21, total: 21, complete: true, ordered: true}
```

## 3. Menyetel dari panel (tanpa redeploy)

Panel **Ambil → Dokumen besar**:

| Kontrol | Arti |
| --- | --- |
| Anggaran konteks (token) | berapa banyak isi dokumen yang benar-benar dikirim ke model |
| top_k bawaan | jumlah potongan pencarian sebelum dokumen dilengkapi |
| Maks. potongan per dokumen | batas potongan satu dokumen di tahap pencarian |
| lengkapi dokumen sampai utuh | `context_expand_documents` |

Nilainya global (semua tenant) dan butuh izin `admin`. Endpoint: `GET /api/v1/settings`,
`PUT /api/v1/settings` dengan isi `{"retrieval": {...}}`. Batas kewajaran dijaga: anggaran
2000–200000 token, `top_k` 1–50, potongan per dokumen 1–200; nilai di luar itu ditolak **sebelum**
disimpan, bukan setelah semua permintaan gagal.

Lewat lingkungan: `CONTEXT_MAX_TOKENS`, `FINAL_TOP_K`, `MAX_CHUNKS_PER_DOCUMENT`,
`CONTEXT_EXPAND_DOCUMENTS`.

!!! warning "Model berjendela kecil"
    Anggaran konteks yang lebih besar daripada jendela model membuat penyedia menolak permintaan —
    lebih buruk daripada jawaban sebagian. Kalau modelnya hanya 16k token, turunkan anggaran
    (mis. 12000) dan biarkan pelengkap dokumen mengisi sisanya sesuai sisa ruang.

## 4. Batas yang jujur

- Kalau dokumennya benar-benar lebih besar daripada anggaran, konteksnya **tidak** dipotong di
  tengah baris — potongan dari awal dokumen yang masuk, sisanya dilaporkan sebagai
  `complete: false` dengan jumlah bagian yang ikut. Naikkan anggaran kalau memang butuh semuanya.
- Tanpa OCR (tesseract), PDF hasil pindai tetap tidak punya teks untuk diindeks.
- Kolom yang bertumpuk secara visual pada PDF bergantung pada tata letak hasil pembaca; kalau
  berkasnya menyimpan teks dalam urutan yang kacau, tidak ada pembaca yang bisa menebak dengan benar.
