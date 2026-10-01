# Knowledge dari web dan berkas publik

Dua sumber di luar unggahan berkas:

| Sumber | Cara pakai | Jadi apa |
|---|---|---|
| **Berkas publik** | `POST /knowledge/index` dengan `file_url` | satu dokumen, diambil server lalu diparse seperti unggahan biasa |
| **Halaman / situs web** | `POST /knowledge/index` dengan `web_url` | satu dokumen berisi banyak halaman; setiap halaman menyimpan alamatnya sendiri |

Bedanya penting: `file_url` mengambil **satu berkas**, `web_url` **menjelajahi tautan** di dalamnya.
Satu permintaan hanya boleh memuat salah satu — mencampurnya ditolak `422`.

## Contoh

```bash
# satu berkas publik
curl -X POST "$BASE/knowledge/index" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "document_id": "rfc2606",
  "knowledge_base_id": "kb_chat",
  "file_url": "https://www.rfc-editor.org/rfc/rfc2606.txt"
}'

# satu halaman saja
curl -X POST "$BASE/knowledge/index" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "document_id": "panduan",
  "knowledge_base_id": "kb_chat",
  "web_url": "https://contoh.id/panduan",
  "web_max_pages": 1,
  "web_max_depth": 0
}'

# menjelajahi situs kecil
curl -X POST "$BASE/knowledge/index" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
  "document_id": "situs_sop",
  "knowledge_base_id": "kb_chat",
  "web_url": "https://contoh.id/sop/",
  "web_max_pages": 25,
  "web_max_depth": 2,
  "web_follow_files": true
}'
```

Dari konsol: tombol **Dari URL** di panel Dokumen, lalu centang **Jelajahi tautan di halaman ini**.
Isi maks halaman dan kedalaman, tekan **Jelajahi & indeks**.

## Yang diambil, dan yang tidak

- Halaman HTML diubah jadi teks (navigasi, skrip, dan gaya dibuang).
- Berkas yang ditautkan (PDF, DOCX, XLSX, ...) ikut diambil bila `web_follow_files` aktif, dan
  diparse oleh parser format yang sama dengan unggahan biasa.
- Tautan yang dilewati: gambar/gaya/skrip/video, arsip, `mailto:`/`tel:`/`javascript:`, serta
  alamat yang jelas bukan isi (`/login`, `/cart`, `/wp-admin`, `?s=`, `/tag/`, ...).
- **robots.txt dihormati** secara bawaan; halaman yang dilarang tidak diambil dan alasannya
  dicatat (bukan gagal diam-diam).
- Hanya tautan pada alamat yang sama yang diikuti (`web_crawl_same_host`), kecuali dimatikan.
- Batas: jumlah halaman, kedalaman, ukuran per halaman, dan waktu per permintaan.

## Satu halaman = satu sumber, bukan satu tumpukan

Setiap halaman web menjadi bagian dokumen dengan alamatnya sendiri, dan potongan **tidak pernah
menggabung dua halaman berbeda**. Akibatnya:

- sitasi menunjuk halaman yang benar (`/sop/cuti`, bukan alamat akar situs);
- di konsol, tiap sitasi punya tautan **buka**;
- tumpang tindih potongan (overlap) dimatikan antar halaman, karena menyalin ekor halaman lain
  membuat jawaban tentang halaman A bisa tampak berasal dari halaman B.

## Pengaman (SSRF) — jangan dimatikan sembarangan

Fitur ini membuat server mengeluarkan permintaan HTTP atas nama pemanggil. Tanpa pengaman, satu
baris payload bisa menyuruh server membaca `http://169.254.169.254/...` (kredensial cloud),
`http://127.0.0.1:8000/...` (API ini sendiri), atau port lain di jaringan dalam — lalu isinya
masuk knowledge dan bisa dibaca lewat pencarian.

Yang dilakukan `app/core/urlguard.py`:

1. hanya `http`/`https`;
2. tidak boleh ada kredensial di URL;
3. nama terlarang: `localhost`, `*.local`, `*.internal`, nama metadata cloud;
4. **semua** alamat hasil DNS harus publik — menutup DNS rebinding (nama publik yang sesekali
   menunjuk `127.0.0.1` tetap ditolak);
5. diperiksa **ulang di setiap pengalihan** (jalur klasik untuk lolos dari pemeriksaan awal);
6. `169.254.x.x` dan `metadata.google.internal` **selalu** ditolak, bahkan saat
   `ALLOW_PRIVATE_URLS=true` — itu bukan intranet, itu kredensial mesin.

`ALLOW_PRIVATE_URLS` (bawaan **mati**) hanya untuk pemasangan yang memang harus mengambil
intranet, mis. server uji. Menyalakannya melebarkan jangkauan server ke jaringan tempat ia
berjalan: pastikan hanya dipakai di jaringan tepercaya.

URL yang ditolak menjawab `422 VALIDATION_ERROR` **sebelum** pekerjaan dibuat, jadi tidak ada
job gagal yang menumpuk.

## Setelan (panel **Sumber web**, tanpa redeploy)

| Setelan | Env | Bawaan |
|---|---|---|
| izinkan knowledge dari web | `WEB_CRAWL_ENABLED` | aktif |
| maks halaman per crawl | `WEB_CRAWL_MAX_PAGES` | 20 (1–200) |
| kedalaman tautan | `WEB_CRAWL_MAX_DEPTH` | 2 (0–5) |
| hanya host yang sama | `WEB_CRAWL_SAME_HOST` | aktif |
| ikut ambil berkas tertaut | `WEB_CRAWL_FOLLOW_FILES` | aktif |
| hormati robots.txt | `WEB_CRAWL_RESPECT_ROBOTS` | aktif |
| izinkan alamat privat | `ALLOW_PRIVATE_URLS` | **mati** |

## Hasil di dokumen

Di daftar dokumen, baris hasil web menampilkan jumlah halaman dan tautan sumbernya. Contoh uji
nyata (situs 4 halaman, kedalaman 2):

```
127.0.0.1 (4 halaman) | completed | 4 chunk | http://127.0.0.1:63426/ | web_situs
```

Pertanyaan tentang isi salah satu halaman menjawab dengan sitasi ke halaman itu, mis.
*"Formulir lampiran memakai kode `lampirancuti` [1]"* dengan sumber
`http://…/sop/cuti/lampiran`.

## Uji

```bash
# unit: batas pengaman URL (SSRF, skema, DNS rebinding)
.venv/Scripts/python.exe -m pytest tests/unit/test_urlguard.py -q

# integrasi: halaman, crawl, robots.txt, berkas publik, penolakan URL (server HTTP lokal)
.venv/Scripts/python.exe -m pytest tests/integration/test_web_knowledge.py tests/integration/test_web_settings.py -q

# hidup: unggah + tanya dengan model nyata (perlu layanan di 127.0.0.1:8099)
ALLOW_PRIVATE_URLS=true PORT=8099 JEV_ENABLED=false bash scripts/run_live.sh
.venv/Scripts/python.exe scripts/verify_web_knowledge_live.py
```

## Batas yang jujur

- Tanpa JavaScript: halaman yang isinya dirender di peramban (SPA) akan terbaca kosong. Itu
  dilaporkan sebagai "tidak ada teks terbaca", bukan disimpan sebagai dokumen kosong.
- Tanpa OCR: gambar di halaman tidak dibaca (lihat panel **Format berkas** untuk status OCR).
- Crawl besar tetap dibatasi jumlah halaman/kedalaman; untuk situs besar, indeks per bagian.
- Isi halaman web adalah **data tak tepercaya**: prompt sudah menandainya begitu dan instruksi di
  dalamnya tidak pernah dijalankan (lihat `docs/rag-pipeline.md`).
