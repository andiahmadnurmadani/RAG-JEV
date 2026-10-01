# Pakai ulang perangkat dokumentasi ini untuk aplikasi lain

Perangkat di bawah ini sengaja dibuat **tidak terikat** pada proyek ini. Untuk aplikasi lain, salin
berkasnya, ganti judul/nav — selesai. Tidak ada akun layanan berbayar dan tidak ada build server yang dibutuhkan.

## Berkas yang disalin

| Berkas | Fungsi | Perlu diubah? |
|---|---|---|
| `mkdocs.yml` | Konfigurasi situs (tema, ekstensi, nav) | Ya: `site_name`, `repo_url`, `nav` |
| `requirements-docs.txt` | Perkakas dokumentasi (MkDocs Material) | Ya: sesuaikan versi bila perlu |
| `docs/*.md` | Isi dokumentasi | Ya: isi sendiri |
| `scripts/export_openapi.py` | Menulis `docs/openapi.json` + `.yaml` dari aplikasi | Hanya bila entrypoint bukan `app.main:app` |
| `scripts/gen_api_reference.py` | Menulis `docs/api-reference.md` dari `openapi.json` | Tidak |
| `scripts/build_docs.sh` | Urutan build: ekspor → generate → `mkdocs build` | Tidak |
| `app/main.py` (bagian `DOCS_SITE_DIR`) | Menyajikan `site/` di `/guide` dari aplikasi | Ya: sesuaikan path env |

## Langkah untuk proyek baru

```bash
# 1) salin perangkatnya
cp -r path/ke/rag-service/{mkdocs.yml,requirements-docs.txt} .
mkdir -p docs scripts
cp path/ke/rag-service/scripts/{build_docs.sh,export_openapi.py,gen_api_reference.py} scripts/

# 2) pasang perkakas dokumentasi (terpisah dari dependensi runtime)
uv pip install -r requirements-docs.txt
#    tanpa venv: uvx --from mkdocs-material mkdocs build

# 3) tulis isi minimal
cat > docs/index.md <<'MD'
# Nama Aplikasi

Ringkasan singkat: apa ini, untuk siapa, cara mulai.
MD

# 4) bangun
bash scripts/build_docs.sh          # hasil: site/
mkdocs serve                        # pratinjau di http://127.0.0.1:8001
```

Prasyarat untuk `export_openapi.py` dan `gen_api_reference.py`: aplikasi Anda punya berkas
`openapi.json`. Kerangka apa pun yang menghasilkan OpenAPI bisa dipakai (FastAPI, NestJS, Laravel
Scramble, Spring, ASP.NET, dsb.) — cukup lewatkan berkasnya:

```bash
python scripts/gen_api_reference.py --spec openapi.json --out docs/api-reference.md
```

## Menyajikan situs dari aplikasi Anda sendiri

Pola yang dipakai proyek ini (lihat `app/main.py`):

```python
site_dir = Path(settings.docs_site_dir)
if site_dir.is_dir():
    app.mount("/guide", StaticFiles(directory=str(site_dir), html=True), name="guide")
```

Dengan begitu pengguna cukup membuka `http://<host>:8000/guide/` — tidak ada nginx/tambahan layanan.
Alternatif lain:

- **nginx**: `root /srv/app/site;` pada `location /guide/` (tanpa mengubah aplikasi).
- **GitHub Pages / Cloudflare Pages / Netlify**: `mkdocs gh-deploy` atau publish folder `site/`.
- **GitBook / Notion / Outline**: impor folder `docs/` (GitBook menerima Markdown; `SUMMARY.md` opsional),
  berguna bila tim lebih suka editor daring. Kelemahannya: pratinjau otomatis dan kontrol versi ikut ke layanan itu.

## Pilihan perkakas (dan kapan memilihnya)

| Perkakas | Kelebihan | Kapan dipakai |
|---|---|---|
| **MkDocs Material** (dipakai di sini) | Statis, pencarian bawaan, satu berkas konfigurasi, bisa ikut disajikan aplikasi, tanpa Node | Default untuk dokumentasi produk/API di repo |
| **Swagger UI / ReDoc** (bawaan FastAPI) | Nol konfigurasi, langsung bisa *Try it out* | Referensi API interaktif |
| **Docusaurus / Nextra** | Portal docs multi-versi, blog, i18n kaya | Bila butuh versi rilis bercabang & situs marketing |
| **GitBook** | Editor kolaboratif, non-teknis nyaman | Tim non-teknis yang menulis langsung |
| **Sphinx / docsify / mdBook** | Ekosistem Python / ringan / Rust | Sesuai preferensi tim |

Semuanya menerima Markdown yang sudah ada di `docs/`, jadi berpindah perkakas tidak berarti menulis ulang.

## Aturan supaya dokumentasi tidak basi

1. **Satu sumber kebenaran.** Referensi endpoint dan skema **tidak ditulis tangan** — di-generate dari
   `openapi.json` (`docs/api-reference.md`). Jangan menyunting berkas hasil generate.
2. **Angka & klaim harus punya bukti.** Uji hidup ditaruh di halaman [Verifikasi](verification.md),
   bukan disebar sebagai klaim di halaman pemasaran.
3. **Bangun ulang saat merilis.** `scripts/build_docs.sh` dijalankan sebelum menandai rilis; bila situs
   disajikan aplikasi, jalankan setelah `git pull`.
4. **Tulis manual, bukan karangan mesin.** Halaman seperti ini boleh dibantu AI, tetapi setiap perintah
   harus benar-benar pernah dijalankan (lihat catatan "belum diverifikasi" di [Deploy](deployment.md)).
