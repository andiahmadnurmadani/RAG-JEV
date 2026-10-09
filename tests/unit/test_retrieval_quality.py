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
