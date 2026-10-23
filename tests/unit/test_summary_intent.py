"""Mengenali permintaan ringkasan umum vs ringkasan satu topik."""

import pytest

from app.rag.pipeline import is_generic_summary, is_summary_request


@pytest.mark.parametrize(
    "query",
    [
        "buat ringkasan 5 poin utama",
        "rangkum isi dokumen ini",
        "tolong ringkas ketiga dokumen tersebut secara singkat",
        "buatkan intisari dari semua file yang saya upload",
        "Summarize the key points of all documents",
        "berikan 3 poin penting dari dokumen",
    ],
)
def test_generic_summary_requests(query):
    assert is_summary_request(query)
    assert is_generic_summary(query)


@pytest.mark.parametrize(
    "query",
    ["ringkas kebijakan cuti", "rangkum aturan lembur pegawai kontrak", "summarize the leave policy"],
)
def test_summaries_about_a_topic_are_not_generic(query):
    assert is_summary_request(query)
    assert not is_generic_summary(query)


@pytest.mark.parametrize("query", ["berapa hari cuti tahunan?", "jam masuk kantor pukul berapa"])
def test_ordinary_questions_are_not_summary_requests(query):
    assert not is_summary_request(query)
