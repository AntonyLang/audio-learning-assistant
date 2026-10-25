from __future__ import annotations

from app.services.chunking import build_blocks, chunk_blocks
from app.services.textproc import normalize_text

BODY = normalize_text(
    """# 一级标题

## 小节一

第一段内容。它包含若干句子，用来测试切块边界。这句话稍微长一点，以便让切块有足够的材料。

## 小节二

第二段内容。这里继续增加文字，确保长度足够。再补一句，让段落长度接近切块上限的下限要求。

### 子小节

第三段内容。PDF 页码需要被保留，HTML 段落需要被定位。切块必须严格是原文的切片。

## 小节三

第四段内容。最后一段用于检查重叠部分是否落在合法范围内，并且不会把块起始位置推到自身之前。
"""
)


def test_chunks_are_exact_slices_of_the_body():
    blocks = build_blocks(BODY)
    chunks = chunk_blocks(BODY, blocks, min_chars=40, max_chars=120, overlap_chars=20)
    assert chunks
    for chunk in chunks:
        assert BODY[chunk.start_char : chunk.end_char] == chunk.text
        assert chunk.end_char > chunk.start_char


def test_chunk_size_respects_the_upper_bound():
    blocks = build_blocks(BODY)
    chunks = chunk_blocks(BODY, blocks, min_chars=40, max_chars=120, overlap_chars=20)
    assert all(len(chunk.text) <= 120 for chunk in chunks)


def test_overlap_is_block_aligned_and_never_rewinds_the_start():
    """Overlap is applied at block boundaries, so it may be zero for a long block."""
    blocks = build_blocks(BODY)
    chunks = chunk_blocks(BODY, blocks, min_chars=40, max_chars=100, overlap_chars=30)
    assert len(chunks) >= 2
    for previous, following in zip(chunks, chunks[1:], strict=False):
        assert following.start_char > previous.start_char
        if following.start_char < previous.end_char:
            shared = previous.end_char - following.start_char
            assert following.text.startswith(BODY[following.start_char : previous.end_char])
            assert shared <= 30


def test_section_path_tracks_headings():
    blocks = build_blocks(BODY)
    paths = {block.section_path for block in blocks}
    assert any("小节一" in path for path in paths)
    assert any("子小节" in path for path in paths)


def test_long_paragraph_is_split_on_sentence_boundaries():
    single = "这是一个很长的段落。" * 40
    body = normalize_text(single)
    blocks = build_blocks(body)
    chunks = chunk_blocks(body, blocks, min_chars=50, max_chars=120, overlap_chars=0)
    assert all(len(chunk.text) <= 120 for chunk in chunks)
    assert all(BODY[0:0] == body[chunk.start_char : chunk.end_char][0:0] for chunk in chunks)
    for chunk in chunks:
        assert body[chunk.start_char : chunk.end_char] == chunk.text


def test_page_numbers_come_from_the_pdf_layout():
    first = "第一页内容。"
    second = "第二页内容。"
    body = f"{first}\n\n{second}"
    pages = [(1, 0, len(first)), (2, len(first) + 2, len(body))]
    blocks = build_blocks(body, pages)
    assert blocks[0].page_no == 1
    assert blocks[1].page_no == 2
