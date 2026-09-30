# Mulai cepat

Tujuan halaman ini: dalam 5 menit kamu bisa mengunggah satu dokumen dan mendapat jawaban bersitasi.
Panduan lengkap untuk server ada di [Deploy & menjalankan](deployment.md).

## 1. Jalankan layanan

=== "Tanpa model (paling cepat)"

    ```bash
    cd /path/ke/rag-service
    cp .env.example .env                 # isi API_KEYS_JSON (lihat langkah 2)
    EMBEDDING_PROVIDER=hash RERANKER_PROVIDER=none LLM_PROVIDER=mock \
      .venv/bin/python -m app.main
    ```

    Cocok untuk memastikan API hidup. Jawaban tidak memakai LLM (mode `mock`).

=== "Dengan LLM (jawaban sungguhan)"

    ```bash
    LLM_PROVIDER=openai_compatible \
    LLM_BASE_URL=http://<host-gateway>/v1 \
    LLM_MODEL=<id-model> \
    LLM_API_KEY=<rahasia> \
      .venv/bin/python -m app.main
    ```

Windows: ganti interpreter menjadi `.venv/Scripts/python.exe` (lihat `scripts/run_live.sh`).

## 2. Siapkan kunci API

Kunci API menentukan organisasi pemanggil. Contoh isi `.env`:

```dotenv
API_KEYS_JSON={"kunci-uji":{"user_id":"u1","organization_id":"org_a","application_id":"app_uji","permissions":["read","write","admin"]}}
```

Semua permintaan membawa kunci di header:

```bash
export K="kunci-uji"
export BASE="http://localhost:8000/api/v1"
```

## 3. Cek layanan hidup

```bash
curl -s "$BASE/health"                 # {"status":"ok"}
curl -s "$BASE/ready"                  # status per dependensi: qdrant/embedding/reranker/llm/jev
curl -s -H "Authorization: Bearer $K" "$BASE/knowledge"
```

## 4. Unggah satu dokumen

Pengindeksan berjalan **asinkron**: permintaan dijawab `202` dengan `status: queued`, lalu diproses pekerja latar.

```bash
curl -s -X POST "$BASE/knowledge/index" \
  -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{
        "document_id": "catatan-ops",
        "knowledge_base_id": "kb-uji",
        "document_name": "Catatan operasi",
        "text": "Server produksi berada di rak B. Warna kabel jaringan adalah biru. Kontak darurat: NOC."
      }'
```

Pantau statusnya sampai `completed`:

```bash
curl -s -H "Authorization: Bearer $K" "$BASE/knowledge/catatan-ops"
```

## 5. Bertanya

```bash
curl -s -X POST "$BASE/query" \
  -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"query": "apa warna kabel jaringan?", "knowledge_base_id": "kb-uji"}'
```

Bentuk jawaban (diringkas):

```json
{"success": true, "data": {
  "answer": "Kabel jaringan berwarna biru [1].",
  "grounded": true,
  "sources": [{"document_id": "catatan-ops", "chunk_id": "chunk_0001", "score": 0.83}],
  "usage": {"retrieved_chunks": 5, "reranked_chunks": 5, "reranker": "fastembed"},
  "no_answer_reason": null
}}
```

Bila bukti tidak cukup, `grounded` bernilai `false`, `answer` menjelaskan bahwa jawaban tidak ditemukan,
dan `no_answer_reason` berisi sebabnya (mis. `no_evidence_above_threshold`). Ini **perilaku yang benar**:
lebih baik menolak daripada mengarang.

## 6. Pertanyaan angka dari spreadsheet

Unggah `.xlsx`/`.csv` lalu tanyakan agregatnya; angkanya dihitung dari baris tabel:

```bash
curl -s -X POST "$BASE/query" -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"query": "produk apa yang paling laku dan berapa jumlahnya?", "knowledge_base_id": "kb-uji"}'
# -> "computed": {"operation":"top_n","metric":"Jumlah","group_by":"Produk","rows_matched":40,"rows_total":40,...}
```

Kolom yang tidak ada → permintaan ditolak jujur (tidak ada angka karangan). Daftar tabel terstruktur:
`GET /tables`.

## 7. Lihat dari UI

- Konsol chat: `http://localhost:8000/ui/`
- Swagger UI (coba endpoint dari browser): `http://localhost:8000/docs`
- Situs dokumentasi ini: `http://localhost:8000/guide/` (bila `site/` sudah dibangun — lihat di bawah)

```bash
bash scripts/build_docs.sh        # ekspor OpenAPI → generate referensi → mkdocs build → site/
```

## Langkah berikutnya

- [Integrasi klien](integration.md) — contoh kode per bahasa, kode error, pola retry & idempotensi.
- [Deploy & menjalankan](deployment.md) — profil sumber daya, systemd, nginx, backup, troubleshooting.
