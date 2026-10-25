from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.feeds import build_rss, parse_single_range
from app.config import get_settings


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 1000, None),
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=900-", 1000, (900, 999)),
        ("bytes=-100", 1000, (900, 999)),
        ("bytes=0-5000", 1000, (0, 999)),
        ("bytes=0-10,20-30", 1000, None),
        ("items=0-10", 1000, None),
    ],
)
def test_parse_single_range(header, size, expected):
    assert parse_single_range(header, size) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=abc-def", "bytes=10-5", "bytes=-0"])
def test_parse_single_range_rejects_unsatisfiable(header):
    with pytest.raises(ValueError):
        parse_single_range(header, 1000)


def test_rss_uses_stable_guid_and_escaped_enclosure():
    settings = get_settings()
    episode_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    item = {
        "episode": type(
            "E",
            (),
            {
                "id": episode_id,
                "guid": "guid-abc",
                "question": "超时意味着什么 & 该怎么做？",
                "published_at": datetime(2026, 10, 25, tzinfo=UTC),
            },
        )(),
        "revision": type("R", (), {"id": revision_id, "created_at": datetime.now(UTC)})(),
        "source": type("S", (), {"title": "source"})(),
        "artifact": type("A", (), {"duration_ms": 123_456, "byte_size": 999})(),
    }
    xml = build_rss([item], token="tok123", settings=settings)
    text = xml.decode("utf-8")
    assert "guid-abc" in text
    assert "&amp;" in text
    assert f"/v1/feeds/tok123/episodes/{episode_id}/audio/{revision_id}.mp3" in text
    assert 'type="audio/mpeg"' in text
    assert "2:03" in text
