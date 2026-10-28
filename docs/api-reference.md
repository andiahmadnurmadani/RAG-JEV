<!-- BERKAS HASIL GENERATE: dibuat oleh scripts/gen_api_reference.py dari docs/openapi.json. Jangan disunting manual. -->

# Referensi API (hasil generate)

Berkas ini dibuat dari `docs/openapi.json` — **jangan disunting manual**.

- Judul: **Multi-Tenant RAG & Jev AI Service**
- Versi: `1.0.0` · OpenAPI `3.1.0`

Multi-tenant knowledge indexing, hybrid retrieval and grounded generation. Tenant comes from the trusted context; Jev orchestrates capability only.

Untuk mencoba langsung dari browser: [Swagger UI](/docs) · [ReDoc](/redoc) · [`openapi.json`](openapi.json) · [`openapi.yaml`](openapi.yaml)

## Daftar endpoint

| Metode | Path | Ringkasan |
|---|---|---|
| `GET` | `/api/v1/auth/gate` | Read Gate |
| `POST` | `/api/v1/auth/login` | Login |
| `POST` | `/api/v1/auth/logout` | Logout Post |
| `DELETE` | `/api/v1/auth/session` | Logout |
| `GET` | `/api/v1/auth/session` | Read Session |
| `POST` | `/api/v1/extract` | Extract |
| `GET` | `/api/v1/health` | Health |
| `GET` | `/api/v1/knowledge` | List Knowledge |
| `GET` | `/api/v1/knowledge-bases` | List Knowledge Bases |
| `DELETE` | `/api/v1/knowledge-bases/{knowledge_base_id}` | Delete Knowledge Base |
| `POST` | `/api/v1/knowledge/index` | Index Knowledge |
| `DELETE` | `/api/v1/knowledge/{document_id}` | Delete Knowledge |
| `GET` | `/api/v1/knowledge/{document_id}` | Knowledge Status |
| `PUT` | `/api/v1/knowledge/{document_id}` | Update Knowledge |
| `DELETE` | `/api/v1/logs/requests` | Clear Requests |
| `GET` | `/api/v1/logs/requests` | List Requests |
| `GET` | `/api/v1/metrics` | Metrics |
| `POST` | `/api/v1/query` | Query |
| `GET` | `/api/v1/ready` | Ready |
| `POST` | `/api/v1/search` | Search |
| `GET` | `/api/v1/settings` | Read Settings |
| `PUT` | `/api/v1/settings` | Update Settings |
| `DELETE` | `/api/v1/settings/access` | Clear Access Code |
| `GET` | `/api/v1/settings/access` | Read Access |
| `PUT` | `/api/v1/settings/access` | Set Access Code |
| `DELETE` | `/api/v1/settings/access/sessions` | Revoke All Sessions |
| `DELETE` | `/api/v1/settings/access/sessions/{session_id}` | Revoke One Session |
| `GET` | `/api/v1/settings/api-keys` | List Api Keys |
| `POST` | `/api/v1/settings/api-keys` | Create Api Key |
| `DELETE` | `/api/v1/settings/api-keys/{key_id}` | Revoke Api Key |
| `POST` | `/api/v1/settings/jev/probe` | Probe Jev |
| `POST` | `/api/v1/settings/llm/models` | List Llm Models |
| `GET` | `/api/v1/tables` | List Tables |
| `DELETE` | `/api/v1/unanswered` | Clear Unanswered |
| `GET` | `/api/v1/unanswered` | List Unanswered |
| `PATCH` | `/api/v1/unanswered` | Update Unanswered |
| `POST` | `/api/v1/unanswered/delete` | Delete Unanswered |
| `POST` | `/api/v1/vision` | Vision |

Total: 38 operasi.

## Detail

## `/api/v1/auth/gate`

### `GET /api/v1/auth/gate`

**Read Gate**

Apakah konsol terkunci dan berapa lama sesi berlaku. Tidak membocorkan kode.

Tag: auth

| Kode | Arti |
|---|---|
| 200 | Successful Response |


## `/api/v1/auth/login`

### `POST /api/v1/auth/login`

**Login**

Tukar kode akses dengan sesi. Kode salah ditolak sebelum sesi apa pun dibuat.

Tag: auth

Body `application/json`:

Skema: `LoginRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `code` | string | ya |  |
| `remember` | boolean | — | (default: `False`) |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/auth/logout`

### `POST /api/v1/auth/logout`

**Logout Post**

Sama seperti ``DELETE /auth/session``; disediakan untuk klien yang hanya bisa POST.

Tag: auth

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/auth/session`

### `DELETE /api/v1/auth/session`

**Logout**

Keluar: sesi yang sedang dipakai dicabut. Kunci API tidak bisa 'keluar' di sini.

Tag: auth

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `GET /api/v1/auth/session`

**Read Session**

Siapa yang sedang masuk: kunci API, sesi konsol, atau token KMS.

Tag: auth

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/extract`

### `POST /api/v1/extract`

**Extract**

Tag: extract

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `ExtractRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `output_schema` | object | null | — |  |
| `response_format` | ResponseFormat | null | — |  |
| `top_k` | integer | — | (default: `10`) |
| `document_ids` | array<string> | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/health`

### `GET /api/v1/health`

**Health**

Liveness: the process is up. Never touches a dependency (PRD 27).

Tag: system

| Kode | Arti |
|---|---|
| 200 | Successful Response |


## `/api/v1/knowledge`

### `GET /api/v1/knowledge`

**List Knowledge**

List this organization's documents (newest first).

There is no ``organization_id`` parameter on purpose: a listing endpoint that accepted
one would be the easiest place in the service to leak another tenant's catalogue.

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `knowledge_base_id` | query | string | null | — |  |
| `include_deleted` | query | boolean | — |  |
| `limit` | query | integer | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/knowledge-bases`

### `GET /api/v1/knowledge-bases`

**List Knowledge Bases**

Semua knowledge base organisasi pemanggil, dengan jumlah dokumen/potongan dan status.

Knowledge base tidak dibuat terpisah - ia muncul begitu dokumen pertamanya diindeks. Daftar
ini menggabungkan catatan pengindeksan (status, waktu) dengan indeks kata kunci (jumlah
potongan yang benar-benar bisa dicari), sehingga KB lama yang catatan job-nya sudah tidak
lengkap tetap terlihat. Kunci yang diikat ke KB tertentu hanya melihat KB miliknya.

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/knowledge-bases/{knowledge_base_id}`

### `DELETE /api/v1/knowledge-bases/{knowledge_base_id}`

**Delete Knowledge Base**

Hapus satu knowledge base beserta SELURUH dokumennya (vektor, indeks kata kunci, tabel).

Tidak bisa dibatalkan, jadi pemanggil wajib mengulang nama KB di ``?confirm=`` - salah ketik
atau tombol yang terpencet tidak menghapus apa pun. Hanya KB milik organisasi pemanggil yang
tersentuh; kunci yang diikat ke KB tertentu hanya bisa menghapus KB miliknya.

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `knowledge_base_id` | path | string | ya |  |
| `confirm` | query | string | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/knowledge/index`

### `POST /api/v1/knowledge/index`

**Index Knowledge**

Enqueue an indexing job (PRD 33: 202 Accepted, work happens in the worker).

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `KnowledgeIndexRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `document_id` | string | ya |  |
| `knowledge_base_id` | string | ya |  |
| `organization_id` | string | null | — |  |
| `document_name` | string | null | — |  |
| `file_url` | string | null | — |  |
| `web_url` | string | null | — |  |
| `web_max_pages` | integer | — | (default: `0`) |
| `web_max_depth` | integer | — | (default: `-1`) |
| `web_follow_files` | boolean | — | (default: `True`) |
| `content_base64` | string | null | — |  |
| `text` | string | null | — |  |
| `language` | string | null | — |  |
| `metadata` | object | — |  |
| `replace` | boolean | — | (default: `True`) |

| Kode | Arti |
|---|---|
| 202 | Successful Response |
| 422 | Validation Error |


## `/api/v1/knowledge/{document_id}`

### `DELETE /api/v1/knowledge/{document_id}`

**Delete Knowledge**

Delete every chunk/vector of a document inside the caller's organization.

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `document_id` | path | string | ya |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `GET /api/v1/knowledge/{document_id}`

**Knowledge Status**

Job status for one document, scoped to the caller's organization.

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `document_id` | path | string | ya |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `PUT /api/v1/knowledge/{document_id}`

**Update Knowledge**

Re-index a document: old vectors are removed before the new ones land (PRD 32).

Tag: knowledge

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `document_id` | path | string | ya |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `KnowledgeUpdateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `document_id` | string | ya |  |
| `knowledge_base_id` | string | ya |  |
| `organization_id` | string | null | — |  |
| `document_name` | string | null | — |  |
| `file_url` | string | null | — |  |
| `web_url` | string | null | — |  |
| `web_max_pages` | integer | — | (default: `0`) |
| `web_max_depth` | integer | — | (default: `-1`) |
| `web_follow_files` | boolean | — | (default: `True`) |
| `content_base64` | string | null | — |  |
| `text` | string | null | — |  |
| `language` | string | null | — |  |
| `metadata` | object | — |  |
| `replace` | boolean | — | (default: `True`) |

| Kode | Arti |
|---|---|
| 202 | Successful Response |
| 422 | Validation Error |


## `/api/v1/logs/requests`

### `DELETE /api/v1/logs/requests`

**Clear Requests**

Kosongkan log permintaan. Wajib ``?confirm=hapus``.

Tag: logs

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `confirm` | query | string | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `GET /api/v1/logs/requests`

**List Requests**

Permintaan terbaru lebih dulu, plus ringkasan (asal teratas, jalur teratas, per waktu).

``after_id`` dipakai mode Live: hanya baris dengan id lebih besar, biasanya dengan ``summary=false``.

Tag: logs

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `minutes` | query | integer | — |  |
| `status` | query | enum(`2xx`, `3xx`, `4xx`, `5xx`, `429`, `errors`) | null | — |  |
| `q` | query | string | — |  |
| `client_ip` | query | string | null | — |  |
| `key_id` | query | string | null | — |  |
| `application_id` | query | string | null | — |  |
| `path` | query | string | null | — |  |
| `knowledge_base_id` | query | string | null | — |  |
| `limit` | query | integer | — |  |
| `offset` | query | integer | — |  |
| `after_id` | query | integer | — |  |
| `summary` | query | boolean | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/metrics`

### `GET /api/v1/metrics`

**Metrics**

PRD 37 counters/latencies plus the honest state of the worker and indexes.

Butuh kredensial operator: ``sparse_scopes`` memuat nama organisasi dan knowledge base
SEMUA tenant.

Tag: system

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/query`

### `POST /api/v1/query`

**Query**

Tag: query

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `QueryRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `options` | QueryOptions | — |  |
| `history` | array<HistoryTurn> | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/ready`

### `GET /api/v1/ready`

**Ready**

Readiness: qdrant, embedding, reranker, llm, jev (PRD 28).

Tag: system

| Kode | Arti |
|---|---|
| 200 | Successful Response |


## `/api/v1/search`

### `POST /api/v1/search`

**Search**

Tag: search

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `SearchRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `top_k` | integer | — | (default: `10`) |
| `options` | QueryOptions | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings`

### `GET /api/v1/settings`

**Read Settings**

Masked current configuration (env + stored overrides).

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `PUT /api/v1/settings`

**Update Settings**

Persist overrides, apply them to the live process, and report what changed.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `SettingsUpdateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `llm` | object | null | — |  |
| `jev` | object | null | — |  |
| `uploads` | object | null | — |  |
| `retrieval` | object | null | — |  |
| `web` | object | null | — |  |
| `summary` | object | null | — |  |
| `unanswered` | object | null | — |  |
| `embedding` | object | null | — |  |
| `limits` | object | null | — |  |
| `vision` | object | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/access`

### `DELETE /api/v1/settings/access`

**Clear Access Code**

Matikan kode akses: konsol kembali hanya bisa dibuka dengan API key.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `GET /api/v1/settings/access`

**Read Access**

Apakah konsol terkunci, sesi mana yang aktif, dan berapa lama sesi bertahan.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `PUT /api/v1/settings/access`

**Set Access Code**

Pasang atau ganti kode akses konsol. Mengganti kode langsung mematikan semua sesi lama.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `AccessCodeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `code` | string | ya |  |
| `current_code` | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/access/sessions`

### `DELETE /api/v1/settings/access/sessions`

**Revoke All Sessions**

Keluarkan semua sesi konsol, termasuk yang sekarang (kecuali diminta menyisakan satu).

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/access/sessions/{session_id}`

### `DELETE /api/v1/settings/access/sessions/{session_id}`

**Revoke One Session**

Keluarkan satu sesi tertentu (mis. perangkat yang hilang).

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `session_id` | path | string | ya |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/api-keys`

### `GET /api/v1/settings/api-keys`

**List Api Keys**

Daftar kunci yang bisa memanggil layanan ini. Nilai kunci tidak pernah ikut.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `POST /api/v1/settings/api-keys`

**Create Api Key**

Buat kunci baru. Nilai kunci dikembalikan **sekali** di sini dan tidak disimpan apa adanya.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `ApiKeyCreateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `label` | string | ya |  |
| `permissions` | array<string> | null | — |  |
| `organization_id` | string | null | — |  |
| `user_id` | string | null | — |  |
| `application_id` | string | null | — |  |
| `expires_in_days` | integer | null | — |  |
| `knowledge_base_ids` | array<string> | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/api-keys/{key_id}`

### `DELETE /api/v1/settings/api-keys/{key_id}`

**Revoke Api Key**

Cabut kunci dari registry. Kunci dari env ditolak di sini (dikelola lewat API_KEYS_JSON).

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `key_id` | path | string | ya |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/jev/probe`

### `POST /api/v1/settings/jev/probe`

**Probe Jev**

One cheap decision question: does this Jev endpoint answer, and how fast?

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `JevProbeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `provider` | string | null | — |  |
| `url` | string | null | — |  |
| `model` | string | null | — |  |
| `api_key` | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/settings/llm/models`

### `POST /api/v1/settings/llm/models`

**List Llm Models**

Ask an OpenAI-compatible endpoint which models it serves.

Tag: settings

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `ModelsProbeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `base_url` | string | null | — |  |
| `api_key` | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/tables`

### `GET /api/v1/tables`

**List Tables**

Tabel terstruktur yang tersedia di scope pemanggil (untuk perhitungan agregat).

Tag: query

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `knowledge_base_id` | query | string | null | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/unanswered`

### `DELETE /api/v1/unanswered`

**Clear Unanswered**

Hapus semua yang cocok dengan filter. Wajib ``?confirm=hapus``.

Tag: unanswered

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `confirm` | query | string | — |  |
| `knowledge_base_id` | query | string | null | — |  |
| `status` | query | enum(`open`, `resolved`) | null | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `GET /api/v1/unanswered`

**List Unanswered**

Pertanyaan yang berakhir "tidak ditemukan", terbaru atau tersering lebih dulu.

Tag: unanswered

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `knowledge_base_id` | query | string | null | — |  |
| `status` | query | enum(`open`, `resolved`) | null | — |  |
| `q` | query | string | — |  |
| `sort` | query | enum(`recent`, `frequent`) | — |  |
| `limit` | query | integer | — |  |
| `offset` | query | integer | — |  |
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


### `PATCH /api/v1/unanswered`

**Update Unanswered**

Tag: unanswered

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `UnansweredUpdate`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `ids` | array<integer> | ya |  |
| `status` | enum(`open`, `resolved`) | null | — |  |
| `note` | string | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/unanswered/delete`

### `POST /api/v1/unanswered/delete`

**Delete Unanswered**

Tag: unanswered

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `UnansweredDelete`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `ids` | array<integer> | ya |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## `/api/v1/vision`

### `POST /api/v1/vision`

**Vision**

Gambar + instruksi -> teks, atau JSON yang dijamin sah bila ``response_format`` meminta JSON.

Tidak membaca knowledge dan tidak menyimpan gambar: cukup izin ``read`` pada kunci API.

Tag: vision

| Parameter | Di | Tipe | Wajib | Keterangan |
|---|---|---|---|---|
| `authorization` | header | string | null | — |  |
| `X-Tenant-Context` | header | string | null | — |  |

Body `application/json`:

Skema: `VisionRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `images` | array<VisionImageIn> | ya |  |
| `query` | string | ya |  |
| `response_format` | ResponseFormat | null | — |  |
| `max_tokens` | integer | null | — |  |

| Kode | Arti |
|---|---|
| 200 | Successful Response |
| 422 | Validation Error |


## Skema

### `AccessCodeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `code` | string | ya |  |
| `current_code` | string | null | — |  |

### `ApiKeyCreateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `label` | string | ya |  |
| `permissions` | array<string> | null | — |  |
| `organization_id` | string | null | — |  |
| `user_id` | string | null | — |  |
| `application_id` | string | null | — |  |
| `expires_in_days` | integer | null | — |  |
| `knowledge_base_ids` | array<string> | null | — |  |

### `ExtractRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `output_schema` | object | null | — |  |
| `response_format` | ResponseFormat | null | — |  |
| `top_k` | integer | — | (default: `10`) |
| `document_ids` | array<string> | null | — |  |

### `HTTPValidationError`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `detail` | array<ValidationError> | — |  |

### `HistoryTurn`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `role` | enum(`user`, `assistant`) | ya |  |
| `content` | string | ya |  |

### `JevProbeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `provider` | string | null | — |  |
| `url` | string | null | — |  |
| `model` | string | null | — |  |
| `api_key` | string | null | — |  |

### `KnowledgeIndexRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `document_id` | string | ya |  |
| `knowledge_base_id` | string | ya |  |
| `organization_id` | string | null | — |  |
| `document_name` | string | null | — |  |
| `file_url` | string | null | — |  |
| `web_url` | string | null | — |  |
| `web_max_pages` | integer | — | (default: `0`) |
| `web_max_depth` | integer | — | (default: `-1`) |
| `web_follow_files` | boolean | — | (default: `True`) |
| `content_base64` | string | null | — |  |
| `text` | string | null | — |  |
| `language` | string | null | — |  |
| `metadata` | object | — |  |
| `replace` | boolean | — | (default: `True`) |

### `KnowledgeUpdateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `document_id` | string | ya |  |
| `knowledge_base_id` | string | ya |  |
| `organization_id` | string | null | — |  |
| `document_name` | string | null | — |  |
| `file_url` | string | null | — |  |
| `web_url` | string | null | — |  |
| `web_max_pages` | integer | — | (default: `0`) |
| `web_max_depth` | integer | — | (default: `-1`) |
| `web_follow_files` | boolean | — | (default: `True`) |
| `content_base64` | string | null | — |  |
| `text` | string | null | — |  |
| `language` | string | null | — |  |
| `metadata` | object | — |  |
| `replace` | boolean | — | (default: `True`) |

### `LoginRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `code` | string | ya |  |
| `remember` | boolean | — | (default: `False`) |

### `ModelsProbeRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `base_url` | string | null | — |  |
| `api_key` | string | null | — |  |

### `QueryOptions`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `top_k` | integer | null | — |  |
| `strict_grounding` | boolean | — | (default: `True`) |
| `include_sources` | boolean | — | (default: `True`) |
| `use_hybrid` | boolean | null | — |  |
| `use_reranker` | boolean | null | — |  |
| `threshold` | number | null | — |  |
| `route` | string | null | — |  |
| `document_ids` | array<string> | null | — |  |
| `table_analytics` | boolean | null | — |  |

### `QueryRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `options` | QueryOptions | — |  |
| `history` | array<HistoryTurn> | null | — |  |

### `ResponseFormat`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `type` | enum(`text`, `json_object`, `json_schema`) | — | (default: `json_object`) |
| `json_schema` | object | null | — |  |

### `SearchRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `query` | string | ya |  |
| `knowledge_base_id` | string | null | — |  |
| `top_k` | integer | — | (default: `10`) |
| `options` | QueryOptions | null | — |  |

### `SettingsUpdateRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `llm` | object | null | — |  |
| `jev` | object | null | — |  |
| `uploads` | object | null | — |  |
| `retrieval` | object | null | — |  |
| `web` | object | null | — |  |
| `summary` | object | null | — |  |
| `unanswered` | object | null | — |  |
| `embedding` | object | null | — |  |
| `limits` | object | null | — |  |
| `vision` | object | null | — |  |

### `UnansweredDelete`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `ids` | array<integer> | ya |  |

### `UnansweredUpdate`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `ids` | array<integer> | ya |  |
| `status` | enum(`open`, `resolved`) | null | — |  |
| `note` | string | null | — |  |

### `ValidationError`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `loc` | array<string | integer> | ya |  |
| `msg` | string | ya |  |
| `type` | string | ya |  |
| `input` | any | — |  |
| `ctx` | object | — |  |

### `VisionImageIn`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `data` | string | ya |  |
| `mime` | string | null | — |  |

### `VisionRequest`

| Field | Tipe | Wajib | Keterangan |
|---|---|---|---|
| `images` | array<VisionImageIn> | ya |  |
| `query` | string | ya |  |
| `response_format` | ResponseFormat | null | — |  |
| `max_tokens` | integer | null | — |  |
