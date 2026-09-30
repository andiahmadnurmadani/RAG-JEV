"""Indexing source loading: file:// paths, inline text, base64, size limits (PRD 34)."""

from __future__ import annotations

import base64

import pytest

from app.core.errors import AppError
from app.workers.indexing import IndexingPipeline, JobStore


@pytest.fixture()
def pipeline(settings):
    store = JobStore(settings.job_store_path)
    return IndexingPipeline(settings, embedder=None, sparse=None, jobs=store)  # only _load_source is exercised


def test_file_url_with_a_windows_drive_is_resolved(pipeline, tmp_path):
    target = tmp_path / "sop.txt"
    target.write_text("kebijakan cuti tahunan", encoding="utf-8")
    url = "file:///" + str(target).replace("\\", "/")
    raw, parse_name, display_name = pipeline._load_source(
        file_url=url, content_base64="", text="", document_name=""
    )
    assert raw == b"kebijakan cuti tahunan"
    assert parse_name.endswith("sop.txt")
    assert display_name.endswith("sop.txt")


def test_plain_windows_path_is_accepted(pipeline, tmp_path):
    target = tmp_path / "dokumen.md"
    target.write_text("# Judul", encoding="utf-8")
    raw, parse_name, _ = pipeline._load_source(file_url=str(target), content_base64="", text="", document_name="")
    assert raw == b"# Judul"
    assert parse_name == "dokumen.md"


def test_missing_local_file_reports_document_not_found(pipeline, tmp_path):
    with pytest.raises(AppError) as excinfo:
        pipeline._load_source(
            file_url=str(tmp_path / "hilang.pdf"), content_base64="", text="", document_name=""
        )
    assert excinfo.value.code == "DOCUMENT_NOT_FOUND"


def test_inline_text_is_parsed_as_text_even_when_named_pdf(pipeline):
    """KMS may name the source artifact .pdf while we hold extracted text."""
    raw, parse_name, display_name = pipeline._load_source(
        file_url="", content_base64="", text="isi dokumen", document_name="SOP Cuti.pdf"
    )
    assert parse_name == "SOP Cuti.txt"
    assert display_name == "SOP Cuti.pdf"
    assert raw == b"isi dokumen"


def test_base64_payload_keeps_its_real_name(pipeline):
    encoded = base64.b64encode(b"konten").decode()
    raw, parse_name, display_name = pipeline._load_source(
        file_url="", content_base64=encoded, text="", document_name="lampiran.json"
    )
    assert raw == b"konten"
    assert parse_name == display_name == "lampiran.json"


def test_invalid_base64_is_a_validation_error(pipeline):
    with pytest.raises(AppError) as excinfo:
        pipeline._load_source(file_url="", content_base64="not-base64!!", text="", document_name="x.txt")
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_local_file_over_the_limit_is_rejected_before_reading(pipeline, tmp_path, monkeypatch):
    settings = pipeline._settings
    monkeypatch.setattr(settings, "max_upload_mb", 0, raising=False)
    target = tmp_path / "besar.txt"
    target.write_text("x" * 32, encoding="utf-8")
    with pytest.raises(AppError) as excinfo:
        pipeline._load_source(file_url=str(target), content_base64="", text="", document_name="")
    assert excinfo.value.code == "PAYLOAD_TOO_LARGE"


def test_no_source_at_all_is_rejected(pipeline):
    with pytest.raises(AppError) as excinfo:
        pipeline._load_source(file_url="", content_base64="", text="", document_name="")
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_inline_text_needs_no_extension_in_its_label(settings):
    """A human label like "SOP Cuti 2026" must not be MIME-validated: the bytes we hold are
    text. Validating the label rejected this with UNSUPPORTED_MEDIA_TYPE (found while using
    the browser console, where the name field is free text)."""
    from app.core.security import validate_display_label, validate_upload

    pipeline = IndexingPipeline(settings, embedder=None, sparse=None, jobs=JobStore(settings.job_store_path))
    raw, parse_name, display_name = pipeline._load_source(
        file_url="", content_base64="", text="isi", document_name="SOP Cuti 2026"
    )
    assert display_name == "SOP Cuti 2026"
    assert parse_name == "SOP Cuti 2026.txt"
    validate_display_label(display_name)  # no extension: fine
    validate_upload(settings, parse_name, raw)  # payload is text: fine


def test_a_label_that_advertises_a_refused_type_is_still_rejected(settings):
    """Free-form labels must not open a hole: a label claiming an unsupported type is refused,
    even though the bytes are text (that is what "payload.exe" means to a reader)."""
    from app.core.security import validate_display_label

    with pytest.raises(AppError) as excinfo:
        validate_display_label("payload.exe")
    assert excinfo.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_an_unsupported_payload_is_still_refused(settings):
    from app.core.security import validate_upload

    with pytest.raises(AppError) as excinfo:
        validate_upload(settings, "payload.exe", b"MZ\x90\x00binary")
    assert excinfo.value.code == "UNSUPPORTED_MEDIA_TYPE"
