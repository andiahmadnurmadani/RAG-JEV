# SOP Penanganan Insiden Keamanan Informasi

## Pasal 1 - Tujuan dan Ruang Lingkup
SOP ini mengatur tata cara pelaporan, klasifikasi, eskalasi, dan penanganan insiden keamanan
informasi di seluruh unit kerja, termasuk sistem yang dioperasikan oleh pihak ketiga
(hosting, penyedia cloud, dan vendor aplikasi). SOP ini berlaku untuk seluruh pegawai tetap,
pegawai kontrak, dan mitra yang mengakses aset informasi perusahaan.

## Pasal 2 - Definisi
Insiden keamanan informasi adalah setiap kejadian yang mengancam kerahasiaan, keutuhan, atau
ketersediaan aset informasi. Contoh insiden meliputi kebocoran data pelanggan, serangan
ransomware, akses tidak sah ke basis data produksi, kehilangan perangkat kerja, dan
penyalahgunaan hak akses administrator. Kerentanan yang belum dieksploitasi tidak dihitung
sebagai insiden, tetapi dicatat sebagai temuan dan wajib ditutup sesuai tenggat remediasi.

## Pasal 3 - Klasifikasi Tingkat Insiden
Insiden diklasifikasikan menjadi tiga tingkat. Tingkat 1 adalah insiden kritis dengan dampak
lintas unit, potensi pelanggaran regulasi, atau gangguan layanan publik lebih dari delapan
jam. Tingkat 2 adalah insiden mayor yang mengganggu layanan produksi lebih dari empat jam
tetapi tidak menyentuh data pelanggan. Tingkat 3 adalah insiden minor dengan dampak terbatas
pada satu pengguna atau satu perangkat kerja. Penetapan tingkat dilakukan oleh analis SOC
dan dapat dinaikkan (bukan diturunkan) oleh CISO.

## Pasal 4 - Batas Waktu Pelaporan dan Eskalasi
Setiap pegawai wajib melaporkan insiden paling lambat dua jam sejak pertama kali diketahui
melalui kanal resmi security@perusahaan.co.id atau hotline SOC. Insiden Tingkat 1 wajib
dieskalasi ke CISO dalam waktu tiga puluh menit dan kepada regulator paling lambat tiga hari
kerja. Insiden Tingkat 2 dieskalasi ke kepala unit dalam empat jam kerja. Insiden Tingkat 3
cukup dicatat pada sistem tiket dengan tenggat satu hari kerja.

## Pasal 5 - Tim Tanggap Insiden
Tim tanggap insiden terdiri dari analis SOC, pemilik sistem, perwakilan hukum, dan perwakilan
komunikasi korporat. Ketua tim ditunjuk oleh CISO dan bertanggung jawab menyusun laporan
pasca-insiden paling lambat tujuh hari kerja setelah insiden dinyatakan ditutup. Tim wajib
melakukan latihan tanggap insiden minimal dua kali dalam satu tahun.

## Pasal 6 - Penanganan Bukti dan Forensik
Bukti digital wajib dibuat salinan forensik sebelum sistem dipulihkan. Salinan disimpan
terenkripsi pada media terpisah dengan masa retensi minimal tiga tahun. Rantai kustodi
dicatat pada formulir yang memuat waktu, petugas, dan algoritma hash berkas. Pemulihan
sistem dari cadangan hanya boleh dilakukan setelah persetujuan ketua tim.

## Pasal 7 - Komunikasi Krisis
Pernyataan resmi kepada media hanya boleh disampaikan oleh perwakilan komunikasi korporat.
Unit teknis dilarang berkomentar kepada pihak eksternal mengenai detail insiden, termasuk
melalui akun pribadi. Komunikasi kepada pelanggan terdampak disusun bersama perwakilan hukum
dan dikirim paling lambat tiga hari kerja setelah dampak terkonfirmasi.

## Pasal 8 - Sanksi
Keterlambatan pelaporan tanpa alasan yang sah, penghapusan bukti, atau penyembunyian insiden
dikenakan sanksi sesuai peraturan kepegawaian yang berlaku, mulai dari teguran tertulis
sampai pemutusan hubungan kerja. Sanksi bagi mitra dan vendor diatur pada kontrak kerja sama
dan dapat berupa pemutusan kontrak serta klaim biaya pemulihan.

## Pasal 9 - Pelaporan Berkala
Laporan bulanan insiden disusun analis SOC dan diserahkan kepada CISO setiap tanggal lima.
Laporan memuat jumlah insiden per tingkat, waktu deteksi rata-rata, waktu pemulihan rata-rata,
dan status temuan yang masih terbuka. Laporan triwulanan disampaikan kepada komite manajemen
risiko bersama tinjauan efektivitas kontrol.

## Pasal 10 - Tinjauan SOP
SOP ini ditinjau minimal satu kali dalam satu tahun atau lebih cepat jika terjadi perubahan
regulasi, perubahan arsitektur layanan, atau temuan audit yang signifikan.
