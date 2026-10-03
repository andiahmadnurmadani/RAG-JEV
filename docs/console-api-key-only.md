# Konsol dengan kunci API saja (tanpa kode akses)

Bawaannya sekarang: **tempel kunci API, langsung bisa semua** - termasuk layar Pengaturan.
Gerbang kode akses tidak diminta, dan kunci biasa tidak lagi ditolak 403.

## Kenapa ini perlu

Sebelumnya ada dua aturan yang bertabrakan:

* layar Pengaturan (`/settings`, `/settings/api-keys`, `/settings/access`) menuntut izin
  ``admin``;
* kunci yang dibuat dari konsol diberi ``['read', 'write']`` (bukan admin).

Akibatnya operator membuat kunci dari konsol, lalu kunci itu **tidak bisa** membuka konsol -
403 di mana-mana, tanpa jalan keluar selain kunci bootstrap. Di produksi semua kunci memang
hanya ``['read','write']``.

## Cara kerjanya

Setelan ``CONSOLE_API_KEY_ONLY`` (bawaan ``true``):

* ``GET /auth/gate`` mengembalikan ``enabled: false`` (gerbang tidak dipakai) dan
  ``api_key_only: true`` - konsol tahu ia harus meminta **kunci API**, bukan kode akses;
* setiap kunci API yang sah dianggap operator: seluruh layar Pengaturan terbuka (membuat kunci,
  setelan model, retrieval, web, ringkasan, kode akses);
* tanpa kredensial tetap **401** - membuka Pengaturan bukan berarti membuka tanpa kunci.

``GET /auth/gate`` juga mengembalikan ``code_set`` supaya operator tetap bisa melihat apakah
sebuah kode akses pernah dipasang, tanpa gerbangnya aktif.

## Kembali ke mode admin klasik

Setel ``CONSOLE_API_KEY_ONLY=false``. Gerbang kode akses aktif kembali, dan hanya kunci berizin
``admin`` (atau ``*``) yang boleh membuka Pengaturan. Semua uji lama yang mengharapkan 403
kembali berlaku - dan memang masih diuji, dengan mode itu dimatikan.

## Di konsol

Saat ``api_key_only`` menyala dan belum ada kunci tersimpan, konsol langsung membuka panel
**Koneksi** dengan pesan "tempel kunci API sekali" - tidak ada layar kode akses yang muncul
padahal tidak akan menerima apa pun.

## Verifikasi

```bash
# kedua mode diuji berdampingan
.venv/Scripts/python.exe -m pytest \
  tests/integration/test_settings_api.py \
  tests/integration/test_api_keys_api.py \
  tests/integration/test_access_gate.py -q
```
