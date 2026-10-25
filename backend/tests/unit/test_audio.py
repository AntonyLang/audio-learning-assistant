from __future__ import annotations

import pytest

from app.config import get_settings
from app.services.audio import (
    AudioError,
    duration_to_ms,
    enforce_duration,
    normalize_spoken,
    plan_segments,
    revision_target_seconds,
    segment_fingerprint,
)


def test_segments_stay_under_the_provider_limit():
    spoken = "这一句话用来测试分段。" * 120
    plans = plan_segments(spoken, max_chars=400)
    assert plans
    assert all(len(plan.text) <= 400 for plan in plans)
    assert [plan.index for plan in plans] == list(range(len(plans)))


def test_segments_keep_numbers_conditions_and_negation():
    spoken = "当 duration_ms 不超过 300000 时通过；超过时不截断、不倍速。"
    plans = plan_segments(spoken, max_chars=400)
    joined = "".join(plan.text for plan in plans)
    for token in ("300000", "不超过", "不截断", "不倍速"):
        assert token in joined


def test_segments_cover_the_spoken_text_in_order():
    spoken = "第一句。第二句！第三句？"
    plans = plan_segments(spoken, max_chars=400)
    joined = "".join(plan.text for plan in plans)
    assert joined == normalize_spoken(spoken)
    for previous, following in zip(plans, plans[1:], strict=False):
        assert following.start_char >= previous.end_char - 1


def test_duration_is_rounded_up_to_whole_milliseconds():
    assert duration_to_ms("4.0004") == 4001
    assert duration_to_ms("270.000") == 270_000


def test_unparseable_duration_is_an_error():
    with pytest.raises(AudioError):
        duration_to_ms("not-a-number")
    with pytest.raises(AudioError):
        duration_to_ms("0")


def test_duration_gate_is_inclusive_at_the_limit():
    settings = get_settings()
    enforce_duration(settings.max_episode_ms, settings)
    with pytest.raises(AudioError) as error:
        enforce_duration(settings.max_episode_ms + 1, settings)
    assert error.value.code == "duration_exceeded"


def test_revision_target_shrinks_proportionally():
    settings = get_settings()
    target = revision_target_seconds(1200, 400_000, settings)
    assert 60 <= target < 1200 * 0.8
    assert revision_target_seconds(500, 0, settings) == settings.target_episode_ms // 1000


def test_segment_fingerprint_covers_voice_and_text():
    settings = get_settings()
    owner = __import__("uuid").uuid4()
    first = segment_fingerprint(
        owner_id=owner, spoken="同一句话", settings=settings, voice="Cherry", language="Chinese"
    )
    same = segment_fingerprint(
        owner_id=owner, spoken="同一句话", settings=settings, voice="Cherry", language="Chinese"
    )
    other_voice = segment_fingerprint(
        owner_id=owner, spoken="同一句话", settings=settings, voice="Ethan", language="Chinese"
    )
    assert first == same
    assert first != other_voice
