"""Jawaban LLM berformat Markdown: dirender di konsol, bukan ditampilkan sebagai teks mentah.

Permintaan: keluaran LLM berupa Markdown (bukan berkas, bukan lampiran). Dua sisi diuji:

* sisi prompt - model memang diminta menjawab dengan Markdown;
* sisi tampilan - konsol merender Markdown itu dan jawabannya tetap aman dari HTML berbahaya.

Renderer-nya JS kecil tanpa dependensi (``app/ui/markdown.js``). Ujinya dijalankan lewat Node
bila tersedia; kalau Node tidak ada, uji JS-nya dilewati dengan alasan yang jelas, sedangkan
pemeriksaan aset dan pemakaian di konsol tetap jalan.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_the_answer_prompt_asks_for_markdown_not_a_file():
    """Model harus tahu bentuk keluaran yang diminta: Markdown, bukan berkas."""
    prompt = (REPO / "app" / "rag" / "generator.py").read_text(encoding="utf-8")
    system_prompt = prompt.split("SYSTEM_PROMPT = ", 1)[1].split("EXTRACT_SYSTEM_PROMPT", 1)[0]
    assert "Markdown" in system_prompt, "system prompt jawaban tidak menyebut Markdown"
    assert "never output a file" in system_prompt, "prompt tidak melarang keluaran berupa berkas"
    # Ringkasan juga (knowledge turunan) supaya konsisten.
    assert "bukan berkas, bukan lampiran" in prompt, "prompt ringkasan tidak menyebut Markdown"


def test_the_console_serves_the_markdown_renderer():
    page = (REPO / "app" / "ui" / "index.html").read_text(encoding="utf-8")
    # Dimuat SEBELUM app.js: app.js memakai window.Markdown saat merender jawaban.
    markdown_tag = re.search(r'<script src="markdown\.js\?v=([^"]+)"></script>', page)
    app_tag = re.search(r'<script src="app\.js\?v=([^"]+)"></script>', page)
    assert markdown_tag, "index.html tidak memuat markdown.js dengan cap versi"
    assert app_tag, "index.html tidak memuat app.js dengan cap versi"
    assert markdown_tag.start() < app_tag.start(), "markdown.js harus dimuat sebelum app.js"
    assert markdown_tag.group(1) == app_tag.group(1), "cap versi markdown.js dan app.js harus sama"
    renderer = (REPO / "app" / "ui" / "markdown.js").read_text(encoding="utf-8")
    # `const Markdown` di skrip biasa BUKAN properti window: tanpa ekspor eksplisit app.js selalu
    # jatuh ke teks mentah (bug nyata: jawaban tampil dengan ## dan | apa adanya).
    assert "window.Markdown = Markdown" in renderer, "markdown.js tidak mengekspos window.Markdown"


def test_the_renderer_is_actually_used_for_answers_and_summaries():
    script = (REPO / "app" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "window.Markdown.render(answer, { citations: true })" in script, "jawaban tidak dirender sebagai Markdown"
    assert "Markdown.render(markdownText)" in script, "ringkasan tidak dirender sebagai Markdown"
    # Jawaban tidak boleh lagi dicetak mentah sebagai teks (itu bug yang diperbaiki).
    assert "bubble.textContent = data.answer" not in script, "jawaban masih dicetak mentah"
    # Ada cara menyalin sebagai Markdown (.md), bukan mengunduh berkas.
    assert "Salin .md" in script, "tidak ada tombol salin Markdown"


def test_the_markdown_renderer_passes_its_own_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node tidak tersedia; uji renderer Markdown dilewati")
    result = subprocess.run(
        [node, str(REPO / "scripts" / "test_markdown.mjs")],
        capture_output=True, text=True, timeout=120, cwd=str(REPO),
    )
    assert result.returncode == 0, f"uji renderer gagal:\n{result.stdout}\n{result.stderr}"
    assert "SEMUA LULUS" in result.stdout
