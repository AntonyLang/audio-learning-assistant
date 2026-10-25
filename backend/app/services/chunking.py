"""Paragraph-aware chunking with character-accurate spans (doc §4.2).

Chunks are *slices of the stored normalised body text*: ``body_text[start_char:end_char]``
is always exactly the chunk text. That invariant is what lets a citation highlight the
same characters the model saw, and it is asserted by ``tests/unit/test_chunking.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.textproc import sha256_text, split_sentences

_HEADING_RE = re.compile(r"^\s*(#{1,6}\s+|\d+(?:\.\d+){0,3}[、.\s])\S")


@dataclass(slots=True)
class Block:
    text: str
    start: int
    end: int
    page_no: int | None
    section_path: str
    paragraph_id: str


@dataclass(slots=True)
class ChunkDraft:
    ordinal: int
    text: str
    start_char: int
    end_char: int
    section_path: str
    page_no: int | None
    paragraph_id: str
    content_hash: str = ""
    lexemes: str = ""
    embedding: list[float] | None = None
    extra: dict = field(default_factory=dict)


def build_blocks(body: str, pages: list[tuple[int, int, int]] | None = None) -> list[Block]:
    """Split the body into paragraph blocks, tracking page numbers from ``pages``.

    ``pages`` is a list of ``(page_no, start, end)`` offsets produced by the PDF extractor.
    """
    blocks: list[Block] = []
    section_path: list[str] = []
    page_bounds = pages or []
    cursor = 0
    para_index = 0
    for raw_line in body.split("\n"):
        line_start = cursor
        cursor += len(raw_line) + 1
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _is_heading(stripped):
            level = _heading_level(stripped)
            section_path = section_path[: level - 1]
            while len(section_path) < level - 1:
                section_path.append("")
            section_path.append(_heading_text(stripped))
            continue
        start = line_start + len(raw_line) - len(raw_line.lstrip())
        end = start + len(stripped)
        para_index += 1
        blocks.append(
            Block(
                text=stripped,
                start=start,
                end=end,
                page_no=_page_of(start, page_bounds),
                section_path=" / ".join(p for p in section_path if p),
                paragraph_id=f"p{para_index:04d}",
            )
        )
    return blocks


def _is_heading(line: str) -> bool:
    if len(line) > 80:
        return False
    if line.startswith("#"):
        return True
    if _HEADING_RE.match(line) and len(line) <= 40:
        return True
    return False


def _heading_level(line: str) -> int:
    if line.startswith("#"):
        return min(len(line) - len(line.lstrip("#")), 6)
    return 1


def _heading_text(line: str) -> str:
    return line.lstrip("#").strip()


def _page_of(offset: int, pages: list[tuple[int, int, int]]) -> int | None:
    for page_no, start, end in pages:
        if start <= offset < end:
            return page_no
    return None


def chunk_blocks(
    body: str,
    blocks: list[Block],
    *,
    min_chars: int = 400,
    max_chars: int = 700,
    overlap_chars: int = 80,
) -> list[ChunkDraft]:
    """Merge blocks into 400..700 char chunks with a small overlap.

    A block longer than ``max_chars`` is split on sentence boundaries; the resulting
    pieces stay slices of ``body`` so spans remain exact.
    """
    units: list[Block] = []
    for block in blocks:
        if len(block.text) <= max_chars:
            units.append(block)
            continue
        units.extend(_split_block(body, block, max_chars))

    drafts: list[ChunkDraft] = []
    index = 0
    ordinal = 0
    while index < len(units):
        first = units[index]
        last = first
        index += 1
        while index < len(units):
            candidate = units[index]
            projected = candidate.end - first.start
            if projected > max_chars:
                break
            # keep a chunk inside one section when possible
            if first.section_path and candidate.section_path != first.section_path:
                if last.end - first.start >= min_chars:
                    break
            last = candidate
            index += 1

        start, end = first.start, last.end
        text = body[start:end]
        drafts.append(
            ChunkDraft(
                ordinal=ordinal,
                text=text,
                start_char=start,
                end_char=end,
                section_path=first.section_path,
                page_no=first.page_no,
                paragraph_id=first.paragraph_id,
                content_hash=sha256_text(text),
                extra={"block_span": [first.start, last.end]},
            )
        )
        ordinal += 1

        # Step back for overlap, but never rewind past the start of the current chunk.
        if index < len(units) and overlap_chars > 0:
            target = max(start + 1, end - overlap_chars)
            back = index
            while back > 0 and units[back - 1].start >= target:
                back -= 1
            if back > 0:
                index = back
    return drafts


def _split_block(body: str, block: Block, max_chars: int) -> list[Block]:
    pieces: list[Block] = []
    offset = block.start
    sentence_buffer = ""
    for sentence in split_sentences(block.text):
        if sentence_buffer and len(sentence_buffer) + len(sentence) > max_chars:
            start = offset
            end = start + len(sentence_buffer)
            pieces.append(
                Block(
                    text=body[start:end],
                    start=start,
                    end=end,
                    page_no=block.page_no,
                    section_path=block.section_path,
                    paragraph_id=block.paragraph_id,
                )
            )
            offset = end
            sentence_buffer = ""
        sentence_buffer += sentence
    if sentence_buffer:
        start = offset
        end = start + len(sentence_buffer)
        pieces.append(
            Block(
                text=body[start:end],
                start=start,
                end=end,
                page_no=block.page_no,
                section_path=block.section_path,
                paragraph_id=block.paragraph_id,
            )
        )
    return pieces
