# Verifikasi end-to-end (live)

Semua angka di dokumen ini berasal dari eksekusi nyata terhadap layanan yang berjalan di
`http://127.0.0.1:8099`, bukan dari asumsi atau hasil yang disimulasikan.

Reproduksi:

```bash
EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none JEV_ENABLED=false \\
LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://<gateway>/v1 LLM_MODEL=<model> \\
  .venv/Scripts/python.exe -m app.main      # terminal 1
.venv/Scripts/python.exe scripts/live_verify.py   # terminal 2
```

## Hasil: 19/19 check lulus

Konfigurasi run: `EMBEDDING_PROVIDER=hash`, `RERANKER_PROVIDER=none`, LLM nyata
(`cmc/deepseek/deepseek-v4.1-flash` lewat gateway OpenAI-compatible), Qdrant embedded.
Data uji: satu PDF asli 16 halaman (`Modul_Hosting_Kroombox_v2.2.pdf`) dan satu SOP teks,
keduanya di knowledge base yang sama (`kb_docs`).

| # | Check | Bukti (keluaran nyata) |
|---|---|---|
| 1 | `GET /health` (liveness) | `{"status":"ok"}` |
| 2 | `GET /ready` melaporkan tiap dependensi | `qdrant/embedding/reranker/llm = ok`, `jev = disabled` |
| 3 | Index PDF via `file://` | HTTP 202, status `completed` |
| 4 | PDF ter-chunk benar | `chunks=5`, `pages=16`, `vectors_in_store=5`, `tokens=3176` |
| 5 | Index dokumen kedua di KB yang sama | `completed`, `vectors_in_store=1` |
| 6 | `/ready` menjadi `ready` setelah koleksi terisi | `points=6`, `status=ready` |
| 7 | `/search` menemukan dokumen SOP | `doc_sop_insiden/chunk_0001` skor 1.0 |
| 8 | `/search` menemukan dokumen PDF | `doc_kroombox/chunk_0001..0004` |
| 9 | `/query` dengan LLM nyata menjawab dari SOP | `grounded=True`, `sources=[doc_kroombox, doc_sop_insiden]`, `retrieval_ms=1.64`, `generation_ms=3282.5` |
| 10 | `/query` menjawab dari dokumen lain di KB yang sama | `grounded=True`, sumber memuat `doc_kroombox` |
| 11 | `/extract` mengikuti `output_schema` | `items=[{batas_pelaporan, kanal_resmi}]` + sitasi |
| 12 | Org B tidak bisa mengambil dokumen Org A | `grounded=False`, `sources=[]`, jawaban no-answer kanonik |
| 13 | Field tenant di body ditolak di muka | HTTP 422 `VALIDATION_ERROR` |
| 14 | Metadata dokumen Org A tidak terbaca Org B | HTTP 404 `DOCUMENT_NOT_FOUND` |
| 15 | Opsi tak terdokumentasi ditolak (fail-closed) | HTTP 422 `VALIDATION_ERROR` |
| 16 | `/metrics` melaporkan trafik + indexing + retrieval | `http_requests_total=19`, `documents_indexed=2`, `chunks_created=6`, `no_answer_total=1` |
| 17 | `DELETE` dokumen | HTTP 200 |
| 18 | Dokumen terhapus tidak lagi bisa diambil | `/search` → `[]` |
| 19 | Angka `usage` jujur | `reranked_chunks=0`, `reranker="none"` saat reranker memang tidak dipakai |

Catatan penting pada check 9/19: `reranked_chunks` bernilai 0 dan `reranker` bernilai
`none` karena pada run ini reranker memang dimatikan. Sebelumnya layanan melaporkan
`reranked_chunks=3` untuk reranker no-op — itu sudah diperbaiki; laporan sekarang hanya
menghitung rerank yang benar-benar terjadi.

## Perilaku saat Jev bermasalah (degradasi teruji)

Server kedua dijalankan dengan `JEV_ENABLED=true` dan kredensial jevai.org yang saat ini
ditolak penyedia:

- `GET /ready` → `dependencies.jev = "error"` (dilaporkan apa adanya).
- `POST /query` tetap **200 dan grounded**, dengan `route`:

```json
{
  "capability": "knowledge_query",
  "source": "fallback",
  "confidence": 0.4,
  "reason": "default; jev_error: Jev tool error: The request credentials or model access were rejected.",
  "stripped_tenant_fields": []
}
```

Artinya: keputusan Jev yang gagal tidak pernah diam-diam dianggap sukses, tidak pernah
menggagalkan permintaan pengguna, dan alasannya dapat diaudit dari respons.

## Yang sengaja tidak diklaim

- Model embedding/reranker ONNX (`fastembed`) belum menghasilkan laporan evaluasi karena
  unduhan modelnya macet di jaringan ini; jalur `sentence_transformers` dipakai sebagai
  gantinya (`docs/evaluation.md`).
- Reranker cross-encoder pada run live di atas dimatikan (`RERANKER_PROVIDER=none`), jadi
  check 8/9 membuktikan retrieval hybrid + grounding, bukan kualitas reranking.

## Verifikasi bagian 2 — update/replace, auth, rate limit

`scripts/live_verify_extra.py` (instance yang sama, kecuali rate limit):

| Check | Bukti |
|---|---|
| `POST /knowledge/index` v1 lalu `PUT /knowledge/doc_update` v2 | v1 `completed` (`chunks=1`), v2 `completed` (`chunks=1`) |
| Isi lama benar-benar diganti | pencarian `"tujuh hari kerja"` → tidak ada; `"sepuluh hari kerja"` → ada |
| Tidak ada vektor yatim setelah update | `vectors_in_store=1` = `chunks=1` |
| API key tidak dikenal | HTTP 401 `AUTH_INVALID` |
| Kredensial tidak dikirim | HTTP 401 `AUTH_INVALID` |

Rate limit diuji pada instance terpisah dengan `RATE_LIMIT_PER_MINUTE=5`, karena batas 120/menit
tidak bisa dijangkau skrip verifikasi. Hasil: lima permintaan pertama `404`, permintaan keenam
dibalas

```json
{"success": false, "error": {"code": "RATE_LIMITED",
 "message": "Rate limit of 5 requests/minute exceeded",
 "request_id": "req_...", "details": {"retry_after_seconds": 59}}}
```

Jadi kontrolnya benar-benar mengikat (bukan sekadar terkonfigurasi), dan penolakannya memakai
kode error PRD — bukan 429 generik.

## Verifikasi bagian 3 — jalur dense (vektor) benar-benar mengembalikan hasil

Bug yang tadinya membuat jalur dense tidak pernah menyumbang ke konteks ditemukan saat
menjalankan ablation `--no-hybrid`. Bukti sebelum/sesudah, kueri
`"Berapa kuota cuti tahunan karyawan?"`, `use_hybrid=False`, `EMBEDDING_PROVIDER=sentence_transformers`:

| | dense_hits | fused | candidates | best_score | dokumen |
|---|---|---|---|---|---|
| sebelum perbaikan | 7 | 1 | 0 | 0.0 | — (no_candidates) |
| sesudah perbaikan | 11 | 11 | 5 | 1.0 | `doc_sop_cuti` peringkat 1 |

Penyebabnya: `repository.search_dense()` tidak mengembalikan `document_id` di level atas,
sedangkan kunci fusi adalah `{document_id}::{chunk_id}` → semua hit dense runtuh ke satu kunci.
Di mode hybrid bug ini tidak terlihat karena BM25 memasok kunci yang benar.

Dijaga oleh: `tests/integration/test_answer_quality_and_system.py::test_dense_only_search_returns_every_matching_document`
dan `tests/unit/test_vector_store_guards.py` (dimensi embedding tak diketahui → `EMBEDDING_FAILED`).

## Verifikasi bagian 4 - konsol chat di browser sungguhan

> Konsol versi pertama (satu layar). Kontrol konfigurasi yang disebut di bawah kini berada di
> layar Pengaturan - lihat bagian 5 - sedangkan perilaku kerjanya (upload terpisah, scope dokumen,
> penolakan, sitasi) tidak berubah.

Dijalankan di Chrome terhadap server live `http://127.0.0.1:8099` (kunci `live-key-org-a`, `kb_chat`):

| Aksi di konsol | Hasil yang terlihat |
|---|---|
| buka `/ui/`, isi API key + knowledge base, klik Cek koneksi | badge `ready`, `9 ms /health`, daftar dokumen termuat, setelan menampilkan model LLM aktif |
| tempel teks, nama `SOP Cuti 2026` (tanpa ekstensi) | `selesai: 1 chunk, 59 token`; dokumen muncul di daftar dengan nama manusianya |
| tarik berkas `notulen_operasi.txt` ke dropzone | `selesai: 1 chunk, 19 token`; daftar jadi 2 dokumen (upload terpisah dari kotak prompt) |
| tanya "Berapa kuota cuti tahunan karyawan dan berapa hari sebelum tanggal mulai pengajuan?" | jawaban menyebut 12 hari kerja + 7 hari kerja dengan sitasi `[1] SOP Cuti 2026 hal. 1 - chunk_0001 (1)` dan chip `keputusan: knowledge_query (fallback)`, `model: deepseek/deepseek-v4.1-flash`, `reranker: none`, `retrieval_ms: 33.71`, `generation_ms: 2158.68` |
| centang dokumen lalu tanya "Apa isi SOP Cuti 2026?" | badge scope berubah jadi `1 dokumen dipilih`, jawaban merinci Pasal 1-3 dari dokumen itu saja |
| cari "pemeliharaan rutin" dengan 1 dokumen dicentang | 1 hasil, hanya dari dokumen yang dicentang (scope mengikat) |
| lepas centang, cari ulang | 2 hasil: `notulen_operasi.txt` (skor 1) dan `SOP Cuti 2026` (0.5) |
| pertanyaan di luar korpus ("harga saham ... 2030") | jawaban penolakan, kelas `msg assistant no-answer`, chip `alasan: strict_grounding`, `generation_ms: 0` (LLM tidak dipanggil) |
| saklar "cari tanpa jawaban" | `Retrieval saja (hybrid): N hasil, tanpa LLM` + sitasi |
| setelan Jev: route hint `knowledge_search` | chip `keputusan: knowledge_search (request_hint)` - setelan benar-benar memengaruhi keputusan |

Dijaga oleh `tests/integration/test_test_console.py`: `/ui/` 200 tanpa kredensial, aset terjangkau,
upload terpisah dari composer, setelan Jev ada, `organization_id` tidak pernah dikirim UI,
path API memakai prefiks `/api/v1`, HTML yang dikirim tidak memuat kredensial apa pun, dan CORS
tertutup secara default.

### Dua bug yang hanya muncul saat konsol dipakai

1. **Label dokumen tanpa ekstensi ditolak `UNSUPPORTED_MEDIA_TYPE`.** Nama "SOP Cuti 2026" adalah
   label manusia, tetapi validasi MIME/ekstensi memakainya, sehingga alur "tempel teks" - alur
   utama konsol - gagal. Sekarang payload divalidasi dengan nama yang menjelaskan byte-nya,
   sementara label tetap tidak boleh mengaku tipe yang ditolak (`payload.exe` tetap 415).
2. **Semua tombol konsol menjawab 404.** Path di konsol relatif terhadap `/api/v1` tetapi prefiksnya
   tidak pernah ditambahkan (`/health` alih-alih `/api/v1/health`). Sekarang base url diisi
   `location.origin + "/api/v1"` dan dijaga satu test regresi.

## Verifikasi bagian 5 — layar Pengaturan (model AI + Jev custom) di browser

Konsol sekarang dua layar: halaman utama hanya upload + daftar dokumen + prompt, sedangkan seluruh
konfigurasi pindah ke `#/settings`. Kontrol di bagian 4 yang dulu menempel di halaman chat (API key,
knowledge base, route Jev, `top_k`, `threshold`, `strict_grounding`, `hybrid`, `reranker`) sekarang
ada di layar Pengaturan, dengan perilaku yang sama.

Dijalankan di Chrome terhadap server live `http://127.0.0.1:8099` (`bash scripts/run_live.sh`,
kunci `live-key-org-a`, `kb_chat`, koleksi `live_chunks` 24 titik):

| Aksi di konsol | Hasil yang terlihat |
|---|---|
| buka `/ui/` dengan profil browser kosong | pil status `butuh API key`; layar Pengaturan menolak menampilkan setelan dan menyuruh isi kunci dulu |
| isi API key + klik **Uji koneksi** | pil `siap`; keterangan `Terhubung. Model: cmc/Qwen/Qwen3.6-Plus - Jev: live (systemone)`; petunjuk dropzone ikut berubah jadi `maksimal 8 MB` (dibaca dari `/ready`) |
| klik **Simpan** | daftar `kb_chat` termuat: 3 dokumen dengan nama manusia + id + `2 chunk, 28 token` + waktu |
| buka Pengaturan setelah tersambung | bagan Model AI dan Jev terisi sendiri dari server: `https://9router.yanto.top/v1` / `cmc/Qwen/Qwen3.6-Plus` + `Kunci tersimpan sk-d...15e8`, Jev aktif `systemone` `http://localhost:20128/v1/systemone` `oc/jev-1.13-free` + `Kunci tersimpan sk-c...ab50` |
| klik **Muat daftar model** | `407 model dari https://9router.yanto.top/v1 (6506.77 ms)`; saringan `qwen` menyisakan 48 baris; klik satu baris mengisi field model |
| klik **Uji Jev** | `Jev menjawab dalam 3178.21 ms dengan model oc/jev-1.13-free (noul)` — URL + kunci + model yang sedang tampil benar-benar dipanggil |
| ganti model jadi `cmc/deepseek/deepseek-v4.1-flash`, klik **Simpan model** | `Tersimpan: llm.provider, llm.base_url, llm.model`; `GET /ready` langsung melaporkan `cmc/deepseek/deepseek-v4.1-flash` **tanpa restart**; berkas `data/live/settings.json` hanya memuat 3 field itu (tidak ada kunci) |
| ubah transport Jev ke `mcp` | field endpoint System One hilang; kembali ke `systemone` → muncul lagi |
| tanya "berapa kuota cuti tahunan karyawan?" | jawaban `Kuota cuti tahunan karyawan adalah dua belas hari kerja per tahun [1].` + 4 sitasi (`SOP Cuti 2026` 1.00, `SOP Lembur 2026` 0.98, `notulen_operasi.txt` 0.97, `SOP Lembur 2026` 0.96) + chip `keputusan: knowledge_query (jev)`, `model: Qwen/Qwen3.6-Plus`, `retrieval_ms: 4.4`, `rendering_ms: 8606`, `context_tokens: 194` |
| mode **Cari saja** | `Retrieval saja (hybrid): 4 hasil, tanpa memanggil model.` + `retrieval_ms: 7.19` |
| **Tempel teks** `Catatan Uji UI` | `Catatan Uji UI selesai: 1 chunk, 21 token.` daftar jadi 4 dokumen; hapus → `Dihapus: catatan_uji_ui_qc4ze (1 chunk).` daftar kembali 3 |
| berkas 9 MB lewat pemilih berkas | ditolak **di browser**: `besar_sekali.txt berukuran 9.0 MB, melebihi batas 8.0 MB. Tidak dikirim sama sekali - perkecil berkas dulu.` jumlah dokumen tetap (tidak ada permintaan ke server) |
| berkas `payload.exe` | `Unsupported file type '.exe' (UNSUPPORTED_MEDIA_TYPE)` dari server, tanpa job, jumlah dokumen tetap |

Analisis anti-slop dijalankan pada `app/ui` (`impeccable detect --json`): temuan awal `cramped
padding` pada pil status, `gpt-thin-border-wide-shadow` pada permukaan ber-border, dan
`pulsing-dot` pada indikator menunggu; ketiganya diperbaiki (padding vertikal sungguhan, bayangan
rapat untuk elemen ber-border, indikator statis "Menyusun jawaban") sehingga analisis ulang
melaporkan **0 temuan**.

Dijaga oleh `tests/integration/test_test_console.py` (sekarang 15 test): halaman utama tidak boleh
memuat satu pun field konfigurasi, layar Pengaturan harus memuat semuanya, label tiap input ada,
dan UI memanggil `GET/PUT /settings`, `/settings/llm/models`, `/settings/jev/probe`.

## Verifikasi bagian 6 — format berkas knowledge bisa dipilih & semua teksnya terbaca

Yang diuji: (a) daftar format bisa diubah dari layar Pengaturan dan batas ukuran ikut berubah,
(b) format yang belum didukung tidak pernah ditawarkan sebagai pilihan hidup, (c) berkas
berformat terlarang ditolak di browser sebelum dikirim, (d) **teks 12 format sungguhan terbaca**
sampai bisa ditemukan kembali lewat pencarian.

Alat bukti: `scripts/make_format_samples.py` (membuat contoh dengan pustaka Office asli:
python-docx, openpyxl, python-pptx, odfpy, ebooklib — di venv terpisah supaya venv service tetap
tanpa pustaka tambahan) dan `scripts/verify_format_samples_live.py` (unggah + cari penanda).

Hasil `verify_format_samples_live.py` di `http://127.0.0.1:8099` (kunci `live-key-org-a`,
32 format aktif, batas 25 MB):

| Berkas | Parser | Bagian | Penanda | Cuplikan teks yang ditemukan kembali |
|---|---|---|---|---|
| `kebijakan_cuti.docx` | `docx-xml` | 1 | KUA2026DOCX | `Kebijakan Cuti 2026 Kuota cuti tahunan karyawan tetap adalah 12 hari kerja.` |
| `kuota_cuti.xlsx` | `xlsx-stdlib` | 2 | KUA2026XLSX | `Lembar: Kuota baris 1: A=Nama \| B=Bagian \| C=Kuota Cuti` |
| `rapat_rutin.pptx` | `pptx-stdlib` | 2 | KUA2026NOTES | `Pemeliharaan Rutin Kuota cuti dua belas hari` + `Catatan pembicara: ...` |
| `kebijakan_cuti.odt` | `odf-stdlib` | 1 | KUA2026ODT | `Kebijakan Cuti ... Bagian \| Kuota` |
| `buku_uji.epub` | `epub-stdlib` | 2 | KUA2026EPUB | `Bab 1 Bab Satu. Cuti tahunan dua belas hari kerja.` |
| `sop_cuti.rtf` | `rtf-stdlib` | 1 | KUA2026RTF | `SOP Cuti 2026 / Kuota cuti adalah 12 hari kerja.` (tiga baris, tabel font dibuang) |
| `sop_cuti.md` | `text` | 1 | KUA2026MD | `# SOP Cuti Kuota cuti 12 hari.` |
| `catatan_latin.txt` | `text` | 1 | KUA2026LATIN | `Ringkasan: kuota cuti 12 hari, café buka 08.00.` (berkas latin-1, aksen utuh) |
| `kuota.csv` | `csv-stdlib` | 1 | KUA2026CSV | `baris 2: Andi \| Keuangan \| 12` |
| `kebijakan.yaml` | `text` | 1 | KUA2026YAML | `kebijakan: cuti kode: KUA2026YAML kuota: 12` |
| `sop.html` | `html` | 1 | KUA2026HTML | `SOP Cuti Kode KUA2026HTML. Kuota 12 hari. Bagian \| Kuota \| Keuangan \| 12` |
| `audit.log` | `text` | 1 | KUA2026LOG | `2026-09-30 ERROR gagal simpan KUA2026LOG` |

Penutup skrip: `SEMUA LULUS: 12 berkas terbaca, terindeks, dan teksnya bisa ditemukan kembali.`

Di layar Pengaturan (Chrome, `#/settings`, kunci `live-key-org-a`):

| Yang dilakukan | Bukti |
|---|---|
| Buka Pengaturan + Uji koneksi | pil `siap`; 27 kotak format dalam 5 grup: `DOKUMEN 5/5`, `PRESENTASI 2/2`, `SPREADSHEET 4/4`, `TEKS 12/12`, `GAMBAR 0/0` |
| Lihat format tanpa dukungan | 4 kotak nonaktif dengan alasan: `.doc`/`.ppt` "pustaka textract belum terpasang", `.xls` "pustaka xlrd belum terpasang", `.png .jpg ...` "program tesseract belum terpasang" |
| Matikan semua Spreadsheet + `.epub`, batas 6 MB, Simpan | `Tersimpan: ... uploads.extensions, uploads.max_upload_mb`; grup jadi `SPREADSHEET 0/4` |
| Balik ke halaman utama | ringkasan berubah: `CFG, CONF, DOCX, ... +18 lain - maksimal 6 MB`; `accept` pemilih berkas ikut menyusut |
| Seret `.csv` (format yang baru dimatikan) | `uji_nonaktif.csv berjenis .csv tidak termasuk format yang diizinkan (...)`; jumlah dokumen tetap 0 — berkas tidak pernah dikirim |
| Simpan ulang semua format tersedia + 25 MB | `GET /ready` → `allowed_extensions` 32 entri, `max_upload_mb: 25` |

Dijaga oleh test: `tests/unit/test_document_formats.py` (24 test — katalog, ketersediaan, dan
berkas berstruktur nyata: xlsx dengan atribut XML terbalik, epub dengan `href` sebelum `id` +
`nav` dilewati, RTF dengan `par` menempel ke teks, tabel ODF tanpa pemisah kosong) dan
`tests/integration/test_settings_api.py` (+8 test: katalog di `GET`, pilih format, ekstensi tak
dikenal ditolak sebelum ditulis, hanya-format-tak-didukung ditolak, ekstensi dimatikan → `415`
tanpa job, dinyalakan lagi → jalan, batas ukuran 1 MB → `413` pre-flight, nilai di luar rentang → `422`),
`tests/integration/test_test_console.py` (+3 test: kontrol format tidak bocor ke halaman utama,
terhubung ke `/settings`, format tanpa dukungan tidak pernah jadi saklar hidup).

### Perbaikan yang lahir dari pengujian dengan berkas nyata

- **Urutan atribut XML tidak seragam.** Excel menulis `<Relationship Target=... Id=...>` dan
  ebooklib menulis `<item href=... id=...>`; pembacaan yang bergantung urutan membuat nama sheet
  jatuh ke `sheet1` dan daftar isi epub ikut terbaca sebagai bab. Kini atribut dibaca per tag.
- **RTF yang menempelkan kata kontrol ke teks** (`\parKuota`) kehilangan kata pertama paragraf.
  Kini kata kontrol dikenali satu per satu, dan sisa yang bukan kata kontrol diperlakukan sebagai teks.
- **Berkas non-UTF-8** (latin-1/CP1252) sebelumnya bisa merusak tanda baca; kini didekode bertingkat
  (`utf-8-sig` → `utf-8` → deteksi charset → `utf-8 ignore`).
- **HTML**: judul tidak lagi muncul dua kali dan sel tabel kini terpisah `a | b`, bukan `ab`.
- Satu berkas `.docx`/`.xlsx` benar-benar diuji lewat API, bukan hanya lewat unit test, karena
  struktur keluaran pustaka Office lebih kaya daripada contoh buatan tangan.

### Catatan operasional

- Menutup sesi terminal tidak selalu mematikan uvicorn di Windows: proses lama bisa tetap memegang
  port 8099 sehingga server baru gagal bind dan **permintaan tetap dilayani kode lama** (gejala:
  `/ready` melaporkan koleksi lama). Sebelum menjalankan ulang, pastikan portnya bebas
  (`Get-NetTCPConnection -LocalPort 8099 -State Listen`) dan matikan proses `E:\rag-service` yang
  tersisa.
- `chmod 0600` pada berkas override hanya berlaku di Linux/macOS; di Windows mode POSIX tidak
  mengikat, jadi andalkan izin folder.



## Verifikasi bagian 7 — spreadsheet (xlsx) & pertanyaan agregat tanpa halusinasi

Yang diuji: (a) berkas `.xlsx` sungguhan benar-benar dibaca sampai barisnya, (b) pengguna cukup
mengetik pertanyaan biasa ("produk apa yang paling laku?", "berapa totalnya?") tanpa menyebut
kolom, (c) angka yang keluar **dihitung dari data**, bukan dikarang model, (d) permintaan yang
tidak punya dasar di data **ditolak jujur**.

Alat bukti:

- `data/tmp/xlsx/penjualan_agustus_2026.xlsx` — dibuat dengan `openpyxl` (bukan disiapkan manual):
  lembar `Penjualan`, header `Tanggal, Kode, Produk, Kategori, Jumlah, Harga Satuan, Total`,
  **40 baris**, 7 produk, `random.seed(7)`; ditambah lembar `Ringkasan` (3 baris keterangan).
- Jawaban acuan dihitung terpisah dengan `openpyxl` (`data/tmp/xlsx_truth.py`) — **bukan** dari
  service — supaya pembandingnya independen.
- Instans uji `:8103` (`APP_ENV=xlsxtest`, data terpisah di `data/xlsxtest/`), embedding nyata
  `BAAI/bge-m3`, LLM nyata `oc/mimo-v2.6-flash-free` lewat `http://localhost:20128/v1`,
  `TABLE_STORE_PATH` diarahkan ke `data/xlsxtest/tables.sqlite`.

Perbandingan jawaban service vs hitungan `openpyxl` atas berkas yang sama:

| Pertanyaan (apa adanya dari pengguna) | Jawaban layanan | Hitungan acuan `openpyxl` | Sama? |
|---|---|---|---|
| "Produk apa yang paling laku?" | Ayam Geprek **184**, lalu Air Mineral 141, Nasi Goreng 124, Keripik Pisang 111, Kopi Susu 100 | Ayam Geprek 184, Air Mineral 141, Nasi Goreng 124, Keripik Pisang 111, Kopi Susu 100 | **ya, seluruh peringkat** |
| "Berapa total penjualan seluruhnya dalam rupiah?" | **Rp 14.830.000** (kolom `Total`, 40/40 baris) | 14.830.000 | **ya** |
| "Berapa jumlah unit Ayam Geprek yang terjual?" | **184** unit, "6 baris dihitung dari 40 baris" | 184 (6 baris produk itu) | **ya** |
| "Berapa total kolom diskon?" | tidak dijawab dengan angka; disebutkan kolom yang benar-benar ada (`Tanggal, Kode, Produk, Kategori, Jumlah, Harga Satuan, Total`) dan ditawarkan menghitung `Total` | kolom `Diskon` memang tidak ada | **ya — menolak, bukan mengarang** |

Yang membuat angka itu bisa dipercaya (dan bukan sekadar "kebetulan benar"):

1. **Perencana hanya memilih, tidak menghitung.** LLM dipanggil dengan skema kolom nyata dan
   diminta mengembalikan JSON `{operation, metric, group_by, filters}` saja (`app/tables/analytics.py`).
2. **Rencana divalidasi sebelum dijalankan** — operasi harus salah satu dari `sum/avg/count/
   min/max/top_n`, kolom harus benar-benar ada, kolom agregasi harus numerik, filter kosong
   ditolak. Rencana tidak valid → `PlanError` → jawaban jujur + `no_answer_reason="table_plan_invalid"`.
3. **Angka dihitung kode dari SELURUH baris** (bukan sampel, bukan dari chunk yang lolos
   retrieval). Balasan membawa `computed.rows_matched` / `rows_total` supaya bisa diaudit:
   pada uji di atas selalu **40/40** dengan `rows_skipped=0`.
4. **Model hanya menarasikan** angka yang sudah jadi; prompt-nya melarang menghitung ulang.
   `computed.result` dan `explanation` ditulis kode, jadi bisa dibandingkan langsung dengan
   `openpyxl` — itulah yang dilakukan tabel di atas.
5. **`parse_number` sengaja ketat**: tanggal (`2026-08-02`) dan kode barang (`PRD-001`) tidak
   dianggap angka, supaya kolom seperti `Kode` tidak pernah ikut dijumlahkan.

Bentuk bukti mentah (keluaran probe, `data/tmp/xlsx_live_out2.txt`):

```
unggah: {"document_id": "xlsx_penjualan", "status": "queued", "job_id": "job_ae0b74372063"}
status: completed | chunks: 2
daftar tabel: 2  (lembar "Penjualan" 40 baris, 7 kolom; lembar "Ringkasan" 3 baris)
TANYA: Produk apa yang paling laku?            -> top_n | kolom: Jumlah | group: Produk | baris: 40 / 40
TANYA: Berapa total penjualan seluruhnya...?   -> Rp 14.830.000 (kolom "total", 40 dari 40 baris)
TANYA: Berapa jumlah unit Ayam Geprek?         -> 184 unit (6 baris dihitung dari 40 baris tabel)
TANYA: Berapa total kolom diskon?              -> tidak ada angka; kolom nyata disebutkan
```

Catatan jujur:

- Pertanyaan teks biasa (bukan agregat) tetap lewat jalur retrieval seperti semula; fitur tabel
  hanya **menambah** jalur, tidak menggantinya. Bila perencana gagal, sistem jatuh kembali ke
  jalur teks dan menuliskan alasannya di `table_note` — `/query` tidak pernah mati karena tabel.
- Yang dihitung hanya berkas yang benar-benar terbaca sebagai tabel (`xlsx/xlsm/xls/ods/csv/tsv`).
  Berkas `.xls` lama (biner Excel 97-2003) **sudah** didukung sejak bagian 8 di bawah; sebelumnya
  tidak, dan saat itu dilaporkan sebagai batasan — bukan dikira-kira.

### Bukti tambahan di instans yang sama: CSV sungguhan

`stok_gudang.csv` (7 baris, satu sel `Nilai` sengaja dikosongkan) diunggah ke `:8099` lalu ditanya
dengan bahasa biasa. Acuan dihitung terpisah dengan modul `csv` Python:

| Pertanyaan | Jawaban layanan | Acuan `csv` Python | Sama? |
|---|---|---|---|
| "Barang apa yang stoknya paling banyak?" | Gula 350; Kopi 195; Teh 140 | Gula 350, Kopi 195, Teh 140 | **ya** |
| "Berapa total nilai seluruhnya?" | **10.270.000** dengan keterangan `n = 6` | 10.270.000 dari 6 baris bernilai | **ya** |

Baris ber-`Nilai` kosong tidak dianggap nol dan tidak disembunyikan: jumlah baris yang benar-benar
ikut dihitung dilaporkan (`n = 6`, `rows_matched = 7/7`, `rows_skipped = 0`).

## Verifikasi bagian 8 - format berkas lengkap & tidak ada bagian knowledge yang hilang

Diminta: *"lengkap untuk format berkasnya termasuk doc ppt excel dsb, lalu hati-hati jangan sampai
ketika user upload knowledge ada bagian yang hilang"*. Yang diperiksa di sini: (a) `.doc`/`.ppt`/`.xls`
lama benar-benar terbaca, (b) bagian berkas yang dulu terlewat (header/footer, catatan kaki, komentar,
properti, catatan pembicara, master, header/footer cetak, komentar sel, sel tanggal, kolom yang
terpotong) sekarang ikut terbaca atau alasannya dilaporkan.

### Bahan uji

`scripts/make_legacy_samples.py` membuat berkas biner Word/Excel/PowerPoint 97-2003 sungguhan
(penanda `DOC2026MARK`, `XLS2026MARK`, `PPT2026MARK`), termasuk dua berkas yang menipu:
`catatan_html.doc` (isi HTML bernama .doc) dan `lpj_rtf.doc` (isi RTF bernama .doc).
Sifat bahan uji ini disebut apa adanya: `.xls` ditulis **xlwt** dan dibaca ulang **xlrd** (pustaka
pihak ketiga, hanya di venv uji `data/tmp/olevenv`), sedangkan kontainer `.doc`/`.ppt` dibuat penulis
CFB sendiri lalu isinya mengikuti spesifikasi Word/PowerPoint 97. Pembaca di jalur layanan
(`app/parsing/ole.py`, `doc_binary.py`, `xls_binary.py`, `ppt_binary.py`) tidak memakai pustaka
pihak ketiga sama sekali - ia diverifikasi terhadap acuan itu.

### Hasil potong lintang: pembaca sendiri vs acuan pihak ketiga

| Berkas | Acuan (pihak ketiga, venv uji) | Hasil pembaca layanan | Sama? |
|---|---|---|---|
| `sop_cuti_lama.doc` | olefile: stream `WordDocument` 2303 byte | teks utuh + `DOC2026MARK`, 4 paragraf terpisah | **ya** |
| `sop_cuti_potongan.doc` | olefile: `WordDocument` 2224 byte, `1Table` 4125 byte | kedua potongan (ANSI + UTF-16) tergabung, `é ü ½` utuh | **ya** |
| `laporan_penjualan_lama.xls` | xlrd: 2 lembar, sel tanggal 2026-08-02, "Rp1.500.000" sebagai teks | tabel `Penjualan` 6 baris x 6 kolom, `Tanggal` = tanggal ISO | **ya** |
| `presentasi_lama.ppt` | olefile: stream `PowerPoint Document` 283 byte | 2 slide, seluruh atom teks terbaca | **ya** |

### Janji "semua datanya terambil" sebagai tes otomatis

`tests/unit/test_legacy_formats.py` (19 tes) dan `tests/unit/test_no_data_loss.py` (27 tes) menaruh data
justru di tempat yang dulu terlewat, lalu menuntut datanya muncul di teks knowledge atau di baris tabel
(total suite kini **263 test**, 0 gagal):

| Bagian berkas | Dulu | Sekarang |
|---|---|---|
| docx: header, footer, catatan kaki, komentar, properti dokumen | hanya `word/document.xml` yang dibaca | ikut dibaca, masing-masing diberi label bagian |
| docx: sel tabel | "NamaHarga" menyatu tanpa pemisah | `Nama \| Harga` (pemisah sel menang atas pemisah paragraf) |
| docx: kode field (`PAGE \* MERGEFORMAT`) | ikut jadi teks | dibuang, isi field tetap diambil |
| xlsx: sel bertipe tanggal | serial `46075` dianggap angka biasa | tanggal ISO (`2026-02-22`) di teks **dan** di tabel |
| xlsx: header/footer cetak, komentar sel, teks objek/gambar | tidak dibaca | ikut dibaca per lembar (lewat rels lembar, target relatif diselesaikan `posixpath.normpath`) |
| ods: header/footer di `styles.xml`, sel angka/tanggal tanpa teks, sel menutup diri sendiri | `content.xml` saja, sel tanpa teks dianggap kosong | header/footer + nilai atribut ikut terbaca |
| pptx: catatan pembicara, master, tata letak | slide saja | ketiganya dibaca, master/tata letak di halaman terpisah |
| eml: isi surat + lampiran | format tidak ada | `.eml` didukung (isi + nama lampiran) |
| tabel: baris judul lembar | lebar tabel dikunci baris judul → kolom B..F hilang | lebar dari **seluruh** baris; judul dicatat di `notes` |
| tabel: baris terpotong / kolom kosong | senyap | dicatat di `notes` (`hanya N baris dari M`, `kolom tanpa isi: ...`) |

### Uji live di `:8099` (bukan hanya unit test)

Enam berkas diunggah lewat API sungguhan (`scripts/verify_legacy_formats_live.py`): `sop_cuti_lama.doc`,
`presentasi_lama.ppt`, `laporan_penjualan_lama.xls`, `catatan_html.doc`, `lpj_rtf.doc`, dan berkas nyata
`Data_Penjualan_100_Data.xlsx` (100 baris x 25 kolom). Semua berstatus `completed`.

| Pertanyaan | Jawaban layanan | Dokumen |
|---|---|---|
| "Apa isi SOP cuti tahunan dan berapa kuota cutinya?" | kuota 12 hari kerja, pengajuan lewat sistem HR, hangus 31 Desember, "café tetap buka 08.00-17.00", disahkan Divisi SDM | `sop_cuti_lama.doc` |
| "Apa target kuartal pada presentasi tahunan?" | "Naikkan penjualan 20 persen, fokus produk Ayam Geprek" | `presentasi_lama.ppt` |
| "Berapa total penjualan pada lembar Penjualan berkas xls lama?" | **2.911.000** (`n = 5`, 6 dari 6 baris) dihitung kode, `computed: true` | `laporan_penjualan_lama.xls` |
| "Apa isi catatan yang sebenarnya berkas HTML?" | "Catatan DOC2026MARK - Berkas ini HTML walau bernama .doc" | `catatan_html.doc` |
| "Apa isi laporan yang sebenarnya berkas RTF?" | "Laporan disimpan sebagai RTF walau berekstensi .doc" | `lpj_rtf.doc` |

Dua berkas terakhir penting: isinya **tidak** cocok dengan ekstensinya, dan tetap terbaca karena rute
parser ditentukan dari isi berkas (kontainer OLE → tilik stream; awalan `{\rtf`; awalan `<html`).

### Pemotongan baris: dibuktikan live, bukan hanya di unit test

Ditemukan kelemahan saat pengujian ini: berkas berisi **tepat** di batas (`MAX_TABLE_ROWS = 200.000`)
tampak "utuh" padahal sisanya tidak dibaca - pembaca CSV/ODS berhenti di batas, dan `_build_table`
tidak melihat baris berlebih sehingga `truncated` tetap `false`. Perbaikannya: pembaca yang berhenti di
batas menandai `more_rows`, dan `_build_table` memakai penanda itu (plus catatan tertulis).

Bukti live: `besar_200k.csv` (200.005 baris data, 3,4 MB) diunggah ke `:8099`:

| Yang diperiksa | Hasil |
|---|---|
| `GET /tables` | `row_count: 200000`, **`truncated: true`**, `notes: ["berkas memuat lebih dari 200000 baris; hanya 200000 baris pertama yang disimpan"]` |
| `/query` "Berapa total Harga pada berkas besar_200k.csv?" | "Harga total = **200.000.000** (n = 200000). **Catatan tabel: tabel ini dipotong: hanya 200000 baris tersimpan, jadi angkanya dihitung dari baris itu saja; berkas memuat lebih dari 200000 baris; hanya 200000 baris pertama yang disimpan.**" |
| `DELETE /knowledge/...` | `deleted_tables: 1` - tabel ikut terhapus bersama dokumennya |

Angka 200.000.000 = 200.000 baris x 1.000 (isinya memang begitu) dan cakupannya disebut di jawaban,
bukan disembunyikan.

### Tabel raksasa: satu chunk jadi 2.740 chunk (dan barisnya bisa dicari)

Kelemahan lain yang muncul saat pengujian ini: tabel besar menjadi **satu chunk raksasa**, karena
pemisah chunk memakai batas kalimat (`.`/`!`/`?`) yang tidak ada di dalam baris tabel - dan deteksi baris
tabel hanya mengenali bentuk `|a|b|` atau tab, bukan bentuk `a | b` yang dihasilkan parser CSV/XLSX kita
sendiri. Akibatnya: satu CSV 200.000 baris menjadi satu chunk 6 MB yang praktis tidak bisa ditemukan
lewat pencarian teks.

Perbaikannya: pola baris tabel mengenali `a | b` juga, dan tabel yang melebihi anggaran dipotong **per
baris** dengan **baris kepala diulang** di setiap potongan (setelah tumpang tindih, baris kepala tetap
di baris pertama).

Bukti live (`besar_200k.csv`, 200.005 baris):

| Yang diperiksa | Sebelum | Sesudah |
|---|---|---|
| jumlah chunk | 1 | **2.740** |
| pencarian `baris 139568` | tidak bisa (1 chunk tak terbelah) | ketemu, skor 1.0, chunk diawali `baris 1: Barang \| Qty \| Harga` lalu 74 baris data |
| hapus dokumen | - | `deleted_chunks: 2740`, `deleted_tables: 1` |

### Penghitung vektor yang tidak boleh menjatuhkan daftar dokumen

Saat verifikasi ini, `GET /knowledge` pernah menjawab **500** karena Qdrant lokal (embedded) gagal
menghitung ulang setelah penghapusan (`operands could not be broadcast together with shapes (538,) (539,)`).
Kolom pelengkap itu kini dilindungi: kegagalan dihitung sebagai "tidak diketahui" (`vectors_in_store: -1`)
dan dicatat di log, daftar dokumen tetap 200. Bukti sesudah perbaikan: `GET /knowledge` 200 dengan
`chunks: 2740`, `vectors_in_store: 2740`.

### Catatan tabel terlihat di API (sesudah migrasi kolom `notes`)

| knowledge base | dokumen | lembar | kolom | notes |
|---|---|---|---|---|
| `kb_chat` (indeks lama, sebelum perbaikan) | `Data_Penjualan_100_Data.xlsx` | Ringkasan | **1** `['RINGKASAN DATA PENJUALAN']` | `[]` |
| `kb_nyata` (diunggah setelah perbaikan) | `Data_Penjualan_100_Data.xlsx` | Ringkasan | **2** `['Total Transaksi', 'Bulan']` | `['judul lembar: RINGKASAN DATA PENJUALAN', 'kolom tanpa isi: Penjualan, Laba Kotor']` |

Dua baris di atas adalah perbandingan langsung berkas yang sama sebelum dan sesudah perbaikan - bukan
dua berkas berbeda.

### Angka yang dipakai sebagai patokan

- `.xls` uji: 540.000 + 625.000 + 1.050.000 + 336.000 + 360.000 = **2.911.000** (5 baris bernilai;
  baris catatan ber-`Rp1.500.000` tetap teks, tidak ikut dijumlah).
- `.xlsx` nyata (100 baris x 25 kolom): `Total Penjualan` **1.240.781.865**, `Qty` **481**,
  `Laba Kotor` **222.539.570,34** - identik dengan acuan `openpyxl`, dan kolom `Tanggal` kini
  tanggal ISO (sebelumnya serial `46075`).

### Perintah mengulang

```bash
bash scripts/make_legacy_samples.py            # (dari venv uji) buat berkas .doc/.xls/.ppt
.venv/Scripts/python.exe -m pytest tests/unit/test_legacy_formats.py tests/unit/test_no_data_loss.py -q
bash scripts/run_live.sh                       # layanan di :8099
.venv/Scripts/python.exe scripts/verify_legacy_formats_live.py
```
---

## 9. Dokumentasi, Swagger, dan situs panduan

Tiga permukaan dokumentasi disajikan oleh aplikasi yang sama, dan ketiganya diuji hidup
(instans terpisah di port 8100, `DOCS_SITE_DIR=site`, penyimpanan terpisah, `EMBEDDING_PROVIDER=hash`):

| Permukaan | Alamat | Hasil |
|---|---|---|
| Situs dokumentasi MkDocs Material | `/guide/` | 200 — judul `RAG Service — Knowledge API multi-tenant` |
| Halaman integrasi | `/guide/integration/` | 200 — judul `Integrasi klien` |
| Referensi API (hasil generate) | `/guide/api-reference/` | 200 — 16 operasi dari `openapi.json` |
| Halaman deploy | `/guide/deployment/` | 200 |
| Spesifikasi di dalam situs | `/guide/openapi.json` | 200 — memuat `paths` |
| Indeks pencarian situs | `/guide/search/search_index.json` | 200 — pencarian klien berfungsi |
| Swagger UI | `/docs` | 200 — judul `... - Swagger UI` |
| ReDoc | `/redoc` | 200 |
| Spesifikasi dari aplikasi | `/openapi.json` | 200 — 13 path, 11 skema, OpenAPI 3.1.0 |
| Konsol chat | `/ui/` | 200 — judul `RAG Chat` |
| Rute API tetap utuh | `/api/v1/health`, `/ready`, `/metrics` | 200 |
| Redirect akar | `/` | 307 → `/ui/` |
| Tidak bocor | `/.env`, `/.git/config`, `/guide/../.env` | 404 |

Perintah mengulang:

```bash
bash scripts/build_docs.sh          # ekspor OpenAPI -> generate referensi -> mkdocs build --strict
bash scripts/check_docs_serve.sh    # periksa penyajian (port 8100, data terpisah, keluar != 0 bila gagal)
```

Catatan kejujuran: `mkdocs build --strict` lulus tanpa peringatan; salinan `site/` **tidak** dikomit
(`.gitignore`) sehingga harus dibangun di server. Yang belum diuji di sini: penyajian oleh nginx
(tidak ada nginx di mesin ini) dan `git push` situs ke hosting statis.

---

## 10. Gerbang kode akses: diuji di browser, bukan hanya di unit test

Keluhan yang melahirkan fitur ini adalah keluhan layar: menekan **Buat kunci** di konsol gagal
dengan `AUTH_INVALID` karena konsol di peramban tidak punya API key untuk dikirim. Karena itu
bukti yang dikumpulkan bukan hanya hasil `pytest`, melainkan percakapan peramban sungguhan
(Chrome lewat CDP) dengan server hidup di `127.0.0.1:8099`:

| Langkah | Hasil yang terlihat |
|---|---|
| Buka `/ui/` saat kode akses aktif | gerbang tampil (`display: flex`), panel konsol tersembunyi, isian tunggal + *Ingat saya* (`7 hari`) |
| Kirim kode salah | `Kode akses salah Sisa percobaan: 7. (AUTH_INVALID)`; gerbang tetap menutup layar |
| Kirim kode benar | gerbang hilang, konsol terbuka, status `Terhubung. Model: …`, tombol Keluar muncul |
| Panel **Akses & Sesi** | status kode (`Kode aktif (potongan uji******26), diubah …`), `2 / 200` sesi aktif, tombol *Keluarkan* per sesi |
| **Buat kunci** tanpa menempel API key | kunci baru dibuat dari sesi (`Buat kunci` → `Kunci untuk default dibuat`) — keluhan aslinya berhenti di sini |
| Muat ulang halaman | konsol langsung terbuka (sesi *Ingat saya* masih berlaku), tanpa gerbang |
| Tombol Keluar | gerbang kembali dengan pesan *Anda sudah keluar…*, `localStorage` sesi kosong |
| Konsol galat peramban | **nol** `error`/`unhandledrejection` selama seluruh perjalanan |

Yang diuji otomatis (`pytest`) menyertai perilaku itu — `tests/integration/test_access_gate.py`
(23 test) dan `tests/integration/test_bootstrap_admin.py` (8 test):

- kode hanya tersimpan sebagai digest (`data/access.json` tidak memuat kode apa adanya);
- token sesi hanya sebagai hash, dan tidak pernah muncul di respons mana pun;
- `generation` (digest kode) tidak pernah dikirim ke klien — nilainya cukup untuk menebak kode;
- sesi diterima di header yang sama dengan API key; `remember` menambah masa berlaku 12 jam → 7 hari;
- mengganti kode mematikan semua sesi lama (termasuk sesi pemanggil); mencabut sesi dicatat, bukan dihapus;
- hanya izin `admin` boleh membaca/mengubah kode; kunci yang sedang dipakai tidak bisa mencabut dirinya;
- kunci bootstrap hanya terbit bila belum ada kunci `admin`/`*`, ditulis mode `0600`, tidak masuk log,
  dan tidak diterbitkan ulang setelah dicabut.

Satu bug nyata yang tertangkap justru dari uji ini: `status()` semula memotong digest `generation`
menjadi 12 karakter untuk ditampilkan, sementara sesi menyimpan digest penuh — akibatnya **semua
sesi hidup dianggap "kode sudah diganti"** (`active_sessions: 0`) dan tombol *Keluarkan* hilang.
Perbaikannya: `generation` penuh untuk perbandingan internal, `public_status()` untuk respons HTTP.

Perintah mengulang:

```bash
.venv/bin/python -m pytest tests/integration/test_access_gate.py tests/integration/test_bootstrap_admin.py -q
```

## 11. Dokumen besar: diukur pada berkas asli, bukan diklaim

Keluhan yang melahirkan perubahan ini: `Struktur Lengkap Database KMS Telin (2).pdf` (20 halaman)
sudah diunggah, tetapi jawabannya berbunyi *"tidak lengkap — chunk terpotong … info tidak ditemukan
di konteks"*. Jadi buktinya pun harus memakai berkas itu, dari ujung ke ujung.

Angka hasil pengukuran (parser + pemotong + konteks produksi):

| Ukuran | Sebelum | Sesudah |
|---|---|---|
| Pembaca PDF | `pypdf` | `PyMuPDF` (urutan tata letak, tabel jadi baris `\| a \| b \|`) |
| Halaman terbaca | 20 | 20 (tidak ada halaman kosong) |
| Baris sumber hilang | ada baris tabel terbelah | **0 dari 959** |
| Potongan dokumen | ~20 potongan raksasa, sebagian terbelah di tengah baris | 21 potongan, semuanya berakhir di batas baris |
| Potongan sampai ke model | 5 (dan maks. 3 per dokumen) | **21 (+13 potongan pelengkap)** |
| Token konteks | ~4–6k | 19.038 (anggaran 24.000) |
| Kelengkapan yang dilaporkan | tidak ada laporan | `{included: 21, total: 21, complete: true, ordered: true}` |

Uji otomatisnya ada di repositori dan bisa diulang:

```bash
.venv/Scripts/python.exe -m pytest tests/integration/test_real_kms_pdf.py -q -s   # berkas asli
.venv/Scripts/python.exe -m pytest tests/integration/test_large_document_context.py tests/unit/test_pdf_and_large_tables.py -q
```

`test_real_kms_pdf.py` mengulang jalur yang sama dengan pengguna (unggah → tanya) dan dilewati
kalau berkas PDF-nya tidak ada di mesin ini. `test_large_document_context.py` mengunci perilaku
konteks: dokumen 40 bagian harus masuk konteks **seluruhnya** saat `top_k` kecil, dilaporkan
sebagian saat anggarannya memang kecil, tetap di dalam batas tenant, dan berhenti menambah
potongan ketika saklar pelengkap dimatikan.

### 11.1 Uji hidup dengan model nyata (sebab kedua ketahuan di sini)

Pengukuran di atas memakai parser + pemotong + konteks, bukan model. Setelah dijalankan
end-to-end lewat layanan (`scripts/verify_large_document_live.py`, berkas asli diunggah lalu
ditanya dua kali dengan LLM sungguhan), ketahuan satu sebab yang **tidak** terlihat dari angka
konteks: konteksnya sudah utuh, tetapi jawabannya kosong.

| Temuan | Bukti | Perbaikan |
|---|---|---|
| `LLM_MAX_TOKENS=1024` memotong jawaban panjang; `finish_reason=length` | jawaban kosong padahal 21 potongan (19.038 token) masuk konteks | panel **Model AI → Batas token jawaban** |
| `4096` pun belum cukup untuk daftar seluruh tabel | `output_tokens=5941` dituntut pertanyaan pertama; dengan 4096 jawabannya berhenti (`length`) | bawaan **`LLM_MAX_TOKENS=8192`** |
| Jawaban kosong dilaporkan sebagai *"tidak ditemukan"* | pemakai menyimpulkan datanya hilang | alasan baru `answer_truncated`, potongan jawaban yang sempat terbentuk tetap ditampilkan |
| Sebab tidak terlihat dari luar | hanya kelihatan di log | `usage.finish_reason` ikut di respons API |

Angka pengukuran keluaran pada berkas asli (pertanyaan 1: struktur lengkap + daftar kolom;
pertanyaan 2: tabel knowledge/helpdesk):

| `LLM_MAX_TOKENS` | `output_tokens` | `finish_reason` | Hasil |
|---|---|---|---|
| 1024 | 1024 | `length` | jawaban kosong, dilaporkan "tidak ditemukan" |
| 4096 | 4096 | `length` | jawaban terpotong di tengah daftar tabel → `answer_truncated` |
| **8192** | 5.941 (tanya 1) / 1.923 (tanya 2) | **`stop`** | jawaban utuh, `no_answer_reason=None` |

Sesudah perbaikan, dua pertanyaan yang sama dijawab utuh dari berkas asli: pertanyaan daftar
tabel menyebut seluruh tabel yang ada di dokumen (termasuk kolom-kolom tabel `knowledge`), dan
pertanyaan kelengkapan menjawab `{included: 21, total: 21, complete: true, ordered: true}` —
tanpa satu pun kalimat "informasi tidak ditemukan".

Uji yang mengunci perilaku ini: `tests/integration/test_truncated_answer.py` (jawaban terpotong
harus dilaporkan `answer_truncated` + `finish_reason=length`, potongan jawabannya tetap tampil,
dan jawaban wajar `finish_reason=stop` tidak boleh ditandai terpotong).

## 12. Knowledge dari web: diukur dengan situs nyata, bukan klaim

Fitur ini membuat server mengeluarkan permintaan HTTP, jadi buktinya harus berupa pengambilan
nyata. Situs uji disajikan server HTTP lokal (4 halaman + robots.txt) supaya bisa diulang, lalu
dijalankan lewat layanan hidup dengan model sungguhan (`scripts/verify_web_knowledge_live.py`).

| Yang diuji | Hasil |
|---|---|
| satu halaman (`web_max_pages=1`) | `completed`, 1 halaman, 1 chunk, `source_url` = halaman itu |
| crawl kedalaman 2 | `completed`, **4 halaman** (beranda, /panduan, /lain, /dalam), 4 chunk |
| sitasi per halaman | pertanyaan tentang /panduan → sumber `/panduan`; /dalam → `/dalam`; /lain → `/lain` |
| halaman terlarang robots.txt | tidak terindeks (0 hasil dari `/internal/`) |
| jawaban berisi isi halaman | ya, dengan sitasi `[1]` ke halaman yang benar |
| berkas publik (RFC 2606 di internet) | `completed`, `source_url` = URL berkasnya |

Angka keluaran pada uji hidup (model nyata):

```
TANYA : Apa kode pada formulir lampiran?
angka : konteks=5 token=173 keluar=406 finish='stop'
sumber: ['http://127.0.0.1:63426/sop/cuti/lampiran', ...]
jawab : Berdasarkan dokumen yang tersedia, formulir lampiran memakai kode **lampirancuti** [1].
```

Uji otomatis yang mengunci perilaku ini:

```bash
.venv/Scripts/python.exe -m pytest tests/unit/test_urlguard.py -q                  # 28 uji pengaman URL
.venv/Scripts/python.exe -m pytest tests/integration/test_web_knowledge.py -q      # 12 uji web
.venv/Scripts/python.exe -m pytest tests/integration/test_web_settings.py -q       # 5 uji setelan
```

Dua bug nyata ketahuan justru oleh uji ini, bukan oleh pemeriksaan mata:

1. **Potongan menggabung dua halaman web.** Halaman kecil digabung oleh `min_chunk_tokens` dan
   tumpang tindih, sehingga `source_url` hanya menyimpan satu alamat - sitasi jadi menunjuk
   halaman yang salah. Perbaikan: batas halaman menutup potongan, dan tumpang tindih dilewati
   antar halaman berbeda.
2. **`source_url` hilang di hampir semua potongan.** `_apply_overlap` membangun ulang objek
   `Chunk` tanpa membawa `source_url`/`title`, jadi hanya potongan pertama yang punya alamat.
   Perbaikan: kedua field itu ikut disalin.

Keduanya hanya muncul saat dokumennya punya beberapa halaman ber-alamat berbeda - yaitu tepat
kasus yang fitur ini ada untuk melayani.
