"""Episode creation: one episode -> one revision -> one GENERATE run -> one reservation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    DailyPlan,
    Episode,
    EpisodeRevision,
    GenerationRun,
    PlanItem,
    RevisionStatus,
    RunKind,
    RunState,
    Source,
    SourceVersion,
    UserProfile,
)
from app.services import planning, runs


@dataclass(slots=True)
class EpisodeBundle:
    episode: Episode
    revision: EpisodeRevision
    run: GenerationRun
    plan: DailyPlan
    item: PlanItem | None
    deferred: bool


async def latest_ready_version(
    session: AsyncSession, *, source_id: uuid.UUID
) -> SourceVersion | None:
    from app.models import IndexState

    rows = await session.execute(
        select(SourceVersion)
        .where(
            SourceVersion.source_id == source_id,
            SourceVersion.index_state == IndexState.READY,
        )
        .order_by(SourceVersion.fetched_at.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def create_episode_with_run(
    session: AsyncSession,
    *,
    owner: UserProfile,
    source: Source,
    question: str,
    settings: Settings,
    request_key_suffix: str = "",
) -> EpisodeBundle:
    prefs = dict(owner.prefs or {})
    local_date = planning.local_date_for(owner)
    plan = await planning.get_or_create_plan(
        session,
        owner_id=owner.id,
        local_date=local_date,
        timezone_name=owner.timezone,
        budget_ms=int(prefs.get("daily_budget_ms", settings.daily_budget_ms)),
        prefs=prefs,
    )
    episode = Episode(
        owner_id=owner.id,
        primary_source_id=source.id,
        question=question,
        guid=uuid.uuid4().hex,
    )
    session.add(episode)
    await session.flush()
    revision = EpisodeRevision(
        episode_id=episode.id,
        revision_no=1,
        status=RevisionStatus.BUILDING,
        config_snapshot={
            "model": settings.llm_model,
            "embedding": settings.embedding_model,
            "tts": settings.tts_model,
            "voice": settings.tts_voice,
            "prefs_version": owner.prefs_version,
            "max_episode_ms": settings.max_episode_ms,
            "target_ms": settings.target_episode_ms,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(revision)
    await session.flush()

    run, _created = await runs.create_run(
        session,
        runs.RunSpec(
            owner_id=owner.id,
            kind=RunKind.GENERATE,
            request_key=f"gen:{revision.id}{request_key_suffix}",
            payload_hash="",
            source_id=source.id,
            episode_id=episode.id,
            revision_id=revision.id,
            phase=None,
        ),
        settings,
    )
    item = await planning.reserve_episode(
        session, plan=plan, episode=episode, settings=settings
    )
    deferred = item is None
    if deferred:
        run.state = RunState.DEFERRED
        run.last_error = "plan_budget_exhausted"
    return EpisodeBundle(
        episode=episode, revision=revision, run=run, plan=plan, item=item, deferred=deferred
    )


async def review_snapshot(session: AsyncSession, revision: EpisodeRevision) -> dict:
    return dict((revision.config_snapshot or {}).get("review") or {})
