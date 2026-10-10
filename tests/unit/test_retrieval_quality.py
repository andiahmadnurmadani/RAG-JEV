"""Akurasi pencarian leksikal: analisis teks Indonesia, BM25, reranker absolut, gerbang jawaban."""

from __future__ import annotations

import json

from app.rag.generator import Generator, clean_citations
from app.rag.reranker import LexicalReranker
from app.rag.sparse import INDEX_VERSION, SparseIndex, migrate_legacy_scopes
from app.rag.textnorm import index_terms, keywords, term_forms
from app.workers.indexing import JobStore


# --------------------------------------------------------------------------- #
# Analisis teks
# --------------------------------------------------------------------------- #
def test_imbuhan_berbeda_bertemu_di_bentuk_dasar():
    assert set(term_forms("pengajuan")) & set(term_forms("diajukan")) & set(term_forms("mengajukan"))
    assert "bijak" in term_forms("kebijakan")
    assert "lambat" in term_forms("keterlambatan")
    assert "turun" in term_forms("penurunan")


def test_kata_dasar_tidak_dirusak():
    assert term_forms("pegawai") == ["pegawai"]
    assert "mbang" not in term_forms("dikembangkan")


def test_kata_tugas_tidak_ikut_dicari():
    assert keywords("Bagaimana cara mengajukan cuti yang benar?") == ["mengajukan", "cuti", "benar"]


def test_nominal_dan_kode_tetap_utuh():
    terms = index_terms("Biaya Rp1.500.000 sesuai SOP-12/2026 dan ISO-27001")
    assert "rp1500000" in terms and "1500000" in terms
    assert "sop-12" in terms and "iso-27001" in terms


# --------------------------------------------------------------------------- #
# BM25
# --------------------------------------------------------------------------- #
def _index(settings) -> SparseIndex:
    index = SparseIndex(settings)
    index.upsert(
        "org", "kb",
        [
            ("chunk_0001", "doc_cuti", "Pengajuan cuti tahunan melalui sistem HR minimal tujuh hari."),
            ("chunk_0001", "doc_lain", "Laporan keuangan triwulan memuat neraca."),
            ("chunk_0002", "doc_lain", "Daftar hadir pegawai bulanan."),
        ],
    )
    return index


def test_bm25_hanya_mengembalikan_potongan_yang_cocok(settings):
    hits = _index(settings).search("bagaimana mengajukan cuti", "org", "kb")
    assert [key for key, _ in hits] == ["doc_cuti::chunk_0001"], "potongan tanpa kata kueri tidak boleh ikut"


def test_penanda_ringkasan_bertahan_setelah_dimuat_ulang(settings):
    index = SparseIndex(settings)
    index.upsert("org", "kb", [("summary", "doc_a", "ringkasan kebijakan cuti", True)])
    reloaded = SparseIndex(settings)
    assert reloaded.search("cuti", "org", "kb") == [], "ringkasan tidak boleh ikut pencarian biasa setelah restart"
    assert reloaded.search("cuti", "org", "kb", include_summary=True)


def test_indeks_versi_lama_terbaca_lalu_dibangun_ulang(settings):
    index = SparseIndex(settings)
    path = index._path(index.scope_key("org", "kb"))
    path.write_text(json.dumps({
        "organization_id": "org", "knowledge_base_id": "kb",
        "entries": [{"chunk_id": "chunk_0001", "document_id": "doc_a", "tokens": ["yang", "pengajuan", "cuti"]}],
    }), encoding="utf-8")

    legacy = SparseIndex(settings)
    assert legacy.search("diajukan", "org", "kb"), "indeks lama tetap bisa dicari (dengan bentuk dasar)"
    assert legacy.legacy_scopes() == [("org", "kb")]

    def loader(org, kb, document_ids):
        assert document_ids == ["doc_a"]
        return [("chunk_0001", "doc_a", "SOP Cuti\nPengajuan cuti tahunan", False)]

    assert migrate_legacy_scopes(legacy, loader) == {"org__kb": 1}
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == INDEX_VERSION
    assert legacy.legacy_scopes() == []
    assert legacy.search("sop", "org", "kb"), "nama dokumen ikut terindeks setelah dibangun ulang"


def test_pembangunan_ulang_tidak_menimpa_perubahan_yang_lebih_baru(settings):
    index = _index(settings)
    generation = index.generation("org", "kb")
    index.upsert("org", "kb", [("chunk_0003", "doc_baru", "dokumen baru")])
    assert index.rebuild_scope("org", "kb", [], expected_generation=generation) is False
    assert index.search("dokumen baru", "org", "kb")


# --------------------------------------------------------------------------- #
# Reranker & gerbang relevansi
# --------------------------------------------------------------------------- #
def test_skor_reranker_absolut_bukan_relatif():
    reranker = LexicalReranker()
    relevant = reranker.score("jatah cuti tahunan", ["Jatah cuti tahunan adalah 12 hari kerja."])[0]
    weak = reranker.score("jatah cuti tahunan", ["Kantor tutup pada hari libur tahunan."])[0]
    assert relevant >= 0.8
    assert weak < relevant
    assert reranker.score("jatah cuti", ["Laporan keuangan triwulan."]) == [0.0]


def test_idf_korpus_membuat_kata_umum_tidak_menentukan():
    reranker = LexicalReranker()
    docs = ["Prosedur perusahaan tentang absensi."]
    common = {"perusahaan": 0.05, "saham": 3.0}
    score = reranker.score("harga saham perusahaan", docs, idf=lambda term: common.get(term, 3.0))[0]
    assert score < 0.3, "kecocokan hanya pada kata umum tidak boleh dianggap relevan"


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #
def test_jawaban_bersitasi_bukan_penolakan():
    assert Generator._looks_like_refusal("Tidak terdapat biaya pendaftaran untuk program magang [1].") is False
    assert Generator._looks_like_refusal("Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia.")
    assert Generator._looks_like_refusal("")


def test_sitasi_ke_blok_yang_tidak_ada_dibuang():
    assert clean_citations("Cuti 12 hari [1][7]. Lembur [2].", 2) == "Cuti 12 hari [1]. Lembur [2]."


# --------------------------------------------------------------------------- #
# Job terputus restart
# --------------------------------------------------------------------------- #
def test_job_terputus_restart_tidak_macet(tmp_path):
    store = JobStore(str(tmp_path / "jobs.json"))
    indexing = store.create(document_id="a", organization_id="o", knowledge_base_id="kb")
    store.update(indexing.job_id, status="processing", stage="embedding")
    summarizing = store.create(document_id="b", organization_id="o", knowledge_base_id="kb")
    store.update(summarizing.job_id, status="processing", stage="summarizing")

    reloaded = JobStore(str(tmp_path / "jobs.json"))
    assert reloaded.recover_interrupted() == 2
    assert reloaded.get(indexing.job_id).status == "failed"
    assert reloaded.get(summarizing.job_id).status == "completed", "isi sudah tersimpan; hanya ringkasan tertunda"


def test_penolakan_yang_ikut_mengutip_tetap_penolakan():
    for text in (
        "Informasi tersebut tidak ditemukan dalam knowledge base yang tersedia. [1]",
        "Dokumen [1] tidak memuat informasi tentang gaji manajer.",
        "Tidak ada informasi mengenai hal itu dalam konteks [2].",
    ):
        assert Generator._looks_like_refusal(text), text
    assert Generator._looks_like_refusal("Jatah cuti 12 hari [1]; cuti besar tidak disebutkan.") is False


def test_pertanyaan_baru_yang_pendek_tidak_dicampur_riwayat():
    from app.rag.pipeline import is_follow_up

    assert is_follow_up("yang kedua?") and is_follow_up("kalau lembur?")
    assert not is_follow_up("Berapa upah lembur jam pertama?")
    assert not is_follow_up("Siapa yang menyetujui pengadaan di atas seratus juta?")


def test_hapus_banyak_dokumen_sekali_tulis(tmp_path):
    import time

    store = JobStore(str(tmp_path / "jobs.json"))
    for number in range(1000):
        store.create(document_id=f"doc_{number}", organization_id="o", knowledge_base_id="kb")
    started = time.perf_counter()
    assert store.mark_deleted_many("o", [f"doc_{number}" for number in range(1000)]) == 1000
    assert time.perf_counter() - started < 2.0
    assert store.list_documents("o", knowledge_base_id="kb") == []


def test_kata_pembingkai_pertanyaan_tidak_menjatuhkan_skor():
    """Kasus nyata production: "Apa kaitan css dengan html" ditolak (skor 0,25) karena "kaitan"
    tidak pernah muncul di dokumen dan dihitung sebagai kata kunci langka yang tidak cocok."""
    docs = ["HTML menyusun struktur halaman web; CSS mengatur tampilan elemen HTML.", "Basis data menyimpan tabel."]
    reranker = LexicalReranker()
    for query in ("Apa kaitan css dengan html", "Jelaskan pengertian css", "Apa perbedaan html dan css?"):
        assert max(reranker.score(query, docs)) >= 0.6, query
    assert "kaitan" not in keywords("Apa kaitan css dengan html")


def test_fokus_dokumen_mencegah_konteks_bercampur():
    from app.rag.retriever import Candidate, _focus_documents

    def cand(doc, score, n=0):
        return Candidate(chunk_id=f"{doc}::chunk_{n}", document_id=doc, content=f"{doc} {n}", score=score)

    # dokumen teratas dan satu dokumen yang hampir sama kuat dipertahankan; ekor pengalih dibuang
    candidates = [cand("sop_cuti", 0.74), cand("sop_cuti", 0.70, 1), cand("cuti_besar", 0.62),
                  cand("cuti_melahirkan", 0.51), cand("tamu", 0.40)]
    kept = _focus_documents(candidates, ratio=0.8, limit=3)
    assert [c.document_id for c in kept] == ["sop_cuti", "sop_cuti", "cuti_besar"]
    # batas jumlah dokumen berlaku walau skornya rapat
    close = [cand(f"d{i}", 0.70 - i * 0.001) for i in range(10)]
    assert len({c.document_id for c in _focus_documents(close, ratio=0.8, limit=3)}) == 3
    # rasio 0 + batas besar = mati (perilaku lama)
    assert len(_focus_documents(close, ratio=0.0, limit=99)) == 10


def test_pertanyaan_perbandingan_melonggarkan_fokus():
    from app.rag.retriever import wants_multiple_documents

    assert wants_multiple_documents("Apa perbedaan cuti besar dan cuti melahirkan?")
    assert wants_multiple_documents("Bandingkan tunjangan shift di setiap cabang")
    assert not wants_multiple_documents("Siapa yang menyetujui pengajuan cuti?")
