# Swagger UI & OpenAPI

Layanan ini sudah menyediakan dokumentasi API interaktif dari FastAPI — tidak ada langkah tambahan.

| Alamat | Isi |
|---|---|
| `/docs` | **Swagger UI** — daftar endpoint, skema, tombol *Try it out* |
| `/redoc` | ReDoc — tampilan dokumentasi yang lebih enak dibaca untuk referensi |
| `/openapi.json` | Spesifikasi OpenAPI 3.1 (sumber semua yang di atas) |
| `docs/openapi.json`, `docs/openapi.yaml` | Salinan yang **dikomit** ke repo agar bisa dipakai tanpa menjalankan layanan |

Cara mencoba endpoint dari Swagger UI:

1. Buka `http://<host>:8000/docs`.
2. Klik **Authorize**, isi kunci API (skema `HTTPBearer`), lalu *Authorize*.
3. Pilih endpoint → **Try it out** → isi body → **Execute**.
4. `organization_id` tidak perlu diisi (datang dari kunci).

## Ekspor ulang spesifikasi

Spesifikasi di repo dibuat ulang dari kode, jadi tidak akan menyimpang:

```bash
.venv/bin/python scripts/export_openapi.py     # tulis docs/openapi.json + docs/openapi.yaml
.venv/bin/python scripts/gen_api_reference.py  # tulis docs/api-reference.md dari spesifikasi itu
```

Keduanya dijalankan otomatis oleh `scripts/build_docs.sh` sebelum situs dibangun.

## Memakai spesifikasi di alat lain

=== "Postman / Insomnia / Bruno"

    Impor berkas `docs/openapi.json` (atau URL `<host>/openapi.json`). Semua endpoint, parameter, dan
    contoh body akan terbentuk otomatis; isi variabel environment `host` dan `token`.

=== "Generate klien (openapi-generator)"

    ```bash
    npx @openapitools/openapi-generator-cli generate \
      -i docs/openapi.json -g python -o clients/python
    # g = javascript, typescript-fetch, php, go, java, kotlin, swift, csharp, rust ...
    ```

=== "Kontrak-test / validasi"

    ```bash
    # validasi spesifikasi
    npx @redocly/cli lint docs/openapi.json
    # atau pakai schemathesis untuk uji properti terhadap layanan yang hidup
    pip install schemathesis && schemathesis run docs/openapi.json --base-url http://localhost:8000
    ```

=== "Mock server dari spesifikasi"

    ```bash
    npx @stoplight/prism-cli mock docs/openapi.json --port 4010
    # berguna untuk mengembangkan klien sebelum layanan siap
    ```

## Yang perlu diketahui tentang spesifikasi ini

- Autentikasi ditandai sebagai `HTTPBearer` (`Authorization: Bearer <kunci>`).
- `X-Request-Id` dan `X-Tenant-Context` adalah header opsional (tersedia di CORS allow-list).
- Response error memakai satu envelope yang sama; rinciannya di [Kontrak & alur API](api.md).
- Skema `QueryDataOut` memuat `computed` (hasil hitung tabel) dan `table_note` (catatan cakupan),
  sehingga klien tahu kapan angka berasal dari perhitungan kode.
