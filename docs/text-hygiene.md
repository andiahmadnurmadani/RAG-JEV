# Teks bersih: aksara asing dan sampah biner

Dua keluhan nyata yang dijawab dokumen ini:

1. **Aksara China muncul di jawaban.** Model menyelipkan aksara Han ke tengah kalimat
   Indonesia (`Dokumen final perakitan完整的 memuat wiring`). Faktanya benar, tapi pemakai
   melihat aksara yang tidak bisa dibaca.
2. **Sampah biner masuk jadi knowledge.** Sebuah PDF di web disajikan tanpa `content-type`
   yang benar, sehingga byte mentahnya (`%PDF-1.4`, `endstream`) masuk ke Qdrant sebagai
   "knowledge" — potongan yang tidak bisa dicari dan mencemari jawaban model.

## Temuan: bukan satu masalah, tapi tiga

Memindai **2.840 potongan** di produksi (`scripts/scan_cjk_sqlite.py`) menemukan 81 potongan
beraksara CJK — dan setelah diklasifikasi (`scripts/classify_cjk_chunks.py`), ternyata
**hanya dua dari tiga sumber yang cacat**:

| Sumber | Jumlah | Sifat | Tindakan |
|---|---|---|---|
| Byte biner PDF gagal parse | 37 | cacat | **Ditolak** saat parsing |
| Aksara Han **diselipkan model** | 29 | cacat | **Dibuang** dari jawaban & ringkasan |
| Dokumen **memang** beraksara Han | 15 | **fakta** | **Dipertahankan** |

Yang ketiga itu penting: label skema seperti `地` (shield) adalah isi dokumen. **Filter yang
membuang semua aksara asing akan merusak data yang sah.** Karena itu aturannya:

> Aksara asing dibuang **hanya bila ia tidak ada di sumbernya.**

Perbandingan dengan konteks itulah yang membedakan selipan model dari kutipan dokumen.

## 1. Sampah biner: akar di `content-type` yang salah

`FetchedPage.sniffed_type` kini menilai tipe dari **isi berkas** (magic bytes), bukan dari
header yang bisa menyesatkan:

```python
if head[:4] == b"%PDF":   return "application/pdf"
if head[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
```

Isi yang ternyata biner **ditolak** sebagai `WebFetchError`, bukan disimpan sebagai teks.

Ambangnya dijaga agar tidak salah tuduh: dokumen yang memang **membahas** format PDF menyebut
`%PDF`/`endobj` secara sah, jadi yang ditandai hanya jejak biner **berulang** (≥4 penanda) atau
karakter rusak yang menumpuk.

## 2. Aksara asing dari model

Tiga lapis, dari yang paling murah:

| Lapis | Yang dilakukan |
|---|---|
| Prompt | Jawaban + ringkasan (ID & EN) melarang aksara non-Latin secara eksplisit |
| Penjaga jawaban | `foreign_tokens()` membandingkan jawaban dengan konteks; yang tidak ada di sumber dibuang |
| Ringkasan | Dibersihkan **sebelum disimpan ke Qdrant** — ringkasan jadi knowledge turunan yang ikut terbawa ke jawaban berikutnya |

### Bug nyata yang ditemukan saat menguji

Uji simulasi (memaksa model mengeluarkan `perakitan完整的`) menemukan bahwa versi pertama
menghapus **kata `perakitan` yang sah**, karena rentang tokennya diperlebar ke huruf Latin yang
menempel. Sekarang hanya aksara asingnya yang dibuang:

```
model : Dokumen final perakitan完整的 memuat wiring dan firmware [1].
hasil : Dokumen final perakitan memuat wiring dan firmware [1].
```

Kata Latin, sitasi `[1]`, dan garis tabel Markdown `|---|---|` semuanya tetap utuh. Dikunci
uji regresi.

## Verifikasi

```bash
# detektor & pembersih (tanpa model)
PYTHONPATH=. .venv/Scripts/python.exe scripts/check_foreign_text_filter.py

# simulasi model yang memaksa keluaran beraksara China (menemukan bug "perakitan")
PYTHONPATH=. .venv/Scripts/python.exe scripts/check_foreign_filter_simulated.py

# uji hidup: dokumen normal bersih, dump biner ditolak
.venv/Scripts/python.exe scripts/verify_foreign_filter_live.py
.venv/Scripts/python.exe scripts/verify_foreign_filter_model_live.py

# uji otomatis
.venv/Scripts/python.exe -m pytest tests/integration/test_foreign_text_filter.py -q
```

Hasil suite: **484 lulus, 1 dilewati**.

## Yang TIDAK diubah

* **Model LLM** — tetap dari Pengaturan; itu keputusan operator.
* **Data lama di Qdrant.** 81 potongan lama masih ada di indeks. Skrip pembersihan disediakan
  (`scripts/scan_cjk_sqlite.py` untuk memindai ulang), tetapi **tidak dijalankan otomatis**:
  menghapus potongan adalah operasi destruktif pada data produksi dan harus disetujui dulu.
  Potongan baru sudah bersih; potongan lama akan tergantikan saat dokumennya diunggah ulang.
* **Dokumen beraksara asing** — sengaja dibiarkan utuh.
