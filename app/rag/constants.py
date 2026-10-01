"""Konstanta yang dipakai lintas modul RAG tanpa menimbulkan impor melingkar.

``SUMMARY_CHUNK_ID`` perlu dikenal oleh chunker, indeks sparse, dan repositori. Menaruhnya di
``app/rag/summary.py`` membuat impor melingkar (summary -> context -> retriever -> sparse ->
summary), jadi nilainya tinggal di sini: satu modul tanpa dependensi apa pun.
"""

from __future__ import annotations

# chunk_id potongan ringkasan dokumen (knowledge turunan). Sengaja bukan pola "chunk_NNNN"
# supaya tidak pernah bentrok dengan potongan isi.
SUMMARY_CHUNK_ID = "chunk_summary"

__all__ = ["SUMMARY_CHUNK_ID"]
