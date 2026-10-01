# Ringkasan dokumen sebagai knowledge turunan

Saat dokumen diunggah, isinya **diringkas sekali** dan ringkasannya ikut disimpan sebagai
knowledge. Jadi ketika user meminta ringkasan, jawabannya datang dari ringkasan itu - bukan dari
sebagian potongan hasil pencarian.

## Kenapa ini perlu

Pertanyaan *"ringkas dokumen X"* sulit dijawab dengan retrieval biasa:

- pencarian kemiripan mengembalikan potongan yang **mirip dengan pertanyaannya**, bukan seluruh
  isi dokumen - jadi ringkasannya sebagian, atau model bilang datanya tidak ada di konteks;
- menyeret seluruh dokumen ke konteks itu mahal dan cepat menghabiskan anggaran token.

Ringkasan yang dibuat sekali saat upload menyelesaikan keduanya: satu potongan padat, dibuat dari
seluruh isi, dan bisa dipakai berulang tanpa biaya model lagi.

## Cara kerjanya

```
unggah berkas
   -> parse + potong jadi chunk          (seperti biasa)
   -> RINGKAS isi dokumen                (map-reduce bila dokumennya besar)
   -> indeks chunk + potongan ringkasan  (document_id sama)
```

Ringkasan disimpan sebagai potongan tersendiri di indeks yang sama dengan:

- `is_summary: true` — penandanya, supaya bisa dibedakan dari isi;
- `chunk_id: chunk_summary` — bukan pola `chunk_NNNN`, jadi tidak pernah bentrok;
- `document_id` yang sama — supaya ikut terhapus saat dokumen dihapus, dan tetap tersaring tenant.

Di daftar dokumen, baris yang punya ringkasan menampilkan tautan **ringkasan N token**; klik untuk
membacanya.

## Tiga aturan yang dijaga (dan diuji)

### 1. Ringkasan tidak ikut bersaing di pencarian biasa

Ringkasan adalah teks yang sudah **dipadatkan**: ia bagus untuk meringkas, tetapi lossy. Kalau ia
ikut masuk hasil pencarian biasa, ia bisa mendesak potongan isi keluar dari `top_k` — dan jawaban
faktual akan datang dari ringkasan, kehilangan detail (nama kolom, angka, pengecualian) tanpa
jejak. Karena itu:

- pencarian dense dan sparse menyaring `is_summary` keluar (`summary_filter`);
- ringkasan diambil **hanya** ketika niat pertanyaannya memang `knowledge_summary` (dikenali
  router: "ringkas", "rangkum", "summary", ...).

Bukti dari uji hidup: pertanyaan *"Berapa tahun masa retensi arsip kepegawaian?"* dijawab dengan
`ringkasan=0` di konteks (murni dari isi), sedangkan *"Tolong ringkas isi dokumen ini"* dijawab
dengan `ringkasan=1` dan sumber `chunk_summary`.

### 2. Isi dokumen tidak pernah dikorbankan demi ringkasan

Ini jaminan lama yang harus tetap berlaku ("tidak ada data yang hilang"). Saat anggaran konteks
mepet:

- **isi didahulukan** (dihitung lebih dulu, sampai `max_chunks`);
- ringkasan hanya mengisi sisa, dan punya porsi sendiri (25% anggaran, maksimum 4000 token).

Cacat ini sempat ada di versi pertama (ringkasan di depan, menghabiskan anggaran, isi terdorong
keluar) dan ketahuan oleh uji `test_the_content_is_never_dropped_in_favour_of_the_summary`.

### 3. Ringkasan gagal tidak menggagalkan pengindeksan

Ringkasan itu opsional. Kalau modelnya mati, kredit habis, atau dimatikan operator: isi dokumen
tetap terindeks dan bisa dicari, `summary` kosong, dan `summary_error` menjelaskan alasannya —
supaya tidak terbaca sebagai "sudah diringkas".

## Dokumen besar: map-reduce

Dokumen besar tidak muat dalam satu jendela model, jadi diringkas bertahap:

1. potongan dikelompokkan sebanyak `summary_window_tokens` (bawaan 12.000) per kelompok;
2. setiap kelompok diringkas (tahap **map**);
3. ringkasan-ringkasan itu diringkas lagi menjadi satu (tahap **reduce**).

Kalau dokumennya lebih dari `summary_max_parts` (bawaan 400), ringkasannya mencakup sebagian dan
itu **dilogkan** - bukan dipotong diam-diam.

## Setelan (panel **Ringkasan**, tanpa redeploy)

| Setelan | Env | Bawaan | Batas |
|---|---|---|---|
| buat ringkasan setiap dokumen | `DOCUMENT_SUMMARY_ENABLED` | aktif | - |
| jendela per tahap (token) | `SUMMARY_WINDOW_TOKENS` | 12000 | 1000–200000 |
| panjang ringkasan (token) | `SUMMARY_MAX_TOKENS` | 2048 | 256–32000 |
| maks dokumen per pertanyaan | `SUMMARY_MAX_DOCUMENTS` | 3 | 1–20 |

## Contoh

```bash
# unggah: ringkasan dibuat otomatis
curl -X POST "$BASE/knowledge/index" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"document_id":"sop","knowledge_base_id":"kb_chat","content_base64":"..."}'

# lihat ringkasannya
curl "$BASE/knowledge/sop" -H "Authorization: Bearer $KEY" | jq '.data.summary'

# minta ringkasan lewat chat
curl -X POST "$BASE/query" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"query":"Tolong ringkas dokumen ini.","knowledge_base_id":"kb_chat"}' | jq '.data.usage.context_summary_chunks'
```

`usage.context_summary_chunks` menunjukkan berapa potongan ringkasan ikut ke konteks;
`usage.context_chunks` tetap berarti **berapa bagian isi** yang dibaca.

## Batas yang jujur

- Ringkasan adalah **turunan**: ia bisa keliru kalau modelnya keliru, walaupun prompt-nya sudah
  melarang menambah isi di luar konteks. Isi aslinya tetap tersimpan dan tetap jadi dasar jawaban
  faktual - karena itu ringkasan tidak pernah menggantikan isi.
- Dokumen yang diunggah **sebelum** fitur ini menyala belum punya ringkasan (`summary_error`
  kosong, `summary` kosong). Unggah ulang dokumennya, atau biarkan - pertanyaan faktual tetap
  jalan seperti sebelumnya.
- Mengganti model LLM tidak membuat ringkasan lama ikut berubah; ringkasan dibuat sekali saat
  pengindeksan.

## Uji

```bash
# 8 uji: dibuat & diindeks, dipakai saat diminta, tidak bersaing di pencarian biasa,
#        isi tidak dikorbankan, map-reduce, gagal tidak fatal, bisa dimatikan, batas tenant
.venv/Scripts/python.exe -m pytest tests/integration/test_document_summary.py -q

# 3 uji setelan + UI
.venv/Scripts/python.exe -m pytest tests/integration/test_summary_settings.py -q

# hidup (model nyata): unggah -> ringkasan -> minta ringkasan -> pertanyaan faktual
PORT=8099 JEV_ENABLED=false bash scripts/run_live.sh
.venv/Scripts/python.exe scripts/verify_document_summary_live.py
```
