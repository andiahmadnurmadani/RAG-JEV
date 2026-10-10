"""Analisis teks untuk pencarian leksikal (BM25 + reranker leksikal).

Dipakai bersama oleh indeks BM25 dan reranker supaya kueri dan dokumen dipecah dengan
aturan yang SAMA - kalau keduanya berbeda, kata yang sama tidak akan pernah cocok.

Tiga hal yang dulu membuat pencarian meleset:

* **Imbuhan bahasa Indonesia.** "mengajukan", "pengajuan", "diajukan" dulu dianggap tiga
  kata berbeda. Sekarang tiap kata juga menghasilkan bentuk dasar (stemming ringan tanpa
  kamus). Bentuk aslinya tetap ikut, jadi kecocokan persis tetap bernilai lebih tinggi.
* **Kata tugas.** "yang", "dan", "apa", "bagaimana" dulu ikut menyumbang skor, sehingga
  potongan apa pun yang kebetulan memuatnya terlihat relevan.
* **Angka dan kode.** "Rp1.500.000" dulu terpecah jadi ``rp1``/``500``/``000``; sekarang
  utuh (``rp1500000``) dan bagian angkanya juga bisa dicari.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

# Kata yang tersusun dari huruf/angka, boleh disambung - . / , (kode, tanggal, nominal).
_COMPOUND_RE = re.compile(r"\w+(?:[-./,]\w+)*", re.UNICODE)
_SPLIT_RE = re.compile(r"[-./,]+")
_THOUSANDS_RE = re.compile(r"(?<=\d)[.,](?=\d{3}(?:\D|$))")
_NOMINAL_RE = re.compile(r"[a-z]{0,3}\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?")

STOPWORDS = frozenset(
    """
    yang dan atau di ke dari pada untuk dengan ini itu tersebut adalah ialah merupakan
    akan sudah telah belum masih sedang dapat bisa boleh harus perlu mau ingin juga saja
    pun lah kah tah nya ada tidak bukan jangan apa apakah siapa mana manakah kapan dimana
    bagaimana mengapa kenapa berapa berapakah kah sebutkan jelaskan tolong mohon coba
    saya aku kami kita anda kamu dia mereka beliau ia
    oleh sebagai secara agar supaya karena sebab jika jikalau kalau bila apabila maka
    namun tetapi tapi serta lalu kemudian setelah sebelum sesudah saat ketika selama
    hingga sampai antara dalam luar atas bawah tentang terhadap bagi tanpa via per
    para sang si se suatu sebuah seorang setiap tiap semua seluruh segala beberapa banyak
    lebih kurang sangat paling amat begitu demikian sini situ sana
    hal cara yaitu yakni bahwa adapun ataupun maupun dll dsb dst
    mengenai terkait berikut seperti misalnya contohnya
    the a an and or of to in on at for with from by as is are was were be been being
    this that these those it its what which who whom whose when where why how
    do does did done can could should would will shall may might must
    i you he she we they me him her us them my your our their
    about into over under than then there here also only just not no yes
    please tell show give list explain
    """.split()
) | frozenset(
    # Kata pembingkai pertanyaan: hampir tidak pernah muncul di dokumen, jadi kalau dihitung ia
    # dianggap "kata langka yang tidak cocok" dan menurunkan skor pertanyaan yang sebenarnya bisa
    # dijawab ("Jelaskan pengertian css" ditolak padahal dokumen CSS ada).
    """
    kaitan keterkaitan hubungan hubungannya perbedaan beda bedanya perbandingan persamaan
    pengertian definisi maksud dimaksud arti artinya makna penjelasan uraian uraikan
    contoh contohnya misal misalnya gambaran seputar dijelaskan disebut gimana gmn bgmn
    knp yg dgn utk dr tdk sih dong deh ya
    relation relationship relations difference differences between meaning definition
    define describe example examples mean means
    """.split()
)

_VOWELS = set("aeiou")
MIN_STEM = 3


def _strip_suffixes(word: str) -> List[str]:
    """Kembalikan bentuk-bentuk setelah partikel/posesif/akhiran dilepas (urut)."""
    forms: List[str] = []
    current = word
    for suffix in ("lah", "kah", "tah", "pun"):
        if current.endswith(suffix) and len(current) - len(suffix) >= 4:
            current = current[: -len(suffix)]
            forms.append(current)
            break
    for suffix in ("nya", "ku", "mu"):
        if current.endswith(suffix) and len(current) - len(suffix) >= 4:
            current = current[: -len(suffix)]
            forms.append(current)
            break
    # "-kan" dan "-an" sama-sama dicoba bila katanya berakhir "kan": "dikembangkan" -> kembang
    # (-kan), tetapi "kebijakan" -> bijak (-an). Tanpa kamus tidak bisa dipastikan mana yang
    # benar, jadi keduanya disimpan.
    for suffix in ("kan", "an", "i"):
        if not current.endswith(suffix) or len(current) - len(suffix) < 4:
            continue
        base = current[: -len(suffix)]
        # Akhiran -i hanya dilepas bila sebelumnya konsonan: "dekati" -> "dekat", tetapi
        # "pegawai"/"sesuai" adalah kata dasar.
        if suffix == "i" and base[-1] in _VOWELS:
            continue
        forms.append(base)
        if suffix != "kan":
            break
    return forms


def _strip_prefix(word: str) -> List[str]:
    """Bentuk dasar yang mungkin setelah satu awalan dilepas (bisa lebih dari satu)."""
    out: List[str] = []

    def add(value: str) -> None:
        if len(value) >= MIN_STEM and value not in out:
            out.append(value)

    for plain in ("memper", "diper", "ber", "ter", "per", "di", "ke", "se"):
        if word.startswith(plain) and len(word) - len(plain) >= MIN_STEM:
            rest = word[len(plain):]
            add(rest)
            if plain in ("ber", "ter") and rest[0] in _VOWELS:
                # "berangkat" -> angkat, "beragam" -> ragam: dua-duanya disimpan.
                add("r" + rest)
            return out

    for prefix in ("me", "pe"):
        if not word.startswith(prefix) or len(word) < len(prefix) + MIN_STEM:
            continue
        rest = word[len(prefix):]
        if rest.startswith("ny") and len(rest) > 2 and rest[2] in _VOWELS:
            add("s" + rest[2:])                                  # menyapu -> sapu
        elif rest.startswith("ng") and len(rest) > 2:
            tail = rest[2:]
            if tail[0] in _VOWELS:
                add("k" + tail)                                  # mengirim -> kirim
                add(tail)                                        # mengajar -> ajar
            else:
                add(tail)                                        # menghitung -> hitung
        elif rest.startswith("m") and len(rest) > 1:
            tail = rest[1:]
            if tail[0] in _VOWELS:
                add("p" + tail)                                  # memakai -> pakai
                add("m" + tail)                                  # memasak -> masak
            else:
                add(tail)                                        # membaca -> baca
        elif rest.startswith("n") and len(rest) > 1:
            tail = rest[1:]
            if tail[0] in _VOWELS:
                add("t" + tail)                                  # menulis -> tulis
                add("n" + tail)                                  # menanti -> nanti
            else:
                add(tail)                                        # mencari -> cari
        elif rest[0] in "lrwy":
            add(rest)                                            # melihat -> lihat
        return out
    return out


def stem_variants(token: str) -> List[str]:
    """Bentuk dasar tambahan untuk satu token (tanpa token aslinya)."""
    if len(token) < 5 or not token.isalpha():
        return []
    variants: List[str] = []
    bases = [token] + _strip_suffixes(token)
    for base in bases:
        if base != token and base not in variants and len(base) >= MIN_STEM:
            variants.append(base)
        for stripped in _strip_prefix(base):
            if stripped not in variants and stripped != token:
                variants.append(stripped)
            # Awalan bertumpuk: "diperbaiki" -> "perbaiki" -> "baik". Hanya per-/ber-/pe-/ter-
            # yang boleh jadi awalan kedua; "kembang" tidak boleh jadi "mbang".
            if not stripped.startswith(("per", "ber", "pe", "ter")):
                continue
            for deeper in _strip_prefix(stripped):
                if deeper not in variants and deeper != token:
                    variants.append(deeper)
    return [item for item in variants if item not in STOPWORDS]


def surface_tokens(text: str) -> List[str]:
    """Token permukaan (huruf kecil), dengan kode/nominal dijaga utuh + bagian-bagiannya."""
    out: List[str] = []
    for match in _COMPOUND_RE.finditer((text or "").lower()):
        token = match.group(0).strip("-./,_")
        if not token:
            continue
        parts = [part for part in _SPLIT_RE.split(token) if part]
        if len(parts) <= 1:
            out.append(token)
            continue
        forms: List[str] = []
        if _NOMINAL_RE.fullmatch(token):
            # "rp1.500.000" -> "rp1500000" + "1500000": nominal dicari utuh, bukan per 3 digit.
            joined = _THOUSANDS_RE.sub("", token)
            forms.append(joined)
            digits = re.sub(r"\D", "", joined)
            if digits != joined:
                forms.append(digits)
        else:
            forms.append(token)
            # "sop-12/2026" -> "sop-12", "2026": kode di kiri/kanan garis miring tetap utuh.
            forms.extend(segment for segment in token.split("/") if segment and segment != token)
        forms.extend(part for part in parts if len(part) >= 2 or part.isdigit())
        out.extend(unique(forms))
    return out


def keywords(text: str) -> List[str]:
    """Token bermakna (bukan kata tugas), urut kemunculan."""
    return [
        token
        for token in surface_tokens(text)
        if token not in STOPWORDS and (len(token) >= 2 or token.isdigit())
    ]


def term_forms(token: str) -> List[str]:
    """Token + bentuk dasarnya - satu "istilah" yang bisa cocok lewat bentuk mana pun."""
    return [token] + stem_variants(token)


def index_terms(text: str) -> List[str]:
    """Daftar istilah untuk BM25: kata bermakna + bentuk dasarnya."""
    out: List[str] = []
    for token in keywords(text):
        out.extend(term_forms(token))
    return out


def positions(text: str) -> Dict[str, List[int]]:
    """Peta bentuk-istilah -> posisi kata bermakna di teks (untuk kedekatan & frasa)."""
    table: Dict[str, List[int]] = {}
    for position, token in enumerate(keywords(text)):
        for form in term_forms(token):
            table.setdefault(form, []).append(position)
    return table


def unique(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(items))


def sparse_text(document_name: str, section: str, content: str) -> str:
    """Teks yang diindeks BM25: nama dokumen + bagian ikut, supaya pertanyaan yang menyebut
    nama dokumen ("SOP cuti") cocok walau potongannya sendiri tidak mengulang nama itu."""
    header = " ".join(part for part in (document_name or "", section or "") if part)
    return f"{header}\n{content}" if header else (content or "")


def legacy_tokenize(text: str) -> List[str]:
    """Pemecah token versi lama (sebelum stemming) - hanya untuk indeks lama yang belum
    dibangun ulang, supaya kueri tetap cocok dengan token yang tersimpan."""
    return [token.lower() for token in re.findall(r"[\w\-]+", text or "", re.UNICODE)]

