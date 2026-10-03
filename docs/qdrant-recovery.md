# Koleksi Qdrant rusak: gejala, akar, dan pemulihan

Insiden nyata yang melahirkan dokumen ini: konsol mengembalikan **500** dan **seluruh pencarian
gagal**, padahal kunci API sudah benar.

## Gejala

```
GET /api/v1/ready   -> 500 {"code":"INTERNAL_ERROR"}
GET /api/v1/search  -> 500 {"code":"RETRIEVAL_FAILED",
                             "message":"Qdrant search failed: index 5431 is out of bounds
                                        for axis 0 with size 5431"}
```

Di log server, galat aslinya:

```
File "/srv/app/qdrant/collections.py", line 70, in collection_info
    info = client.get_collection(name)
  ...
  File "qdrant_client/local/local_collection.py", line 665, in _payload_and_non_deleted_mask
    mask = payload_mask & ~self.deleted
ValueError: operands could not be broadcast together with shapes (5432,) (5431,)
```

## Akar masalah

Klien Qdrant **embedded** (``QDRANT_URL`` kosong, ``path=``) menyimpan koleksi di memori dan
**tidak thread-safe**. Setelah ringkasan dokumen mulai berjalan di worker terpisah (agar unggahan
kecil tidak menunggu - lihat `document-summary.md`), ada **dua penulis** pada koleksi yang sama:
pengindeksan dan ringkasan.

Dua penulisan bersamaan merusak struktur internal koleksi: panjang mask `deleted` tidak lagi sama
dengan jumlah titik (5431 vs 5432). Kerusakan itu **permanen** - setiap pembacaan berikutnya gagal,
bukan sekadar galat sesaat. Karena `/ready` pun membaca koleksi, health check-nya juga 500.

Jadi ada dua hal yang bertemu: perbaikan antrian (benar) membuka jalan bagi bug thread-safety
yang selama ini tersembunyi.

## Perbaikan

1. **Serialisasi di mode lokal** (`app/qdrant/client.py`). Mode lokal kini mengembalikan
   ``SerialisedClient`` yang menjalankan setiap panggilan di bawah satu kunci re-entrant, sehingga
   hanya satu penulis/ pembaca menyentuh koleksi pada satu waktu. Mode server (`QDRANT_URL` diisi)
   tidak dibungkus - klien server sudah thread-safe, dan serialisasi akan membuang concurrency
   yang memang diinginkan di produksi.
2. **Restart membangun ulang indeks dari disk.** Titik-titik tersimpan di
   ``collection/<nama>/storage.sqlite``; yang rusak adalah struktur indeks di memori/berkas
   turunannya. Karena itu **me-restart layanan sudah cukup** untuk koleksi yang datanya utuh:
   indeks dibangun ulang dari titik yang tersimpan.

## Urutan pemulihan yang disarankan

```bash
# 1. Backup dulu (selalu).
docker exec <container> sh -c "cd /data && tar czf /tmp/backup.tgz qdrant sparse jobs.json"
docker cp <container>:/tmp/backup.tgz ./backup-$(date +%s).tgz

# 2. Restart layanan - indeks dibangun ulang dari storage.sqlite.
#    (Dokploy: redeploy/restart aplikasi.)

# 3. Verifikasi.
curl -s https://<host>/api/v1/ready | grep -o '"points":[0-9]*'
curl -s -X POST https://<host>/api/v1/search \
  -H "Authorization: Bearer <key>" -H 'Content-Type: application/json' \
  -d '{"query":"uji","knowledge_base_id":"kb_chat","options":{"top_k":3}}'
```

Bila restart **tidak** cukup (titiknya sendiri rusak), pakai `scripts/recover_local_qdrant.py`:
ia membaca setiap titik langsung dari `storage.sqlite` (pickle Qdrant), membangun koleksi baru di
**salinan** folder, lalu memverifikasi jumlah titik dan satu query nyata sebelum dipakai.

Catatan teknis yang menghemat waktu saat menulis alat itu: kolom ``id`` di tabel ``points``
disimpan sebagai ``base64(pickle(uuid))`` - **jangan** dipakai apa adanya sebagai ID titik
(Qdrant menolak dengan "not a valid UUID"); ambil ``record.id`` dari record pickle-nya.

## Uji yang mengunci

`tests/integration/test_qdrant_thread_safety.py`:

* mode lokal wajib memakai ``SerialisedClient``;
* pembungkus meneruskan atribut non-fungsi apa adanya dan memanggil yang fungsi;
* **6 penulis + 3 pembaca bersamaan** (300 titik), lalu memastikan pembacaan tetap konsisten -
  tanpa serialisasi, uji ini gagal dan koleksinya rusak.

Uji itu dibuktikan menangkap bug: dengan ``SerialisedClient`` dilepas sementara, ujinya gagal
dengan pesan "klien Qdrant lokal tidak diserialkan: penulisan bersamaan akan merusak koleksi".

## Pencegahan yang lebih baik: pakai Qdrant server

Mode lokal adalah kemudahan pengembangan. Untuk produksi multi-pengguna, isi ``QDRANT_URL`` agar
memakai Qdrant server: ia thread-safe, tahan proses mati mendadak, dan tidak lagi menyimpan
seluruh koleksi di memori proses aplikasi.
