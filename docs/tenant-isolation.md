# Tenant Isolation

PRD 6, 13, 19, 21, 34. Aturannya satu: **`organization_id` selalu berasal dari trusted
context, tidak pernah dari permintaan.**

## Di mana tenant ditentukan

| Sumber | Cara | Diterima? |
|---|---|---|
| `Authorization: Bearer <key>` | dipetakan ke entri `API_KEYS_JSON` | ✅ |
| `X-Tenant-Context` (HS256 dari KMS) | diverifikasi dengan `KMS_SHARED_SECRET` | ✅ |
| body request (`organization_id`, `org_id`, `tenant_id`) | — | ❌ 422 sebelum kerja apa pun |
| body `/knowledge/index` (KMS) | hanya jika **sama** dengan trusted context | ⚠️ 403 jika berbeda |
| keluaran Jev | field tenant dibuang (`stripped_tenant_fields`) | ❌ diabaikan |

`TrustedContext.as_dict()` hanya berisi `user_id`, `organization_id`, `application_id`,
`permissions` — dan yang dikirim ke Jev hanya sinyal klasifikasi non-rahasia
(`application`, `surface`, `has_tenant_context`), bukan identitas.

## Lapisan pertahanan (berurutan)

1. **Validasi skema** — `extra="forbid"` pada model request; field tenant yang dikirim
   klien → 422 `VALIDATION_ERROR` (bukan diabaikan diam-diam).
2. **Guard body** — `reject_client_tenant_fields` memindai body secara rekursif
   (termasuk `options.*`) sebagai pertahanan kedua.
3. **Filter retrieval** — `qdrant/collections.py:tenant_filter()` adalah satu-satunya
   tempat filter dibuat, dan `must` selalu memuat `organization_id`; `knowledge_base_id`
   ikut bila ada.
4. **Re-read payload** — `repository.get_chunks_by_ids()` melakukan scroll dengan filter
   tenant + `chunk_id MatchAny`, jadi chunk lintas organisasi tidak bisa masuk ke konteks
   walaupun ada tabrakan id. Hasilnya di-kunci `"{document_id}::{chunk_id}"`, bukan
   `chunk_id` saja: setiap dokumen memulai penomoran dari `chunk_0001`, dan peta yang
   di-kunci `chunk_id` membuang salah satu dari dua dokumen yang sama-sama cocok.
5. **Index lexikal terpisah** — BM25 disimpan per `(org, kb)`; secara struktural tidak ada
   jalur kueri lintas tenant.
6. **Operasi destruktif ter-scope** — `DELETE` menghapus berdasarkan filter
   `organization_id + document_id`; dokumen milik tenant lain → 404 tanpa memberi tahu
   apakah dokumen itu ada.
7. **Log** — `organization_id`/`application_id` dicatat; API key/token tidak pernah
   (redaksi `redact()` + pola `jev_*`, `Bearer ...`, `sk-*`).

## Bukti (test yang mengikat, bukan asumsi)

`tests/integration/test_tenant_isolation.py`:

```
test_org_b_cannot_see_org_a_knowledge            -> sources == [] , grounded == false
test_org_a_gets_its_own_knowledge_with_citations -> sitasi hanya doc miliknya
test_client_supplied_organization_id_is_rejected_on_query -> 422
test_indexing_with_a_foreign_organization_id_is_forbidden  -> 403
test_read_only_key_cannot_index_or_delete        -> 403
test_missing_or_invalid_credentials_are_refused_early -> 401
test_org_b_cannot_delete_org_a_document          -> 404 dan dokumen tetap hidup
test_document_status_is_not_readable_across_tenants -> 404
test_kms_context_token_provides_the_tenant       -> token valid → org_kms
test_forged_context_token_is_rejected            -> AUTH_INVALID
```

Semua skenario di atas dijalankan pada Qdrant nyata (embedded) dengan dokumen
benar-benar ter-index — bukan mock pencarian.

## Yang belum tercakup

- Rotasi/kedaluwarsa API key otomatis (PRD 44 menyebut rotasi; di sini key statis di `.env`).
- Rate limit per tenant bersifat in-process (satu replika).
- Enkripsi at-rest Qdrant (volume) belum diatur; andalkan enkripsi disk/volume.
- Audit trail perubahan dokumen (siapa mengubah apa) belum ada tabel tersendiri; hanya log
  akses yang memuat `user_id`/`application_id`.
