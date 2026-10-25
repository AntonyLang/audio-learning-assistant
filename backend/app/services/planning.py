"""Daily listening plan: budget reservations and reuse of already-ready episodes (doc §9.1).

The plan is a *playback-time budget*, not a to-do list. A new episode reserves 300 s up
front, the reservation is replaced by the measured duration on publication, and an
episode that was already scheduled never returns budget when it is played or the page is
refreshed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    DailyPlan,
    Episode,
    GenerationRun,
    PlanItem,
    PlanItemState,
    RunState,
    UserProfile,
)
from app.services import runs
from app.services.events import append_event


@dataclass(slots=True)
class PlanTotals:
    budget_ms: int
    reserved_ms: int
    available_ms: int

    @property
    def committed_ms(self) -> int:
        return self.reserved_ms + self.available_ms

    @property
    def remaining_ms(self) -> int:
        return self.budget_ms - self.committed_ms


def local_date_for(profile: UserProfile, now: datetime | None = None) -> date:
    from zoneinfo import ZoneInfo

    moment = now or datetime.now(UTC)
    return moment.astimezone(ZoneInfo(profile.timezone or "Asia/Shanghai")).date()


async def get_or_create_plan(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    local_date: date,
    timezone_name: str,
    budget_ms: int,
    prefs: dict | None = None,
) -> DailyPlan:
    existing = await session.execute(
        select(DailyPlan)
        .where(DailyPlan.owner_id == owner_id, DailyPlan.local_date == local_date)
        .with_for_update()
    )
    plan = existing.scalar_one_or_none()
    if plan is not None:
        return plan
    plan = DailyPlan(
        owner_id=owner_id,
        local_date=local_date,
        timezone=timezone_name,
        budget_ms=budget_ms,
        plan_version=1,
        prefs_snapshot=prefs or {},
    )
    session.add(plan)
    try:
        async with session.begin_nested():
            await session.flush()
    except Exception:
        session.expunge(plan)
        rows = await session.execute(
            select(DailyPlan)
            .where(DailyPlan.owner_id == owner_id, DailyPlan.local_date == local_date)
            .with_for_update()
        )
        return rows.scalar_one()
    return plan


async def totals(session: AsyncSession, plan: DailyPlan) -> PlanTotals:
    rows = await session.execute(
        select(PlanItem.state, PlanItem.reserved_ms, PlanItem.duration_ms).where(
            PlanItem.plan_id == plan.id
        )
    )
    reserved = 0
    available = 0
    for state, reserved_ms, duration_ms in rows:
        if state == PlanItemState.RESERVED:
            reserved += int(reserved_ms or 0)
        elif state == PlanItemState.AVAILABLE:
            available += int(duration_ms or reserved_ms or 0)
    return PlanTotals(budget_ms=plan.budget_ms, reserved_ms=reserved, available_ms=available)


async def reserve_episode(
    session: AsyncSession,
    *,
    plan: DailyPlan,
    episode: Episode,
    settings: Settings,
) -> PlanItem | None:
    """Reserve playback time for a new episode, or return ``None`` when the day is full."""
    existing = await session.execute(
        select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.episode_id == episode.id)
    )
    if existing.scalar_one_or_none() is not None:
        return None
    current = await totals(session, plan)
    if current.remaining_ms < settings.episode_reservation_ms:
        return None
    item = PlanItem(
        plan_id=plan.id,
        episode_id=episode.id,
        source_id=episode.primary_source_id,
        position=await next_position(session, plan.id),
        state=PlanItemState.RESERVED,
        reserved_ms=settings.episode_reservation_ms,
    )
    session.add(item)
    await session.flush()
    return item


async def attach_ready_episode(
    session: AsyncSession, *, plan: DailyPlan, episode: Episode, settings: Settings
) -> PlanItem | None:
    """Add an existing, already-published episode to today's plan at its real duration."""
    existing = await session.execute(
        select(PlanItem).where(PlanItem.plan_id == plan.id, PlanItem.episode_id == episode.id)
    )
    if existing.scalar_one_or_none() is not None:
        return None
    duration = await published_duration_ms(session, episode)
    if duration is None:
        return None
    current = await totals(session, plan)
    if current.remaining_ms < duration:
        return None
    item = PlanItem(
        plan_id=plan.id,
        episode_id=episode.id,
        source_id=episode.primary_source_id,
        position=await next_position(session, plan.id),
        state=PlanItemState.AVAILABLE,
        reserved_ms=duration,
        duration_ms=duration,
    )
    session.add(item)
    await session.flush()
    return item


async def next_position(session: AsyncSession, plan_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(PlanItem.position), 0)).where(PlanItem.plan_id == plan_id)
    )
    return int(result.scalar_one()) + 1


async def published_duration_ms(session: AsyncSession, episode: Episode) -> int | None:
    from app.models import EpisodeRevision, StageArtifact

    if episode.published_revision_id is None:
        return None
    row = await session.execute(
        select(StageArtifact.duration_ms)
        .join(EpisodeRevision, EpisodeRevision.audio_id == StageArtifact.id)
        .where(EpisodeRevision.id == episode.published_revision_id)
    )
    value = row.scalar_one_or_none()
    return int(value) if value is not None else None


async def promote_deferred_runs(
    session: AsyncSession, *, plan: DailyPlan, settings: Settings
) -> list[uuid.UUID]:
    """DEFERRED -> QUEUED while the plan still has room (doc §8.2)."""
    current = await totals(session, plan)
    promoted: list[uuid.UUID] = []
    if current.remaining_ms < settings.episode_reservation_ms:
        return promoted
    rows = await session.execute(
        select(GenerationRun)
        .where(
            GenerationRun.owner_id == plan.owner_id,
            GenerationRun.state == RunState.DEFERRED,
        )
        .order_by(GenerationRun.created_at)
        .with_for_update(skip_locked=True)
    )
    for run in rows.scalars():
        if current.remaining_ms < settings.episode_reservation_ms:
            break
        if await runs.requeue_deferred(session, run.id):
            promoted.append(run.id)
            current = PlanTotals(
                budget_ms=current.budget_ms,
                reserved_ms=current.reserved_ms + settings.episode_reservation_ms,
                available_ms=current.available_ms,
            )
    return promoted


async def today_snapshot(session: AsyncSession, *, owner_id: uuid.UUID, plan: DailyPlan) -> dict:
    rows = await session.execute(
        select(PlanItem, Episode)
        .join(Episode, Episode.id == PlanItem.episode_id)
        .where(PlanItem.plan_id == plan.id)
        .order_by(PlanItem.position)
    )
    items = []
    for item, episode in rows.all():
        items.append(
            {
                "episode_id": str(episode.id),
                "position": item.position,
                "state": item.state.value,
                "reserved_ms": item.reserved_ms,
                "duration_ms": item.duration_ms,
                "question": episode.question,
                "published": episode.published_revision_id is not None,
            }
        )
    current = await totals(session, plan)
    return {
        "local_date": plan.local_date.isoformat(),
        "plan_id": str(plan.id),
        "budget_ms": plan.budget_ms,
        "reserved_ms": current.reserved_ms,
        "available_ms": current.available_ms,
        "total_ms": current.committed_ms,
        "items": items,
    }


async def mark_available(
    session: AsyncSession, *, plan_id: uuid.UUID, episode_id: uuid.UUID, duration_ms: int
) -> PlanItem | None:
    rows = await session.execute(
        select(PlanItem)
        .where(PlanItem.plan_id == plan_id, PlanItem.episode_id == episode_id)
        .with_for_update()
    )
    item = rows.scalar_one_or_none()
    if item is None:
        return None
    item.state = PlanItemState.AVAILABLE
    item.duration_ms = duration_ms
    item.reserved_ms = duration_ms
    await session.flush()
    return item


async def release_reservation(session: AsyncSession, *, episode_id: uuid.UUID) -> int:
    """Drop a reservation whose run failed for good, so the day's budget is not leaked."""
    rows = await session.execute(
        select(PlanItem)
        .where(PlanItem.episode_id == episode_id, PlanItem.state == PlanItemState.RESERVED)
        .with_for_update()
    )
    released = 0
    for item in rows.scalars():
        item.state = PlanItemState.DROPPED
        item.reserved_ms = 0
        released += 1
    await session.flush()
    return released


async def defer_item(session: AsyncSession, *, plan_id: uuid.UUID, episode_id: uuid.UUID) -> None:
    rows = await session.execute(
        select(PlanItem)
        .where(PlanItem.plan_id == plan_id, PlanItem.episode_id == episode_id)
        .with_for_update()
    )
    item = rows.scalar_one_or_none()
    if item is None:
        return
    item.state = PlanItemState.DEFERRED
    await session.flush()


async def log_plan_event(session: AsyncSession, run_id: uuid.UUID, payload: dict) -> None:
    await append_event(session, run_id, "plan.updated", payload)
