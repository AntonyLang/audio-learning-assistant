"""Destructive helpers used by the demo and the evaluation harness.

They are deliberately explicit: only rows owned by a known account are removed, and the
content-addressed object files are kept so a re-run can demonstrate artifact reuse.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Chunk,
    DailyPlan,
    Episode,
    EpisodeRevision,
    FeedbackEvent,
    FeedToken,
    GenerationRun,
    IdempotencyKey,
    PlanItem,
    PlaybackEvent,
    ProviderCall,
    RunAttempt,
    RunEvent,
    Session,
    Source,
    SourceVersion,
    StageArtifact,
)


async def reset_owner(session: AsyncSession, owner_id: uuid.UUID) -> None:
    run_ids = [
        row[0]
        for row in (
            await session.execute(select(GenerationRun.id).where(GenerationRun.owner_id == owner_id))
        ).all()
    ]
    if run_ids:
        await session.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
        await session.execute(delete(ProviderCall).where(ProviderCall.run_id.in_(run_ids)))
        await session.execute(delete(RunAttempt).where(RunAttempt.run_id.in_(run_ids)))
        await session.execute(delete(GenerationRun).where(GenerationRun.id.in_(run_ids)))
    episode_ids = [
        row[0]
        for row in (await session.execute(select(Episode.id).where(Episode.owner_id == owner_id))).all()
    ]
    if episode_ids:
        await session.execute(delete(FeedbackEvent).where(FeedbackEvent.episode_id.in_(episode_ids)))
        await session.execute(delete(PlaybackEvent).where(PlaybackEvent.episode_id.in_(episode_ids)))
        await session.execute(delete(PlanItem).where(PlanItem.episode_id.in_(episode_ids)))
        await session.execute(
            delete(EpisodeRevision).where(EpisodeRevision.episode_id.in_(episode_ids))
        )
        await session.execute(delete(Episode).where(Episode.id.in_(episode_ids)))
    await session.execute(delete(DailyPlan).where(DailyPlan.owner_id == owner_id))
    await session.execute(delete(StageArtifact).where(StageArtifact.owner_id == owner_id))
    source_ids = [
        row[0]
        for row in (await session.execute(select(Source.id).where(Source.owner_id == owner_id))).all()
    ]
    if source_ids:
        version_ids = [
            row[0]
            for row in (
                await session.execute(
                    select(SourceVersion.id).where(SourceVersion.source_id.in_(source_ids))
                )
            ).all()
        ]
        if version_ids:
            await session.execute(delete(Chunk).where(Chunk.source_version_id.in_(version_ids)))
        await session.execute(delete(SourceVersion).where(SourceVersion.source_id.in_(source_ids)))
        await session.execute(
            update(Source).where(Source.id.in_(source_ids)).values(parent_source_id=None)
        )
        await session.execute(delete(Source).where(Source.id.in_(source_ids)))
    await session.execute(delete(FeedToken).where(FeedToken.owner_id == owner_id))
    await session.execute(delete(Session).where(Session.owner_id == owner_id))
    await session.execute(delete(IdempotencyKey).where(IdempotencyKey.owner_id == owner_id))
    await session.commit()
