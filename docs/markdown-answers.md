# Jawaban berformat Markdown

Model diminta menjawab dalam **Markdown**, dan konsol merendernya. Yang diminta bukan berkas
`.md` untuk diunduh, melainkan jawaban yang tampil rapi: judul, daftar, tabel, dan penekanan.

## Kenapa diatur di dua sisi

Hanya mengubah salah satu sisi tidak cukup:

- Kalau **hanya prompt** yang diubah, konsol tetap mencetak teks apa adanya - pemakai melihat
  `## Ringkasan` dan `| kolom | tipe |` sebagai karakter mentah.
- Kalau **hanya tampilan** yang diubah, model tidak tahu bentuk keluaran yang diinginkan dan
  Markdown-nya datang acak (kadang tanpa struktur sama sekali).

Karena itu keduanya diubah dan dikunci uji.

## Sisi model (`app/rag/generator.py`)

`SYSTEM_PROMPT` memuat aturan bentuk keluaran:

- Jawab dalam Markdown (GitHub-flavoured).
- `##`/`###` untuk bagian, `-` untuk daftar, tabel Markdown untuk data tabular.
- `**tebal**` untuk angka/kata kunci; `` `code` `` untuk nama kolom, tabel, dan nilai literal.
- **Jangan** mengeluarkan berkas, lampiran, atau tautan unduhan.
- Jangan membungkus seluruh jawaban dalam pagar kode, dan jangan mengulang pertanyaan.

`SUMMARY_PROMPT_ID` dan `SUMMARY_PROMPT_EN` mendapat aturan yang sama supaya ringkasan dokumen
(knowledge turunan) tampil dengan bentuk yang konsisten.

Aturan isi yang lama **tidak berubah**: hanya dari konteks, tidak mengarang angka/nama/kebijakan,
menyebut sitasi `[1]`, dan menjawab dalam bahasa pertanyaan.

## Sisi tampilan (`app/ui/markdown.js`)

Renderer kecil tanpa build step, tanpa CDN, tanpa dependensi - sejalan dengan aturan konsol.
Yang didukung: judul, tebal/miring, kode inline & blok berpagar, tautan, daftar berurut/tak
berurut (termasuk bersarang), kutipan, garis pemisah, dan tabel gaya GitHub. Konstruk di luar itu
dibiarkan sebagai teks biasa, bukan dirusak.

### Jawaban diperlakukan sebagai masukan berbahaya

Jawaban datang dari model yang membaca dokumen yang tidak dipercaya, jadi:

1. Teks di-escape **lebih dulu** (`&`, `<`, `>`, `"`, `'`), baru sejumlah kecil konstruk Markdown
   dikenali. Tidak ada HTML mentah dari jawaban yang pernah dijalankan.
2. Tautan disaring: hanya `http`, `https`, `mailto`, dan tautan relatif yang hidup; `javascript:`
   dan `data:` ditolak (tetap tampil sebagai teks).
3. Atribut event (`onerror`, `onmouseover`, ...) tidak pernah lahir dari renderer.

Hasilnya: `<script>alert(1)</script>` di dalam jawaban tampil sebagai tulisan, bukan dijalankan.

## Tombol "Salin .md"

Di bawah jawaban ada tombol **Salin .md** yang menyalin jawaban sebagai **teks Markdown** (bukan
mengunduh berkas). Memakai Clipboard API bila tersedia (konteks aman: https/localhost), dengan
cadangan `textarea` + `execCommand` untuk akses http biasa supaya tombolnya tetap bekerja.

## Verifikasi

```bash
# uji renderer (Node, tanpa dependensi) - 11 kasus termasuk 3 kasus XSS
node scripts/test_markdown.mjs

# uji integrasi: prompt, pemuatan aset, pemakaian di konsol, + uji renderer di atas
.venv/Scripts/python.exe -m pytest tests/integration/test_markdown_answers.py -q
```

Uji integrasi ikut gagal bila jawaban dikembalikan ke `textContent` mentah - jadi bug "jawaban
Markdown tampil sebagai teks mentah" tidak bisa kembali tanpa ketahuan.

## Bug yang ditemukan saat mengerjakan ini: shutdown worker tidak menunggu ringkasan

Suite uji sempat **mati total** (`Windows fatal exception: access violation` di
`qdrant_client/local/persistence.py`) di tengah jalan, bukan gagal dengan rapi. Penyebabnya:
`IndexingWorker.stop()` memakai `executor.shutdown(wait=False)`, jadi thread ringkasan yang
sedang menulis ke Qdrant **masih berjalan** ketika pemanggil menutup client Qdrant. Menulis ke
client yang sudah ditutup bukan galat Python - prosesnya langsung mati.

Perbaikannya: `stop()` kini membatalkan pekerjaan yang masih menunggu di antrian, lalu
**menunggu** (`wait=True`) pekerjaan yang sedang berjalan benar-benar selesai sebelum kembali.
Dikunci uji `test_stopping_the_worker_waits_for_a_running_summary` (dibuktikan menangkap bug:
dengan `wait=False` dikembalikan, ujinya gagal "stop() tidak menunggu ringkasan selesai").
