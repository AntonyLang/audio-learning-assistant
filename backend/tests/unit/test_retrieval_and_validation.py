from __future__ import annotations

import uuid

from app.schemas.content import ScriptDoc, ScriptSegment
from app.services.retrieval import (
    LoadedChunk,
    build_tsquery,
    rrf_fuse,
    select_quote,
    sentence_spans,
)
from app.services.validation import merge_review, validate_script_structure


def test_tsquery_uses_controlled_terms_and_drops_punctuation():
    query = build_tsquery("调用超时为什么不能断定远端没有完成？", max_terms=4)
    assert "&" in query
    assert "？" not in query
    assert len(query.split(" & ")) <= 4


def test_tsquery_is_empty_for_punctuation_only():
    assert build_tsquery("？？？") == ""


def test_rrf_fuses_ranks_not_scores():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fused = rrf_fuse({"keyword": [a, b], "semantic": [b, c]}, top=3)
    order = [item for item, _score, _ranks in fused]
    assert order[0] == b  # present in both channels
    scores = {item: score for item, score, _ranks in fused}
    assert scores[b] > scores[a]
    assert abs(scores[b] - (1 / 62 + 1 / 61)) < 1e-9


def test_rrf_top_k_truncates():
    ids = [uuid.uuid4() for _ in range(20)]
    fused = rrf_fuse({"keyword": ids}, top=8)
    assert len(fused) == 8


def test_sentence_spans_are_contiguous_and_ordered():
    text = "第一句。第二句！第三句？"
    spans = sentence_spans(text)
    assert spans[0][0] == 0
    for (_s1, e1), (s2, _e2) in zip(spans, spans[1:], strict=False):
        assert s2 >= e1


def test_select_quote_returns_absolute_offsets_inside_the_body():
    body = "前言。调用超时不能证明远端没有完成，需要先检查回执。后记。"
    chunk = LoadedChunk(
        id=uuid.uuid4(),
        source_version_id=uuid.uuid4(),
        ordinal=0,
        text=body,
        start_char=0,
        end_char=len(body),
        section_path="",
        page_no=None,
        paragraph_id="p0001",
    )
    start, end, quote = select_quote(chunk, "调用超时能不能证明远端完成")
    assert 0 <= start < end <= len(body)
    assert body[start:end] == quote
    assert "超时" in quote


def _script(review: str = "NOT_FOUND") -> tuple[ScriptDoc, list[dict]]:
    vid, cid = uuid.uuid4(), uuid.uuid4()
    claim = {
        "claim_id": "c01",
        "text": "调用超时并不能证明远端没有完成",
        "kind": "external_fact",
        "critical": True,
        "evidence": [
            {
                "source_version_id": str(vid),
                "chunk_id": str(cid),
                "span_start": 3,
                "span_end": 23,
                "quote": "调用超时并不能证明远端没有完成",
                "relation": "supports",
            }
        ],
        "review": review,
    }
    pack_items = [
        {
            "source_version_id": vid,
            "chunk_id": cid,
            "span_start": 3,
            "span_end": 23,
            "quote": "调用超时并不能证明远端没有完成",
            "relation": "supports",
            "origin": "primary",
        }
    ]
    script = ScriptDoc(
        episode_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        core_question="超时意味着什么？",
        one_line_answer="意味着结果未知。",
        segments=[
            ScriptSegment(
                segment_id="s1", heading="", spoken_text="调用超时并不能证明远端没有完成", claim_ids=["c01"]
            )
        ],
        claims=[claim],
        spoken_text="调用超时并不能证明远端没有完成",
        char_count=16,
    )
    return script, pack_items


def test_script_structure_rejects_references_outside_the_pack():
    script, items = _script()
    from app.schemas.content import EvidencePack

    pack = EvidencePack(
        pack_id="p1",
        primary_version_id=uuid.uuid4(),
        items=[{**item} for item in items],
    )
    assert validate_script_structure(script, pack) == []
    broken = script.model_copy(
        update={
            "claims": [
                script.claims[0].model_copy(
                    update={
                        "evidence": [
                            script.claims[0].evidence[0].model_copy(update={"span_start": 9})
                        ]
                    }
                )
            ]
        }
    )
    problems = validate_script_structure(broken, pack)
    assert any(problem.code == "ref_not_in_pack" for problem in problems)


def test_merge_review_blocks_critical_unsupported_claims():
    script, _items = _script()
    updated, ok, blocking = merge_review(
        script, [{"claim_id": "c01", "verdict": "PARTIAL", "reason": "改写"}]
    )
    assert ok is False
    assert blocking == ["c01"]
    assert updated.claims[0].review == "PARTIAL"

    updated, ok, blocking = merge_review(
        script, [{"claim_id": "c01", "verdict": "SUPPORTED", "reason": "逐字一致"}]
    )
    assert ok is True
    assert blocking == []
