# Integrasi klien (aplikasi lain)

Halaman ini untuk **pengembang aplikasi lain** (web, mobile, backend, otomasi) yang ingin memakai layanan ini.
Semua contoh sudah dicoba terhadap layanan yang hidup; nilai kunci pada contoh adalah placeholder.

## 1. Alur yang benar

```text
  1) POST /knowledge/index        -> 202 queued   (asinkron, jawab cepat)
  2) GET  /knowledge/{doc_id}     -> poll sampai status "completed"
  3) POST /query                  -> jawaban + sitasi (atau penolakan jujur)
     POST /search                 -> hanya potongan teks (tanpa LLM)
     POST /extract                -> keluaran terstruktur (JSON sesuai skema Anda)
     GET  /tables                 -> tabel terstruktur dari spreadsheet
```

Dua hal yang wajib diingat:

- **Setiap kueri ter-scope ke satu knowledge base** (`knowledge_base_id` wajib pada `/query`, `/search`,
  `/extract`, `/knowledge/index`).
- **`organization_id` tidak boleh dikirim klien.** Kalau dikirim pun, nilainya harus sama dengan konteks
  kunci; kalau berbeda → `403`. Organisasi selalu berasal dari kunci API.

## 2. Autentikasi

```http
Authorization: Bearer <kunci-API>
Content-Type: application/json
X-Request-Id: <id-korelasi-opsional>     # dikembalikan apa adanya di header respons
```

| Permission | Boleh melakukan |
|---|---|
| `read` | `GET /knowledge`, `GET /knowledge/{id}`, `POST /query`, `POST /search`, `POST /extract`, `GET /tables` |
| `write` | `POST /knowledge/index`, `PUT /knowledge/{id}`, `DELETE /knowledge/{id}` |
| `admin` | Semua endpoint `/settings` (baca & ubah setelan LLM/Jev/katalog format) |

Catatan penting: `GET /health`, `GET /ready`, dan `GET /metrics` **tidak** butuh kunci (untuk probe
infrastruktur). Jangan taruh data sensitif di sana — memang tidak ada.

### 2.1 Dari mana kunci datang

| Sumber | Siapa yang membuat | Bisa dicabut sendiri |
|---|---|---|
| `API_KEYS_JSON` (env) | operator saat deploy | tidak — ubah env lalu deploy ulang |
| Panel **Kunci API** di layar Pengaturan | kunci berizin `admin`, lewat `POST /settings/api-keys` | ya — `DELETE /settings/api-keys/{key_id}`, berlaku **segera** |

Kunci buatan UI:

- nilainya berupa `rag_...` dan **hanya dikembalikan sekali** di respons pembuatan; setelah itu
  layanan hanya menyimpan `sha256`-nya, jadi kunci yang hilang harus dibuat ulang;
- konteks tenant-nya **mewarisi konteks pembuat** (`organization_id`, `user_id`, `application_id`);
  membuat kunci untuk tenant lain butuh kunci berizin `*`, kalau tidak dijawab `403`;
- izin default-nya `read` + `write`; `admin` harus dipilih eksplisit (kunci seperti ini bisa
  mengubah setelan layanan);
- bisa diberi masa berlaku (`expires_in_days`), dan setelah lewat kunci langsung tidak berlaku;
- yang dicabut tidak dihapus dari daftar — statusnya `revoked` supaya jejaknya tetap bisa diaudit.

## 3. Endpoint

| Method | Path | Permission | Sifat | Keterangan |
|---|---|---|---|---|
| POST | `/knowledge/index` | write | asinkron (`202`) | Unggah via `text`, `content_base64`, atau `file_url` |
| GET | `/knowledge` | read | sinkron | Daftar dokumen pada scope pemanggil |
| GET | `/knowledge/{document_id}` | read | sinkron | Status/job satu dokumen |
| PUT | `/knowledge/{document_id}` | write | asinkron | Ganti isi dokumen (idempoten) |
| DELETE | `/knowledge/{document_id}` | write | sinkron | Hapus dokumen + vektornya |
| POST | `/query` | read | sinkron | Jawaban bersitasi + `computed` untuk pertanyaan angka |
| POST | `/search` | read | sinkron | Potongan teks + skor (tanpa generasi) |
| POST | `/extract` | read | sinkron | Ekstraksi terstruktur dari knowledge |
| GET | `/tables` | read | sinkron | Tabel terstruktur (xlsx/csv/ods) di scope pemanggil |
| GET/PUT | `/settings` | admin | sinkron | Setelan LLM/Jev/katalog format (kunci bersifat write-only) |
| POST | `/settings/llm/models` | admin | sinkron | Daftar model dari endpoint LLM yang dikonfigurasi |
| POST | `/settings/jev/probe` | admin | sinkron | Uji koneksi Jev |
| GET | `/settings/api-keys` | admin | sinkron | Daftar kunci (nilai kunci tidak pernah ikut) |
| POST | `/settings/api-keys` | admin | sinkron | Buat kunci baru; nilainya hanya tampil di respons ini |
| DELETE | `/settings/api-keys/{key_id}` | admin | sinkron | Cabut kunci buatan UI (kunci env ditolak `422`) |
| GET | `/health` · `/ready` · `/metrics` | — | sinkron | Liveness, kesiapan per dependensi, metrik |

## 4. Contoh kode

=== "cURL"

    ```bash
    BASE=https://rag.example.com/api/v1
    KEY=xxxxx

    # unggah (asinkron)
    curl -s -X POST "$BASE/knowledge/index" \
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -d '{"document_id":"sop-01","knowledge_base_id":"kb-sop","text":"Isi SOP ...","metadata":{"unit":"ops"}}'

    # tanya
    curl -s -X POST "$BASE/query" \
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -d '{"query":"berapa lama proses persetujuan?","knowledge_base_id":"kb-sop"}'
    ```

=== "Python (requests)"

    ```python
    import time, requests

    BASE = "https://rag.example.com/api/v1"
    KEY = "xxxxx"
    H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    def index(doc_id: str, kb: str, text: str) -> str:
        r = requests.post(f"{BASE}/knowledge/index", headers=H, timeout=30, json={
            "document_id": doc_id, "knowledge_base_id": kb, "text": text, "replace": True,
        })
        r.raise_for_status()
        return r.json()["data"]["job_id"]

    def wait(doc_id: str, kb: str, timeout: float = 180) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = requests.get(f"{BASE}/knowledge/{doc_id}", headers=H, timeout=30).json()["data"]
            if s["status"] in {"completed", "failed"}:
                return s["status"]
            time.sleep(1.5)
        raise TimeoutError(f"indeks {doc_id} tidak selesai")

    def ask(question: str, kb: str) -> dict:
        r = requests.post(f"{BASE}/query", headers=H, timeout=120,
                          json={"query": question, "knowledge_base_id": kb})
        r.raise_for_status()
        return r.json()["data"]

    index("sop-01", "kb-sop", "Isi SOP ...")
    print(wait("sop-01", "kb-sop"))
    answer = ask("berapa lama proses persetujuan?", "kb-sop")
    print(answer["grounded"], answer["answer"], answer["sources"])
    ```

=== "Python (httpx async)"

    ```python
    import httpx, asyncio

    BASE, KEY = "https://rag.example.com/api/v1", "xxxxx"
    H = {"Authorization": f"Bearer {KEY}"}

    async def main():
        async with httpx.AsyncClient(base_url=BASE, headers=H, timeout=120) as c:
            r = await c.post("/query", json={"query": "ringkas SOP", "knowledge_base_id": "kb-sop"})
            print(r.json()["data"]["answer"])

    asyncio.run(main())
    ```

=== "JavaScript (browser / Node 18+)"

    ```javascript
    const BASE = "/api/v1";                       // sebaiknya lewat proxy server sendiri
    const KEY  = "xxxxx";                         // jangan hard-code di frontend publik

    async function ask(query, knowledgeBaseId) {
      const res = await fetch(`${BASE}/query`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({ query, knowledge_base_id: knowledgeBaseId }),
      });
      if (!res.ok) throw new Error(`RAG ${res.status}: ${(await res.text()).slice(0, 200)}`);
      const { data } = await res.json();
      return { answer: data.answer, grounded: data.grounded, sources: data.sources };
    }
    ```

=== "PHP (cURL)"

    ```php
    <?php
    $base = 'https://rag.example.com/api/v1';
    $key  = 'xxxxx';

    $ch = curl_init("$base/query");
    curl_setopt_array($ch, [
      CURLOPT_RETURNTRANSFER => true,
      CURLOPT_TIMEOUT        => 120,
      CURLOPT_POST           => true,
      CURLOPT_HTTPHEADER     => ["Authorization: Bearer $key", 'Content-Type: application/json'],
      CURLOPT_POSTFIELDS     => json_encode(['query' => 'ringkas SOP', 'knowledge_base_id' => 'kb-sop']),
    ]);
    $body = json_decode(curl_exec($ch), true);
    curl_close($ch);
    echo $body['data']['answer'] ?? 'tidak ada jawaban';
    ```

=== "PHP (Laravel)"

    ```php
    use Illuminate\Support\Facades\Http;

    $response = Http::withToken(config('services.rag.key'))
        ->timeout(120)
        ->post(config('services.rag.url').'/query', [
            'query' => $question,
            'knowledge_base_id' => 'kb-sop',
        ]);

    $data = $response->throw()->json('data');
    // $data['answer'], $data['grounded'], $data['sources']
    ```

## 5. Pola integrasi yang tahan banting

**Polling status pengindeksan** — beri jeda 1–2 detik lalu naik perlahan; dokumen besar bisa butuh puluhan detik.
Jangan poll lebih cepat dari 1 detik.

**Idempotensi** — pakai `document_id` tetap + `replace: true` (default) supaya unggahan ulang mengganti versi lama,
bukan menggandakan. Aman dipanggil ulang saat jaringan putus.

**Retry** — ulangi untuk `429`, `500`, `502`, `503`, `504` dengan backoff eksponensial + jitter.
`429` menyertakan `details.retry_after_seconds`. **Jangan** mengulang `401`, `403`, `413`, `415`, `422`
(gagal tetap akan gagal).

**Timeout** — `/query` melibatkan retrieval + rerank + generasi; sediakan timeout klien ≥ 120 detik.
`/search` dan `/knowledge` jauh lebih cepat.

**Jawaban "tidak ditemukan" bukan error** — HTTP tetap `200` dengan `grounded: false` + `no_answer_reason`.
Tampilkan apa adanya (jangan minta LLM mengarang ulang).

**Angka dari spreadsheet** — untuk pertanyaan angka, baca `computed` (berisi `operation`, `metric`,
`rows_matched`, `rows_total`, `result`) dan `table_note` bila ada catatan cakupan. Angka itu dihitung
kode dari baris tabel; jangan hitung ulang secara kasar di klien.

**Batasan berkas** — `MAX_UPLOAD_MB` (default 32), ekstensi yang diizinkan bisa dilihat/diatur di `/settings`.
Unggahan lebih besar/format terlarang → `413`/`415`.

**Belum ada**: webhook/callback (pakai polling), SDK resmi (generate dari OpenAPI), streaming token.
Semua endpoint memakai HTTPS biasa.

## 6. Envelope respons & kode error

Sukses:

```json
{"success": true, "data": { ... }}
```

Gagal (semua endpoint memakai bentuk yang sama):

```json
{"success": false, "error": {"code": "AUTH_FORBIDDEN", "message": "...", "request_id": "req_...", "details": {...}}}
```

| Kode | HTTP | Arti & tindakan |
|---|---|---|
| `AUTH_INVALID` | 401 | Kunci hilang/salah. Perbaiki header `Authorization`. |
| `TENANT_CONTEXT_MISSING` | 401 | Konteks tenant wajib tapi tidak ada (mode token tenant) |
| `AUTH_FORBIDDEN` | 403 | Kunci tidak punya izin; lihat `details.permission` |
| `KNOWLEDGE_NOT_FOUND` | 404 | Rute/KB tidak ada |
| `DOCUMENT_NOT_FOUND` | 404 | `document_id` tidak ada di scope ini |
| `VALIDATION_ERROR` | 422 | Body/param tidak valid; `details` menyebut field-nya |
| `PAYLOAD_TOO_LARGE` | 413 | Melebihi `MAX_UPLOAD_MB` |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | Ekstensi/tipe berkas tidak diizinkan |
| `RATE_LIMITED` | 429 | Kuota per menit habis; tunggu `details.retry_after_seconds` |
| `INDEXING_FAILED` | 500 | Pengindeksan gagal; lihat `error.message` pada status dokumen |
| `EMBEDDING_FAILED` | 500 | Embedder bermasalah (model/provider) |
| `RETRIEVAL_FAILED` | 500 | Pencarian gagal (biasanya penyimpanan vektor) |
| `RERANK_FAILED` | 500 | Reranker gagal |
| `LLM_FAILED` | 502 | Endpoint LLM menolak/timeout — cek setelan LLM |
| `JEV_FAILED` | 502 | Jev tidak terjangkau (layanan tetap bisa jalan lewat fallback) |
| `INTERNAL_ERROR` | 500 | Tak terduga; sertakan `request_id` saat melapor |

Sertakan `request_id` dari respons (atau kirim `X-Request-Id` sendiri, yang akan dikembalikan di header respons)
supaya operator bisa menelusuri log.

## 7. Keamanan saat integrasi

- **Kunci API jangan ditaruh di aplikasi klien publik** (browser/mobile). Panggil layanan dari server Anda
  (BFF/proxy) lalu teruskan jawabannya ke pengguna. Konsol `/ui/` milik layanan ini adalah kasus khusus untuk uji internal.
- Satu kunci = satu organisasi. Buat kunci terpisah per aplikasi (`application_id`) supaya kuota bisa
  diaudit/dicabut tanpa mengganggu yang lain.
- `CORS_ORIGINS` dibiarkan kosong kecuali memang perlu akses lintas asal.
- Jangan kirim `organization_id` dari klien; itu bukan parameter, itu konteks.

## 8. Untuk menghasilkan klien sendiri

`docs/openapi.json` (dan `docs/openapi.yaml`) adalah spesifikasi lengkap layanan ini. Pakai untuk
generate klien di bahasa apa pun — lihat [Swagger UI & OpenAPI](swagger.md).
