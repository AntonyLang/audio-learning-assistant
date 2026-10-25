"""Per-run call budget (doc §5.2).

The database row is the authority: a counter is reserved under ``FOR UPDATE`` *before* the
external call and is never refunded for a failed call, so a retry cannot buy extra
queries. The model may suggest continuing, but only this module decides whether it may.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.adapters.base import Usage
from app.config import Settings, get_settings
from app.db import session_scope
from app.models import GenerationRun

KEY_SEARCHES = "searches"
KEY_PAGES = "pages"
KEY_FACT_REVISIONS = "fact_revisions"
KEY_AUDIO_REVISIONS = "audio_revisions"
KEY_TTS_CHARS = "tts_chars"
KEY_TTS_PASSES = "tts_passes"
KEY_TOKENS_IN = "tokens_in"
KEY_TOKENS_OUT = "tokens_out"


class BudgetExceeded(RuntimeError):
    def __init__(self, key: str) -> None:
        super().__init__(f"budget exceeded: {key}")
        self.key = key


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    usage: dict
    deadline_at: datetime | None

    def total_tokens(self) -> int:
        return int(self.usage.get(KEY_TOKENS_IN, 0)) + int(self.usage.get(KEY_TOKENS_OUT, 0))


def limit_for(key: str, settings: Settings) -> int:
    return {
        KEY_SEARCHES: settings.max_searches_per_episode,
        KEY_PAGES: settings.max_pages_per_episode,
        KEY_FACT_REVISIONS: settings.max_fact_revisions,
        KEY_AUDIO_REVISIONS: settings.max_audio_revisions,
        KEY_TTS_PASSES: 2,
        KEY_TOKENS_IN: settings.llm_token_budget,
        KEY_TOKENS_OUT: settings.llm_token_budget,
    }.get(key, 1 << 62)


async def _load_locked(session, run_id: uuid.UUID) -> GenerationRun:
    from sqlalchemy import select

    result = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    return result.scalar_one()


async def snapshot(run_id: uuid.UUID) -> BudgetSnapshot:
    async with session_scope() as session:
        run = await session.get(GenerationRun, run_id)
        return BudgetSnapshot(usage=dict(run.budget_usage or {}), deadline_at=run.deadline_at)


async def can_consume(run_id: uuid.UUID, key: str, amount: int = 1) -> bool:
    snap = await snapshot(run_id)
    settings = get_settings()
    if key in (KEY_TOKENS_IN, KEY_TOKENS_OUT):
        return snap.total_tokens() + amount <= settings.llm_token_budget
    return int(snap.usage.get(key, 0)) + amount <= limit_for(key, settings)


async def consume(run_id: uuid.UUID, key: str, amount: int = 1) -> None:
    """Reserve budget atomically, or raise :class:`BudgetExceeded`."""
    settings = get_settings()
    async with session_scope() as session:
        run = await _load_locked(session, run_id)
        usage = dict(run.budget_usage or {})
        if key in (KEY_TOKENS_IN, KEY_TOKENS_OUT):
            total = int(usage.get(KEY_TOKENS_IN, 0)) + int(usage.get(KEY_TOKENS_OUT, 0))
            if total + amount > settings.llm_token_budget:
                raise BudgetExceeded(key)
        elif int(usage.get(key, 0)) + amount > limit_for(key, settings):
            raise BudgetExceeded(key)
        usage[key] = int(usage.get(key, 0)) + amount
        run.budget_usage = usage


async def record_usage(run_id: uuid.UUID, usage: Usage) -> None:
    async with session_scope() as session:
        run = await _load_locked(session, run_id)
        current = dict(run.budget_usage or {})
        current[KEY_TOKENS_IN] = int(current.get(KEY_TOKENS_IN, 0)) + usage.tokens_in
        current[KEY_TOKENS_OUT] = int(current.get(KEY_TOKENS_OUT, 0)) + usage.tokens_out
        current[KEY_TTS_CHARS] = int(current.get(KEY_TTS_CHARS, 0)) + usage.characters
        run.budget_usage = current


async def deadline_reached(run_id: uuid.UUID) -> bool:
    snap = await snapshot(run_id)
    if snap.deadline_at is None:
        return False
    deadline = snap.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) >= deadline

async def reserve_tokens(run_id: uuid.UUID, estimate: int) -> int:
    """Reserve a conservative token estimate before an LLM call.

    Returns the reserved amount so :func:`settle_tokens` can reconcile it with the
    provider's reported usage afterwards.
    """
    settings = get_settings()
    estimate = max(1, int(estimate))
    async with session_scope() as session:
        run = await _load_locked(session, run_id)
        usage = dict(run.budget_usage or {})
        total = int(usage.get(KEY_TOKENS_IN, 0)) + int(usage.get(KEY_TOKENS_OUT, 0))
        if total + estimate > settings.llm_token_budget:
            raise BudgetExceeded(KEY_TOKENS_IN)
        usage[KEY_TOKENS_IN] = int(usage.get(KEY_TOKENS_IN, 0)) + estimate
        run.budget_usage = usage
    return estimate


async def settle_tokens(run_id: uuid.UUID, reserved: int, usage: Usage) -> None:
    """Replace the reservation with the measured usage (may be smaller)."""
    async with session_scope() as session:
        run = await _load_locked(session, run_id)
        current = dict(run.budget_usage or {})
        tokens_in = int(current.get(KEY_TOKENS_IN, 0)) - int(reserved) + usage.tokens_in
        current[KEY_TOKENS_IN] = max(0, tokens_in)
        current[KEY_TOKENS_OUT] = int(current.get(KEY_TOKENS_OUT, 0)) + usage.tokens_out
        current[KEY_TTS_CHARS] = int(current.get(KEY_TTS_CHARS, 0)) + usage.characters
        run.budget_usage = current
