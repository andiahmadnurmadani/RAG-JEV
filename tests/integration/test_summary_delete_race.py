"""Dokumen yang dihapus tidak boleh "hidup lagi" karena ringkasannya selesai belakangan.

Ringkasan dikerjakan di worker terpisah, jadi ia bisa selesai SETELAH dokumen dihapus. Bila
penutup ringkasan memanggil ``update(status=completed)`` tanpa memeriksa, catatan yang sudah
ditandai terhapus akan hidup kembali dan muncul di daftar dokumen - padahal isinya sudah tidak
ada. Terlihat nyata di produksi: dokumen uji yang sudah dihapus muncul kembali sebagai
``completed``.
"""

from __future__ import annotations

import base64
import threading
import time

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_hapus"


class _HeldSummaryClient:
    """Klien LLM yang menahan panggilan sampai dilepas, supaya kita bisa menghapus di tengah."""

    def __init__(self) -> None:
        self.model = "uji-tahan"
        self.release = threading.Event()
        self.started = threading.Event()

    def chat(self, messages, **kwargs):
        from app.rag.generator import LLMUsage

        self.started.set()
        # Tunggu sampai uji selesai menghapus dokumen (atau batas 20 detik).
        self.release.wait(timeout=20)
        return "Ringkasan yang datang terlambat.", LLMUsage(
            input_tokens=50, output_tokens=10, latency_ms=10.0, model=self.model, finish_reason="stop"
        )

    def health(self) -> str:
        return "ok"


def _index(client, document_id: str, text: str):
    return client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": document_id,
            "document_name": f"{document_id}.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(text.encode()).decode(),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )


def test_a_summary_finishing_after_delete_does_not_resurrect_the_document(client, settings):
    from app.api.deps import get_services

    services = get_services()
    held = _HeldSummaryClient()
    services.generator.rebind_llm_client(held)
    services.indexing._generator = services.generator

    text = "# Dokumen\n\nIsi yang akan diringkas belakangan. " + ("Kalimat isi. " * 40)
    response = _index(client, "doc_hapus", text)
    assert response.status_code == 202, response.text

    # Tunggu ringkasan benar-benar mulai (dokumen masih ada), lalu HAPUS di tengah jalan.
    assert held.started.wait(timeout=30), "ringkasan tidak pernah mulai"
    deleted = client.delete("/api/v1/knowledge/doc_hapus", headers=auth(TENANT_A_KEY))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deleted"

    # Lepaskan ringkasan: ia akan selesai setelah dokumen dihapus.
    held.release.set()
    time.sleep(1.5)

    # Dokumen TIDAK boleh muncul lagi di daftar.
    listed = client.get(f"/api/v1/knowledge?knowledge_base_id={KB}", headers=auth(TENANT_A_KEY))
    ids = [item["document_id"] for item in listed.json()["data"]["documents"]]
    assert "doc_hapus" not in ids, f"dokumen terhapus hidup kembali: {ids}"

    # Dan statusnya tetap terhapus.
    status = client.get("/api/v1/knowledge/doc_hapus", headers=auth(TENANT_A_KEY))
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "deleted", status.json()["data"]


def test_the_delete_guard_ignores_a_stale_summary_write():
    """Penjaga di tingkat JobStore: penulisan ke pekerjaan terhapus tidak berpengaruh."""
    from app.workers.indexing import JobStore

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        store = JobStore(str(Path(tmp) / "jobs.json"))
        record = store.create(
            organization_id="org", document_id="doc", knowledge_base_id=KB,
            document_name="doc.md", status="processing", stage="summarizing",
        )
        store.mark_deleted("org", "doc")
        assert store.is_deleted(record.job_id)

        # Ringkasan yang telat: harus diabaikan, bukan menghidupkan kembali.
        assert store.update_unless_deleted(record.job_id, status="completed", stage="completed") is None
        assert store.get(record.job_id).status == "deleted"
        assert store.is_deleted(record.job_id)
