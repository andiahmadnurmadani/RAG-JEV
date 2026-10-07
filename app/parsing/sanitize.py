"""Sanitasi teks hasil parsing dan penjagaan aksara asing (PRD 8, 36).

Tiga masalah nyata yang dijawab berkas ini, semuanya ditemukan pada data produksi:

1. **Sampah biner lolos jadi teks.** Sebuah PDF di web disajikan tanpa ``content-type`` yang
   benar, sehingga isinya di-decode sebagai teks dan byte mentahnya masuk ke Qdrant
   (``%PDF-1.4``, ``endstream``, ribuan karakter pengganti). Sampah seperti itu tidak bisa
   dicari, tidak bisa dibaca, dan mencemari jawaban.
2. **Sisa kontrol.** Berkas lama (Word/RTF/Excel) kadang membawa NUL, BEL, dan karakter
   pengganti Unicode yang membuat potongan tampak rusak.
3. **Aksara dari bahasa lain yang diselipkan model.** Model gratis kadang menyisipkan aksara
   Han ke tengah kalimat Indonesia (``dokumen finals完整的``). Faktanya benar, tapi pemakai
   melihat teks asing.

Aturan penting yang membedakan modul ini dari filter "buang semua CJK": **teks asing hanya
dibuang bila ia tidak ada di sumber.** Dokumen yang memang memuat aksara Han (mis. label
skema, kutipan asing, nama diri) harus tetap utuh - itu fakta dokumen, bukan kesalahan.
"""

from __future__ import annotations

import re
from typing import Iterable, Tuple

# ---------------------------------------------------------------- aksara & sampah

# Aksara Han, Hiragana, Katakana, Hangul. Dipakai untuk MENDETEKSI, bukan untuk membuang.
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# Blok aksara yang jelas bukan bahasa Indonesia/Inggris: Arab, Ibrani, Thai, Devanagari,
# Kiril, dan aksara Asia Timur di atas.
FOREIGN_SCRIPT_RE = re.compile(
    r"[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f"
    r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
# Karakter pengganti: tanda bahwa decode gagal. Satu-dua masih wajar (berkas rusak sebagian),
# banyak berarti isinya memang bukan teks.
REPLACEMENT_CHAR = "\ufffd"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Jejak berkas biner yang isinya terlanjur di-decode sebagai teks.
BINARY_LEFTOVER_RE = re.compile(
    r"%PDF-\d|%!PS|endobj|endstream|stream\s*\n|/FlateDecode|/Filter\b|obj\s*<<"
    r"|PK\x03\x04|\x89PNG|GIF8[79]a|BM\x00{2}|RIFF....WEBP|OggS|\x1f\x8b\x08"
)
_WS_RUN_RE = re.compile(r"[ \t]{2,}")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def looks_like_binary_garbage(text: str, *, max_replacement_ratio: float = 0.10) -> Tuple[bool, str]:
    """Apakah teks ini sebenarnya isi berkas biner yang gagal di-parse?

    Mengembalikan ``(True, alasan)`` bila ya. Ambangnya sengaja tidak terlalu ketat: teks
    normal yang memuat satu-dua karakter aneh tetap lolos, sedangkan dump PDF/arsip - yang
    ditandai ribuan karakter pengganti dan puluhan kata kunci format - langsung tertangkap.

    Satu kemunculan kata kunci format TIDAK cukup: dokumen yang memang membahas format PDF
    akan menyebut ``%PDF`` beberapa kali secara sah. Yang menentukan adalah **banyaknya** jejak
    biner, karena dump asli memuatnya puluhan kali (``endobj``, ``stream``, ``FlateDecode``).
    """
    if not text:
        return False, ""
    sample = text[:4000]
    replacement = sample.count(REPLACEMENT_CHAR)
    controls = len(_CONTROL_RE.findall(sample))
    if replacement / len(sample) > max_replacement_ratio:
        return True, f"banyak karakter pengganti ({replacement})"
    if controls > 20:
        return True, f"banyak karakter kontrol ({controls})"
    markers = len(BINARY_LEFTOVER_RE.findall(sample))
    # Ambang 4: satu-dua sebutan format wajar di dokumen teknis, tetapi isi berkas biner
    # memuat penanda ini berkali-kali.
    if markers >= 4:
        return True, f"jejak berkas biner (PDF/arsip/gambar) di dalam teks ({markers} penanda)"
    if markers >= 2 and (replacement or controls > 5):
        return True, "jejak berkas biner disertai karakter rusak"
    return False, ""


def clean_text(text: str) -> str:
    """Buang karakter kontrol dan rapikan spasi berlebih tanpa mengubah isi.

    Karakter pengganti (``\\ufffd``) sengaja TIDAK dibuang di sini: ia penanda jujur bahwa
    decode gagal sebagian, dan menghapusnya bisa menyambung dua kata yang seharusnya terpisah.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = _WS_RUN_RE.sub(" ", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


# ---------------------------------------------------------------- aksara asing

def foreign_scripts(text: str) -> set:
    """Aksara asing apa saja yang muncul di teks (untuk dilaporkan, bukan untuk dibuang)."""
    return {match.group(0) for match in FOREIGN_SCRIPT_RE.finditer(text or "")}


def foreign_tokens(text: str, *, allowed: Iterable[str] = ()) -> list:
    """Potongan teks yang memuat aksara asing DAN tidak ada di sumber (``allowed``).

    Inilah yang boleh dibuang dari jawaban model: kata/aksara asing yang model selipkan,
    bukan kutipan dari dokumen. Bila dokumen memang memuatnya, bentuk itu ada di ``allowed``
    (teks konteks) dan dibiarkan utuh.

    Yang dikembalikan adalah **hanya aksara asingnya**, bukan kata Latin yang menempel
    padanya. Ini penting: pada ``perakitan完整的`` kata ``perakitan`` harus tetap ada - kalau
    rentangnya diperlebar ke huruf Latin di sekitarnya, kata yang sah ikut terhapus.
    """
    if not text:
        return []
    haystack = " ".join(allowed or [])
    hasil = []
    for match in FOREIGN_SCRIPT_RE.finditer(text):
        # Ambil rentang AKSARA ASING saja (beserta tanda baca di sekitarnya yang jadi yatim),
        # bukan huruf Latin yang menempel.
        token = match.group(0)
        if token and token not in haystack:
            hasil.append(token)
    return hasil


def strip_foreign_tokens(text: str, tokens: Iterable[str]) -> str:
    """Buang potongan asing tertentu dari teks, lalu rapikan sisa tanda baca/spasi.

    Dipakai HANYA setelah model diminta memperbaiki dan masih menyisakan aksara asing:
    lebih baik jawaban bersih tanpa satu kata selipan daripada jawaban yang menampilkan
    aksara yang tidak bisa dibaca pemakainya.
    """
    cleaned = text
    for token in sorted(set(tokens), key=len, reverse=True):
        if not token:
            continue
        cleaned = cleaned.replace(token, " ")
    # Tanda baca yang jadi yatim karena aksara di sekitarnya hilang ("final ，memuat").
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:!?])\s*([,.;:!?])+", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\[\s*\]", "", cleaned)
    cleaned = re.sub(r"^[ \t]*[-*|]\s*$", "", cleaned, flags=re.MULTILINE)
    return clean_text(cleaned)
