"""Ringkasan tidak boleh menahan antrian pengindeksan.

Keluhan nyatanya: mengunggah berkas kecil terasa lama dan berstatus "queued" padahal ukurannya
kecil. Sebabnya bukan ukuran berkas itu, melainkan dokumen BESAR yang sedang diringkas: ringkasan
memanggil LLM puluhan kali (map-reduce), dan karena semuanya dikerjakan di worker yang sama,
unggahan berikutnya menunggu. Di produksi satu dokumen 1.828 potongan menahan antrian ~15 menit.

Uji ini mengunci pemisahan jalur itu:

* ringkasan dikerjakan di antrian TERPISAH dari pengindeksan;
* `run()` selesai (isi terindeks) tanpa menunggu ringkasan selesai;
* status pekerjaan tetap jujur: `completed` hanya setelah ringkasan selesai (atau setelah
  alasan mengapa ringkasan tidak dibuat dicatat).
"""

from __future__ import annotations

import base64
import threading
import time

from tests.conftest import TENANT_A_KEY, auth, wait_for_job

KB = "kb_queue"


class _SlowSummaryClient:
    """Klien LLM yang sengaja lambat, untuk meniru ringkasan dokumen besar."""

    def __init__(self, delay: float = 0.4) -> None:
        self.model = "uji-lambat"
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()

    def chat(self, messages, **kwargs):
        from app.rag.generator import LLMUsage

        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        return "Ringkasan uji (lambat).", LLMUsage(
            input_tokens=100, output_tokens=20, latency_ms=self.delay * 1000,
            model=self.model, finish_reason="stop",
        )

    def health(self) -> str:
        return "ok"


def _document(sections: int = 30, repeat: int = 20) -> str:
    parts = ["# Dokumen Uji Antrian", ""]
    for index in range(1, sections + 1):
        parts.append(f"## Bagian {index}")
        parts.append(f"Bagian {index} memuat ketentuan antrian_{index:02d}. " + ("Isi rinci. " * repeat))
        parts.append("")
    return "\n".join(parts)


def _index(client, text: str, *, document_id: str) -> dict:
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": document_id,
            "document_name": "antrian.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(text.encode()).decode(),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202, response.text
    return wait_for_job(client, document_id, timeout=120.0)


def test_a_slow_summary_does_not_block_the_next_upload(client, settings):
    """Ringkasan lambat berjalan di jalur sendiri: unggahan berikutnya tidak ikut menunggu."""
    from app.api.deps import get_services

    services = get_services()
    slow = _SlowSummaryClient(delay=0.5)
    services.generator.rebind_llm_client(slow)
    services.indexing._generator = services.generator
    # Beberapa tahap supaya ringkasannya benar-benar lama.
    settings.summary_window_tokens = 300
    settings.summary_max_tokens = 256
    settings.summary_workers = 1

    text = _document()
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_antrian_a",
            "document_name": "antrian-a.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(text.encode()).decode(),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202

    # Tunggu sampai ringkasan benar-benar MULAI (jadi antrian ringkasan sedang sibuk)...
    assert slow.started.wait(timeout=30), "ringkasan tidak pernah mulai"

    # ...lalu unggah dokumen kecil. Isinya harus selesai walau ringkasan pertama belum kelar.
    small = "Dokumen kecil berisi kata kecilkhusus."
    started = time.perf_counter()
    response = client.post(
        "/api/v1/knowledge/index",
        json={
            "document_id": "doc_antrian_b",
            "document_name": "kecil.md",
            "knowledge_base_id": KB,
            "content_base64": base64.b64encode(small.encode()).decode(),
            "replace": True,
        },
        headers=auth(TENANT_A_KEY),
    )
    assert response.status_code == 202
    status = wait_for_job(client, "doc_antrian_b", timeout=60.0)
    elapsed = time.perf_counter() - started
    assert status["status"] == "completed", status
    # Isi dokumen kecil tidak boleh menunggu ringkasan dokumen besar selesai.
    assert elapsed < 20, f"unggahan kecil menunggu terlalu lama ({elapsed:.1f}s) - antrian tidak terpisah?"

    # Dokumen kecilnya benar-benar bisa dicari.
    found = client.post(
        "/api/v1/search",
        json={"query": "kecilkhusus", "knowledge_base_id": KB, "top_k": 5},
        headers=auth(TENANT_A_KEY),
    )
    hits = found.json()["data"]["results"]
    assert any(hit["document_id"] == "doc_antrian_b" for hit in hits), hits


def test_the_job_is_not_marked_completed_before_the_summary_finishes(client, settings):
    """`completed` berarti tuntas: jangan dilaporkan selesai selagi ringkasan belum jalan."""
    from app.api.deps import get_services

    services = get_services()
    slow = _SlowSummaryClient(delay=0.3)
    services.generator.rebind_llm_client(slow)
    services.indexing._generator = services.generator
    settings.summary_window_tokens = 300
    settings.summary_max_tokens = 256

    status = _index(client, _document(sections=12, repeat=12), document_id="doc_antrian_status")
    assert status["status"] == "completed", status
    # Setelah selesai, ringkasannya ada - jadi status "completed" itu memang menunggu ringkasan.
    assert status["summary"], status.get("summary_error")


def test_worker_stats_publish_both_queues(client):
    detail = client.get("/api/v1/ready").json()["data"]["detail"]
    worker = detail["worker"]
    assert "queued" in worker
    assert "summary_queued" in worker
    assert "summary_workers" in worker
    assert worker["summary_workers"] >= 1
