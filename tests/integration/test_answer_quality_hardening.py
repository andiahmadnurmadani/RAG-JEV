"""Kualitas jawaban: teks rusak dideteksi/diperbaiki, dan reranker benar-benar menyaring.

Dua keluhan nyata yang dikunci di sini:

1. **Teks jawaban rusak** — model gratis menghasilkan kata terpotong/tercampur
   ("pemb.cgiian", "hanyaLEMpar", "praktikumaccording"). Detektornya harus menangkap kata
   seperti itu TANPA menandai teks normal (istilah teknis, nama kolom, kata umum) — positif
   palsu akan memicu perbaikan yang tidak perlu dan bisa mengubah jawaban yang sudah benar.
2. **Reranker** — dengan ``reranker_provider=none`` urutan hasil tidak pernah diperbaiki
   (NoopReranker hanya mengembalikan skor menurun). ``lexical`` harus benar-benar menaikkan
   kandidat relevan, dan skornya harus dinormalkan sehingga threshold tidak memotong kandidat
   terbaik (kalau tidak, layanan menjawab "tidak ditemukan" padahal datanya ada).
"""

from __future__ import annotations

import re

import pytest

from app.rag.generator import Generator
from app.rag.reranker import LexicalReranker, NoopReranker, build_reranker

KONTEKS = (
    "MODUL 5 UPDATE DAN DELETE PADA SQLite. Tujuan praktikum: memahami operasi UPDATE dan "
    "DELETE. Kolom employee_id dan tanggal_mulai wajib diisi. Jatah cuti tahunan 12 hari "
    "kerja per tahun. Pembagian materi dilakukan per pertemuan."
)

# (kalimat, apakah harus terdeteksi rusak)
KASUS_TEKS = [
    ("Materi pemb.cgiian dilakukan per pertemuan.", True),
    ("Materi hanyaLEMpar ke mahasiswa.", True),
    ("Praktikumaccording dengan modul SQLite.", True),
    ("Pembagian materi dilakukan per pertemuan sesuai jadwal.", False),
    ("Tujuan praktikum adalah memahami operasi UPDATE dan DELETE pada SQLite.", False),
    ("Kolom employee_id wajib diisi di sistem informasi.", False),
    ("Jatah cuti tahunan adalah 12 hari kerja per tahun.", False),
    ("Mahasiswa memahami instruksi dan mengerjakan latihan dengan teliti.", False),
    ("The update and delete operations are demonstrated in class.", False),
    ("Gunakan knowledgeBase dan frontend untuk membangun antarmuka.", False),
    # Kata menempel pada angka tahun - kasus produksi nyata ("Laporan Tahunan2025").
    ("**Laporan Tahunan2025 — PT Bank Pembangunan Daerah**", True),
    ("Data tahun2026 dan laporan2025 tersedia.", True),
    # Angka tahun dengan spasi wajar, dan kode internal yang memang mengandung tahun.
    ("Laporan Tahunan 2025 sudah diterbitkan.", False),
    ("Dokumen PSL2025 adalah kode internal yang sah.", False),
]


@pytest.fixture()
def generator(settings):
    return Generator(settings)


@pytest.mark.parametrize("teks,harus_rusak", KASUS_TEKS)
def test_corruption_detector_catches_broken_words_without_false_positives(generator, teks, harus_rusak):
    detected = generator._corrupted_words(teks, KONTEKS)
    assert bool(detected) is harus_rusak, f"{teks!r} -> terdeteksi {sorted(detected)}"


def test_the_answer_is_repaired_when_corruption_is_detected(settings):
    """Jawaban rusak harus diganti versi bersih, dan fakta/sitasi tidak boleh hilang."""
    from app.rag.context import BuiltContext

    class RepairingClient:
        model = "uji-perbaikan"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, **kwargs):
            from app.rag.generator import LLMUsage

            self.calls += 1
            if self.calls == 1:
                return (
                    "Pembagian materi dilakukan per pertemuan. Materi pemb.cgiian ke mahasiswa [1]. "
                    "Jatah cuti tahunan 12 hari kerja [2].",
                    LLMUsage(input_tokens=10, output_tokens=20, latency_ms=5.0, model=self.model,
                             finish_reason="stop"),
                )
            return (
                "Pembagian materi dilakukan per pertemuan. Materi dibagikan ke mahasiswa [1]. "
                "Jatah cuti tahunan 12 hari kerja [2].",
                LLMUsage(input_tokens=10, output_tokens=20, latency_ms=5.0, model=self.model,
                         finish_reason="stop"),
            )

        def health(self) -> str:
            return "ok"

    client = RepairingClient()
    gen = Generator(settings, client=client)
    context = BuiltContext(text="konteks uji", used=1)
    answer = gen.answer(query="bagaimana pembagian materi?", context=context)

    assert client.calls == 2, "perbaikan tidak dipanggil"
    assert "cgiian" not in answer.answer, "kata rusak masih ada di jawaban akhir"
    assert "[1]" in answer.answer and "[2]" in answer.answer, "sitasi hilang setelah perbaikan"


def test_the_repair_is_rejected_when_it_loses_facts(settings):
    """Perbaikan yang memangkas jawaban atau membuang sitasi harus DITOLAK, bukan dipakai."""
    from app.rag.context import BuiltContext

    class TruncatingRepairClient:
        model = "uji-perbaikan-buruk"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, **kwargs):
            from app.rag.generator import LLMUsage

            self.calls += 1
            usage = LLMUsage(input_tokens=10, output_tokens=20, latency_ms=5.0, model=self.model,
                             finish_reason="stop")
            if self.calls == 1:
                return "Materi pemb.cgiian ke mahasiswa [1] dengan penjelasan panjang lebar.", usage
            return "Singkat [1].", usage  # jauh lebih pendek -> harus ditolak

        def health(self) -> str:
            return "ok"

    gen = Generator(settings, client=TruncatingRepairClient())
    answer = gen.answer(query="bagaimana?", context=BuiltContext(text="konteks", used=1))
    # Jawaban asli dipertahankan; perbaikan buruk tidak dipakai.
    assert "penjelasan panjang lebar" in answer.answer


def test_lexical_reranker_promotes_the_relevant_candidate():
    query = "berapa jatah cuti tahunan dan syarat pengajuannya"
    candidates = [
        "Laporan keuangan triwulan memuat neraca dan laba rugi perusahaan.",
        "Prosedur pengadaan barang dan jasa diatur dalam kebijakan tersendiri.",
        "Jatah cuti tahunan adalah 12 hari kerja. Pengajuan cuti tahunan paling lambat 3 hari sebelum mulai.",
        "Daftar hadir pegawai dan rekap absensi bulanan.",
    ]
    # NoopReranker mempertahankan urutan masuk: kandidat relevan (indeks 2) tidak naik.
    noop_scores = NoopReranker().score(query, candidates)
    assert noop_scores[0] > noop_scores[2], "NoopReranker seharusnya mempertahankan urutan"

    scores = LexicalReranker().score(query, candidates)
    assert scores[2] == max(scores), "kandidat relevan tidak dinaikkan oleh reranker leksikal"
    # Skor ABSOLUT (bukan dinormalkan ke 1.0): kandidat relevan lolos gerbang relevansi bawaan,
    # kandidat yang tidak memuat satu pun kata kueri bernilai 0.
    assert max(scores) >= 0.25, "kandidat terbaik harus lolos gerbang relevansi bawaan"
    assert scores[0] == 0.0 and scores[3] == 0.0


def test_neural_provider_falls_back_to_lexical_when_the_library_is_missing(settings, monkeypatch):
    """Provider neural tanpa pustakanya tidak boleh berakhir jadi 'none' diam-diam."""
    import app.rag.reranker as module

    monkeypatch.setattr(module, "_neural_available", lambda provider: False)
    settings.reranker_provider = "sentence_transformers"
    settings.reranker_fallback_to_lexical = True
    assert build_reranker(settings).name == "lexical"


def test_the_llm_receives_sampling_parameters(settings):
    """top_p/penalty harus benar-benar dikirim - sebelumnya tidak pernah diset sama sekali."""
    from app.rag import generator as generator_module

    captured = {}

    class CaptureClient:
        model = "uji-sampling"

        def chat(self, messages, **kwargs):
            from app.rag.generator import LLMUsage

            return "jawaban [1].", LLMUsage(input_tokens=1, output_tokens=1, model=self.model)

        def health(self) -> str:
            return "ok"

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "uji",
            }

        @staticmethod
        def raise_for_status():
            return None

    import httpx

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json or {})
        return FakeResponse()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(httpx, "post", fake_post)
    # Bawaannya 0 (tidak dikirim); operator yang menyetelnya harus benar-benar terkirim.
    settings.llm_frequency_penalty = 0.2
    try:
        client = generator_module.LLMClient(settings)
        client.chat([{"role": "user", "content": "halo"}])
    finally:
        monkeypatch.undo()

    assert captured.get("top_p") == settings.llm_top_p
    assert captured.get("frequency_penalty") == settings.llm_frequency_penalty
    assert "max_tokens" in captured and "temperature" in captured


def test_optional_sampling_is_dropped_when_the_gateway_rejects_it(settings, monkeypatch):
    """Gateway yang menolak `top_p` tidak boleh menggagalkan seluruh jawaban.

    Kasus nyata: gateway yang dipakai membalas HTTP 400 saat menerima `top_p`. Tanpa penanganan
    ini, satu parameter OPSIONAL membuat seluruh pertanyaan gagal (502) - jauh lebih buruk
    daripada kehilangan sedikit pengetatan sampling.
    """
    import httpx

    from app.rag import generator as generator_module

    sent: list[dict] = []

    class Ok:
        status_code = 200

        @staticmethod
        def json():
            return {
                "choices": [{"message": {"content": "jawaban [1]"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "uji",
            }

        @staticmethod
        def raise_for_status():
            return None

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(dict(json or {}))
        if "top_p" in (json or {}):
            request = httpx.Request("POST", url)
            response = httpx.Response(400, request=request, text="bad request")
            raise httpx.HTTPStatusError("Client error '400 Bad Request'", request=request, response=response)
        return Ok()

    monkeypatch.setattr(httpx, "post", fake_post)
    text, _usage = generator_module.LLMClient(settings).chat([{"role": "user", "content": "halo"}])

    assert len(sent) >= 2, "tidak ada percobaan ulang tanpa parameter yang ditolak"
    assert "top_p" not in sent[-1], "top_p masih dikirim setelah ditolak"
    assert text == "jawaban [1]", "jawaban tidak keluar walau permintaan tanpa top_p berhasil"
