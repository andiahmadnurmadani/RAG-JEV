"""`_html_to_text` tidak boleh meloloskan boilerplate navigasi/ikon sebagai knowledge.

Latar: audit knowledge produksi menemukan potongan berisi label tombol seperti
"Arrow right" yang tidak relevan dengan isi dokumen manapun. Penyebabnya: `_html_to_text`
hanya melucuti tag `script/style/head`, lalu membuang SEMUA tag lain sambil mempertahankan
teks anaknya - termasuk teks aksesibilitas yang sengaja disembunyikan secara visual
(`sr-only`, `aria-hidden="true"`) dan boilerplate navigasi (`<nav>`, `<button>`, `<svg>`,
`<footer>`) yang umum dipakai situs modern untuk carousel/paginasi/menu.
"""

from __future__ import annotations

from app.parsing.parser import _html_to_text


def test_label_tombol_tersembunyi_dibuang():
    html = (
        '<div class="carousel">'
        '<button class="slick-prev"><span class="sr-only">Arrow left</span></button>'
        '<div class="slide"><p>Promo diskon 50%</p></div>'
        '<button class="slick-next"><span class="sr-only">Arrow right</span>'
        '<svg aria-hidden="true"><path/></svg></button>'
        "</div>"
    )
    text = _html_to_text(html)
    assert "Arrow" not in text
    assert "Promo diskon 50%" in text


def test_svg_title_ikon_dibuang():
    html = '<svg role="img"><title>Arrow Right</title><path d="M0 0"/></svg><p>Isi sah</p>'
    text = _html_to_text(html)
    assert "Arrow" not in text
    assert "Isi sah" in text


def test_aria_hidden_dibuang_tanpa_merusak_kalimat_di_sekitarnya():
    html = (
        "<p>Dokumen SOP <b>Cuti Tahunan</b>: pengajuan maksimal "
        '<span class="sr-only">(lihat ikon info)</span>H-3.</p>'
    )
    text = _html_to_text(html)
    assert "ikon info" not in text
    assert "Dokumen SOP Cuti Tahunan: pengajuan maksimal" in text
    assert "H-3." in text


def test_nav_header_footer_dibuang_isi_utama_tetap_ada():
    html = (
        "<html><body>"
        '<nav class="breadcrumb"><a href="/">Home</a> / <a href="/a">A</a></nav>'
        "<main><h1>Judul</h1><p>Isi dokumen yang sah.</p></main>"
        "<footer>Copyright 2026 PT Contoh</footer>"
        "</body></html>"
    )
    text = _html_to_text(html)
    assert "Home" not in text
    assert "Copyright" not in text
    assert "Judul" in text
    assert "Isi dokumen yang sah." in text


def test_form_dan_tombol_kirim_dibuang():
    html = '<form><button type="submit">Kirim</button></form><p>Isi setelah form tetap ada</p>'
    text = _html_to_text(html)
    assert "Kirim" not in text
    assert "Isi setelah form tetap ada" in text


def test_tag_xlsx_headerfooter_tidak_tertukar_dengan_tag_html_header():
    """``<headerFooter>`` (XML internal XLSX) tidak boleh kena filter tag HTML ``<header>``."""
    xml = "<headerFooter><oddHeader>Laporan Penjualan</oddHeader></headerFooter>"
    assert _html_to_text(xml) == "Laporan Penjualan"


def test_tabel_tetap_satu_baris_per_baris_tabel():
    """Setiap baris tabel jadi satu baris teks (dulu seluruh tabel menyatu jadi satu baris)."""
    html = "<table><tr><th>Kolom A</th><th>Kolom B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    assert _html_to_text(html) == "Kolom A | Kolom B\n1 | 2"


def test_header_artikel_dengan_judul_tetap_ada():
    """<header> situs dibuang, tetapi <header> artikel yang memuat judul/tanggal adalah isi."""
    html = (
        '<header class="site"><a>Logo PT</a></header>'
        "<article><header><h1>Pengumuman Libur Nasional 2026</h1><time>1 Jan 2026</time></header>"
        "<p>Kantor tutup tanggal 17 Agustus.</p></article>"
    )
    text = _html_to_text(html)
    assert "Pengumuman Libur Nasional 2026" in text
    assert "1 Jan 2026" in text
    assert "Logo PT" not in text


def test_banner_cookie_breadcrumb_dan_tombol_bagikan_dibuang():
    html = (
        '<div class="cookie-banner">Kami memakai cookie untuk pengalaman terbaik.</div>'
        '<ol class="breadcrumb"><li>Beranda</li><li>Berita</li></ol>'
        "<p>Isi berita yang sah.</p>"
        '<div class="share-buttons">Bagikan: Facebook Twitter</div>'
        "<aside>Artikel terkait lain</aside>"
    )
    text = _html_to_text(html)
    assert text == "Isi berita yang sah."


def test_wadah_panjang_bernama_sidebar_tidak_menyeret_isi_utama():
    """Kelas "chrome" hanya dibuang bila teksnya pendek - wadah halaman tidak boleh ikut hilang."""
    body = "Isi utama dokumen yang panjang. " * 40
    html = f'<div class="layout with-sidebar"><p>{body}</p></div>'
    assert "Isi utama dokumen yang panjang." in _html_to_text(html)
