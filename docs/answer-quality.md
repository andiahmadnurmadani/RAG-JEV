# Optimasi kualitas jawaban: teks bersih + reranker yang benar-benar bekerja

Dua keluhan nyata yang dijawab dokumen ini:

1. **Teks jawaban rusak** — muncul kata seperti `pemb.cgiian`, `hanyaLEMpar`,
   `praktikumaccording`. Faktanya benar, tapi pemakai melihat kata aneh.
2. **Reranker tidak pernah bekerja** — hasil pencarian tidak pernah disusun ulang.

Model LLM **tidak** diubah di sini: model dipilih dari layar Pengaturan, dan itu keputusan
operator. Yang diperbaiki adalah kode di sekitarnya.

---

## 1. Teks rusak: prompt + sampling + perbaikan

### Akar masalah

Tiga hal bertemu:

* Model gratis (mis. `oc/space-bunny-free`) sesekali menghasilkan kata terpotong atau bocor ke
  bahasa lain. Ini sifat modelnya, bukan bug kode.
* **`top_p` dan penalty tidak pernah dikirim sama sekali.** Kode hanya mengirim `temperature`
  dan `max_tokens`, sehingga endpoint memakai bawaannya sendiri. `top_p` adalah pengatur utama
  seberapa liar ekor distribusi kata — dan kata aneh datang dari ekor itu.
* Prompt tidak pernah menyebut ejaan sama sekali.

### Perbaikan

| Lapis | Yang dilakukan |
|---|---|
| Prompt | Aturan baru: tulis setiap kata utuh, jangan memotong/menggabung kata, jangan menyisipkan tanda baca di tengah kata, satu bahasa saja, dan pakai ejaan dari konteks bila ragu. |
| Sampling | `top_p` (bawaan **0.9**) dan `frequency_penalty` (bawaan **0.2**) kini benar-benar dikirim. Nilai `0` berarti "jangan kirim". |
| Deteksi | `Generator._corrupted_words()` menemukan kata terpotong/tercampur, dibandingkan dengan konteks sehingga istilah teknis dan nama kolom yang sah tidak ikut ditandai. |
| Perbaikan | Bila terdeteksi, model diminta menulis ulang jawaban yang **sama** (tanpa menambah/mengurangi fakta) pada `temperature=0`. Hasilnya ditolak bila jadi jauh lebih pendek atau kehilangan sitasi. |
| Pengaturan | `temperature`, `top_p`, `frequency_penalty`, `repair_attempts` bisa diubah dari **Pengaturan → Model AI**, tanpa redeploy. |

### Detektor: hanya pola yang hampir pasti rusak

Diuji dengan 13 kalimat (3 rusak, 10 normal) — semuanya lulus. Yang ditandai hanya kata yang
**tidak ada di konteks** dan bukan kata umum, lalu cocok salah satu pola:

* huruf besar di tengah kata kecil (`hanyaLEMpar`), kecuali camelCase lazim (`knowledgeBase`);
* huruf sama tiga kali atau lebih (`cgiiian`);
* lima konsonan beruntun (`cgiian` → `cgii`);
* sufiks Inggris menempel pada kata Indonesia (`praktikumaccording`);
* pola vokal `ii+a` (`cgiian`), sedangkan `sesuai` (vokal berbeda) tidak ditandai.

### Jebakan yang ditemukan saat menguji

**Gateway menolak `top_p` dengan HTTP 400.** Yang lebih buruk: pesannya menyesatkan
("insufficient credits"), padahal `frequency_penalty` diterima. Tanpa penanganan, satu parameter
opsional membuat **seluruh jawaban gagal (502)**.

Solusinya adaptif: parameter opsional dikirim sebagai percobaan; bila ditolak 400/422, parameter
itu dibuang dan permintaan diulang. Kegagalan permanen hanya untuk permintaan dasar, dan galat
yang dilaporkan adalah galat asli terakhir — bukan tuduhan keliru ke parameter.

---

## 2. Reranker: dari "tidak ada" menjadi bekerja

### Akar masalah

`reranker_provider=none` **bukan** reranker. `NoopReranker` hanya mengembalikan skor menurun yang
mempertahankan urutan fusi — urutan hasil tidak pernah diperbaiki. Di produksi memang begitu:
image sengaja ramping (`WITH_LOCAL_MODELS=0`, tanpa torch/fastembed), dan gateway yang dipakai
tidak menyediakan `/rerank` (404).

### Perbaikan: `LexicalReranker` (bawaan, tanpa dependensi)

Reranker lintas-encoder berbasis leksikal yang menilai ulang kandidat dengan sinyal yang **tidak**
dipakai pencarian vektor:

* **IDF kueri** — kata langka lebih menentukan daripada kata umum;
* **bonus frasa utuh** — `"cuti tahunan"` sebagai frasa dinilai lebih tinggi;
* **kedekatan kata** — kata kueri yang berdekatan lebih relevan;
* **cakupan kueri** — berapa banyak kata kueri benar-benar hadir.

Terukur: kandidat relevan yang masuk di **peringkat 3** naik ke **peringkat 1**.

### Dua hal yang dijaga

1. **Skor dinormalkan ke 1.0** untuk kandidat terbaik, sama seperti sisi sparse (`skor/best`).
   Alasannya bukan kosmetik: threshold relevansi (bawaan `0.35`) dibandingkan dengan skor
   terbaik. Tanpa normalisasi, semua kandidat bisa berada di bawah threshold dan layanan
   menjawab **"tidak ditemukan" padahal datanya ada**.
2. **Fallback yang jujur.** Provider neural yang pustakanya tidak terpasang tidak lagi berakhir
   `none` diam-diam: layanan memakai `lexical` dan **mencatatnya di log**, supaya operator tahu
   kualitas yang benar-benar dipakai.

`none` tetap tersedia sebagai pilihan sadar untuk mematikan reranking.

### Bisa diatur dari konsol

**Pengaturan → Retrieval → Dokumen besar**: saklar *pakai reranker* + pilihan mesin
(`lexical` / `fastembed` / `sentence_transformers` / `none`). Nilai divalidasi di server: salah
ketik ditolak `422`, bukan diam-diam jatuh ke `none`.

---

## Verifikasi

```bash
# detektor teks rusak (13 kalimat: 3 rusak, 10 normal)
PYTHONPATH=. .venv/Scripts/python.exe scripts/check_corruption_detector.py

# reranker: urutan membaik + skor ternormalkan + fallback
PYTHONPATH=. .venv/Scripts/python.exe scripts/check_reranker_behavior.py

# parameter sampling adaptif (gateway menolak top_p)
PYTHONPATH=. .venv/Scripts/python.exe scripts/check_sampling_fallback.py

# uji otomatis
.venv/Scripts/python.exe -m pytest tests/integration/test_answer_quality_hardening.py -q
.venv/Scripts/python.exe -m pytest tests/integration/test_quality_settings.py -q

# uji hidup (server di 8099): reranker lexical + jawaban bersih
.venv/Scripts/python.exe scripts/verify_reranker_live.py
```

Hasil suite: **468 lulus, 1 dilewati**.

---

## Yang TIDAK diubah (dan alasannya)

* **Model LLM** — dipilih dari Pengaturan; itu keputusan operator, bukan kode.
* **Embedder `hash`** — menggantinya berarti **semua potongan harus di-embed ulang** (ruang
  vektor berbeda). Gateway yang dipakai juga tidak menyediakan `/embeddings` (400). Ini
  pekerjaan tersendiri, bukan bagian dari perbaikan teks rusak.
* **Streaming** — menambah streaming adalah pekerjaan besar (SSE + UI). Lihat catatan di
  `docs/verification.md`: jawaban saat ini muncul sekaligus, bukan token demi token.

## Temuan lanjutan dari produksi

Verifikasi di `rag.aiones.app` menemukan dua hal yang tidak terlihat di lingkungan uji:

1. **Env produksi masih `RERANKER_PROVIDER=none`.** Kode sudah punya reranker leksikal, tetapi
   nilai env di Dockerfile belum ikut berubah - jadi reranker tidak pernah dipakai. Sekarang
   Dockerfile menyetel `RERANKER_PROVIDER=lexical` dan `RERANKER_ENABLED=true` sebagai bawaan
   image; nilainya tetap bisa diubah dari Pengaturan tanpa redeploy.
2. **Kata menempel pada angka tahun** (`Laporan Tahunan2025`). Ini pola yang berbeda dari yang
   sudah ditangani: huruf kecil langsung diikuti empat digit. Detektornya sekarang menandai pola
   itu **hanya bila bentuk tersebut tidak ada di konteks** - sehingga istilah sah seperti
   `PSL2025` (tertulis begitu di dokumen) tidak ikut ditandai, sedangkan `Tahunan2025` ditandai.

Detektor kini diuji dengan **17 kalimat** (5 rusak, 12 normal) dan semuanya lulus.
