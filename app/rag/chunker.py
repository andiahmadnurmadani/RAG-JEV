"""Heading/table-aware chunking (PRD 11).

Design notes
------------
* Structure is preserved: a chunk carries the heading path it belongs to, and a
  table is never split unless it alone exceeds the size budget.
* Offsets are exact (character offsets inside the page text) so citations can be
  traced back to the source page.
* Token counts are an approximation (words * 1.34) — deterministic, dependency
  free, and good enough for a size *budget*.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from app.parsing.parser import ParsedDocument, ParsedPage

HEADING_MD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")
HEADING_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+([A-Z][^.!?]{2,80})$")
HEADING_BAB = re.compile(r"^\s*(BAB|Pasal|Bagian|Section|Chapter)\s+([IVXLC\d]+)\b(.*)$", re.IGNORECASE)
# Baris tabel: markdown berpagar (|a|b|), tab-separated, atau "a | b" - bentuk terakhir ini
# yang dihasilkan parser CSV/XLSX kita sendiri ("baris 12: Nama | Nilai | Total"), dan tanpa
# polanya baris tabel dianggap paragraf biasa sehingga satu tabel raksasa jadi satu chunk.
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$|^[^\n\t]*\t[^\n\t]*\t|^\s*[^\n|]+\s\|\s[^\n|]+")
TOKENS_PER_WORD = 1.34


def looks_like_table(block_text: str) -> bool:
    """True when most non-empty lines are table rows: ``|a|b|``, tabs, atau ``a | b | c``.

    Tabel dikenali dari bentuk barisnya, bukan dari jenis berkasnya: CSV/TSV/XLSX/PPT tabel
    semuanya sampai ke sini sebagai teks dengan pemisah ``|``.
    """
    rows = [line.strip() for line in (block_text or "").splitlines() if line.strip()]
    if not rows:
        return False
    matching = sum(1 for row in rows if TABLE_ROW.match(row))
    return matching >= max(1, int(len(rows) * 0.6))


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text.split()) * TOKENS_PER_WORD))


@dataclass
class Block:
    text: str
    kind: str  # heading | table | paragraph
    level: int
    start: int
    end: int


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    content: str
    page: int
    section: str
    char_start: int
    char_end: int
    token_count: int
    is_table: bool = False


def _blocks_for_page(page: ParsedPage) -> List[Block]:
    """Split one page into ordered blocks while preserving position offsets."""
    blocks: List[Block] = []
    cursor = 0
    text = page.text
    # separate on blank lines, keeping exact offsets
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            cursor = match.end()
            continue
        kind, level = "paragraph", 0
        md = HEADING_MD.match(stripped)
        if md:
            kind, level = "heading", len(md.group(1))
        elif HEADING_BAB.match(stripped):
            kind, level = "heading", 1
        elif HEADING_NUM.match(stripped) and len(stripped) < 90:
            kind, level = "heading", 2
        elif looks_like_table(stripped):
            kind = "table"
        blocks.append(Block(text=stripped, kind=kind, level=level, start=match.start(), end=match.end()))
        cursor = match.end()
    return blocks


def _split_table(block: Block, max_tokens: int) -> List[Block]:
    """Tabel raksasa dipotong per baris, baris kepala diulang di setiap potongan.

    Tanpa ini, tabel besar (CSV/rangkuman ribuan baris) menjadi SATU chunk raksasa karena
    pemisahan kalimat tidak menemukan titik/koma di dalam baris tabel - akibatnya isinya
    tidak bisa ditemukan lewat pencarian teks, dan baris pertama (nama kolom) ikut hilang
    dari pandangan potongan berikutnya.
    """

    lines = [line for line in block.text.split("\n") if line.strip()]
    if len(lines) < 2:
        return [block]
    header, body = lines[0], lines[1:]
    budget = max(32, max_tokens - estimate_tokens(header) - 4)
    pieces: List[Block] = []
    current: List[str] = []
    offset = block.start
    for line in body:
        candidate = "\n".join(current + [line])
        if current and estimate_tokens(candidate) > budget:
            body_text = "\n".join(current)
            text = f"{header}\n{body_text}"
            pieces.append(Block(text, block.kind, block.level, offset, offset + len(text)))
            offset += len(body_text) + 1
            current = [line]
        else:
            current.append(line)
    if current:
        body_text = "\n".join(current)
        text = f"{header}\n{body_text}"
        pieces.append(Block(text, block.kind, block.level, offset, offset + len(text)))
    return pieces or [block]


def _split_oversized(block: Block, max_tokens: int) -> List[Block]:
    """Split a block that alone exceeds the budget, on sentence boundaries."""
    if estimate_tokens(block.text) <= max_tokens:
        return [block]
    if block.kind == "table":
        return _split_table(block, max_tokens)
    pieces: List[Block] = []
    sentences = re.split(r"(?<=[.!?])\s+", block.text)
    current: List[str] = []
    offset = block.start
    for sentence in sentences:
        candidate = " ".join(current + [sentence])
        if current and estimate_tokens(candidate) > max_tokens:
            body = " ".join(current)
            pieces.append(Block(body, block.kind, block.level, offset, offset + len(body)))
            offset += len(body) + 1
            current = [sentence]
        else:
            current.append(sentence)
    if current:
        body = " ".join(current)
        pieces.append(Block(body, block.kind, block.level, offset, offset + len(body)))
    return pieces


def chunk_document(
    parsed: ParsedDocument,
    *,
    document_id: str,
    chunk_size: int = 700,
    chunk_overlap: int = 100,
    min_chunk_tokens: int = 40,
) -> List[Chunk]:
    """Turn a parsed document into ordered, structure-aware chunks."""
    chunks: List[Chunk] = []
    heading_stack: List[Tuple[int, str]] = []
    pending: List[Block] = []
    pending_tokens = 0
    pending_page = 1
    counter = 0

    def current_section() -> str:
        return " / ".join(title for _, title in heading_stack)

    def add_block(block: Block) -> None:
        """Track which page the pending buffer belongs to (first block wins)."""
        nonlocal pending_page
        if not pending:
            pending_page = block_page[0]
        pending.append(block)

    def flush() -> None:
        nonlocal pending, pending_tokens, counter
        if not pending:
            return
        content = "\n\n".join(block.text for block in pending)
        tokens = estimate_tokens(content)
        has_heading = any(block.kind == "heading" for block in pending)
        mergeable = (
            tokens < min_chunk_tokens
            and chunks
            and not has_heading
            and estimate_tokens(chunks[-1].content) + tokens <= int(chunk_size * 1.5)
        )
        if mergeable:
            # merge a stray tail into the previous chunk instead of emitting noise;
            # a block that carries its own heading is a section, never a stray tail
            previous = chunks[-1]
            merged = f"{previous.content}\n\n{content}"
            chunks[-1] = Chunk(
                chunk_id=previous.chunk_id,
                document_id=previous.document_id,
                content=merged,
                page=previous.page,
                section=previous.section,
                char_start=previous.char_start,
                char_end=pending[-1].end,
                token_count=estimate_tokens(merged),
                is_table=previous.is_table or any(b.kind == "table" for b in pending),
            )
        else:
            counter += 1
            chunks.append(
                Chunk(
                    chunk_id=f"chunk_{counter:04d}",
                    document_id=document_id,
                    content=content,
                    page=pending_page,
                    section=current_section(),
                    char_start=pending[0].start,
                    char_end=pending[-1].end,
                    token_count=tokens,
                    is_table=any(b.kind == "table" for b in pending),
                )
            )
        pending = []
        pending_tokens = 0

    block_page: List[int] = [1]
    for page in parsed.pages:
        block_page[0] = page.page
        for block in _blocks_for_page(page):
            if block.kind == "heading":
                flush()
                heading_stack = [(lvl, title) for lvl, title in heading_stack if lvl < block.level]
                title = block.text.lstrip("#").strip()
                heading_stack.append((block.level, title))
                add_block(block)
                pending_tokens += estimate_tokens(block.text)
                continue

            for part in _split_oversized(block, chunk_size):
                part_tokens = estimate_tokens(part.text)
                if part.kind == "table" and pending_tokens + part_tokens <= chunk_size * 1.5:
                    add_block(part)
                    pending_tokens += part_tokens
                    continue
                if pending_tokens + part_tokens > chunk_size and pending:
                    flush()
                add_block(part)
                pending_tokens += part_tokens
                if part.kind != "table" and pending_tokens >= chunk_size:
                    flush()
    flush()

    if chunk_overlap > 0 and len(chunks) > 1:
        chunks = _apply_overlap(chunks, chunk_overlap)
    return chunks


def _apply_overlap(chunks: Sequence[Chunk], overlap_tokens: int) -> List[Chunk]:
    """Prepend the tail of the previous chunk so boundaries are not hard cuts.

    Untuk chunk tabel, baris kepala tetap di baris pertama: potongan ekor dari chunk sebelumnya
    ditaruh SETELAH baris kepala, supaya pembaca (dan pengambil konteks) selalu tahu nama kolomnya.
    """
    out: List[Chunk] = []
    for index, chunk in enumerate(chunks):
        if index == 0:
            out.append(chunk)
            continue
        previous_words = chunks[index - 1].content.split()
        tail_words = max(0, len(previous_words) - int(overlap_tokens / TOKENS_PER_WORD))
        tail = " ".join(previous_words[tail_words:]).strip()
        if tail and chunk.is_table:
            head, _, rest = chunk.content.partition("\n")
            content = f"{head}\n{tail}\n{rest}" if rest else f"{head}\n{tail}"
        else:
            content = f"{tail}\n\n{chunk.content}" if tail else chunk.content
        out.append(
            Chunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                content=content,
                page=chunk.page,
                section=chunk.section,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                token_count=estimate_tokens(content),
                is_table=chunk.is_table,
            )
        )
    return out


def chunk_text(
    text: str,
    *,
    document_id: str,
    chunk_size: int = 700,
    chunk_overlap: int = 100,
) -> List[Chunk]:
    """Convenience wrapper for plain strings (tests, ad-hoc indexing)."""
    parsed = ParsedDocument(document_name="inline", pages=[ParsedPage(page=1, text=text)])
    return chunk_document(parsed, document_id=document_id, chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def find_page_for_chunk(pages: Sequence[ParsedPage], chunk: Chunk) -> Optional[int]:
    for page in pages:
        if chunk.content[:60] and chunk.content[:60] in page.text:
            return page.page
    return None
