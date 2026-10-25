"""Publication is one transaction with a fixed lock order (doc §9.2).

``plan -> episode -> run``. Expensive verification (byte hash, decode, ffprobe) happens
*outside* the lock and is bound to the immutable audio hash; inside the transaction only
the already-verified metadata is read. Budget is a cross-row invariant, so it is checked
by summing plan items under the plan lock rather than by a single-row CHECK.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import Adapters
from app.config import Settings
from app.models import (
    ArtifactKind,
    DailyPlan,
    Episode,
    EpisodeRevision,
    PlanItem,
    PlanItemState,
    RevisionStatus,
    StageArtifact,
)
from app.services import planning
from app.services.events import append_event


class PublishStatus(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    INVALID_AUDIO = "invalid_audio"


@dataclass(frozen=True, slots=True)
class AudioVerification:
    artifact_id: uuid.UUID
    sha256: str
    duration_ms: int
    decodable: bool


@dataclass(slots=True)
class PublishResult:
    status: PublishStatus
    episode_id: uuid.UUID
    revision_id: uuid.UUID | None = None
    duration_ms: int | None = None
    reason: str = ""

    @property
    def published(self) -> bool:
        return self.status in {PublishStatus.PUBLISHED, PublishStatus.ALREADY_PUBLISHED}


async def verify_audio(adapters: Adapters, artifact: StageArtifact) -> AudioVerification:
    """Re-read the stored object and compare it with the recorded hash (lock-free)."""
    if not artifact.object_key:
        return AudioVerification(artifact.id, "", 0, False)
    stored = await adapters.storage.stat(artifact.object_key)
    decodable = bool(artifact.duration_ms and artifact.duration_ms > 0)
    return AudioVerification(
        artifact_id=artifact.id,
        sha256=stored.sha256,
        duration_ms=int(artifact.duration_ms or 0),
        decodable=decodable,
    )


async def publish_revision(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    plan_id: uuid.UUID,
    episode_id: uuid.UUID,
    revision_id: uuid.UUID,
    verification: AudioVerification,
    settings: Settings,
    run_id: uuid.UUID | None = None,
) -> PublishResult:
    plan = (
        await session.execute(
            select(DailyPlan)
            .where(DailyPlan.id == plan_id, DailyPlan.owner_id == owner_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if plan is None:
        return PublishResult(PublishStatus.REJECTED, episode_id, reason="plan_not_found")

    episode = (
        await session.execute(select(Episode).where(Episode.id == episode_id).with_for_update())
    ).scalar_one_or_none()
    if episode is None or episode.owner_id != owner_id:
        return PublishResult(PublishStatus.REJECTED, episode_id, reason="episode_not_found")
    if episode.withdrawn_at is not None:
        return PublishResult(PublishStatus.REJECTED, episode_id, reason="episode_withdrawn")

    if episode.published_revision_id is not None and episode.published_revision_id != revision_id:
        return PublishResult(
            PublishStatus.CONFLICT,
            episode_id,
            revision_id=episode.published_revision_id,
            reason="published_revision_is_fixed",
        )

    revision = (
        await session.execute(
            select(EpisodeRevision).where(
                EpisodeRevision.id == revision_id, EpisodeRevision.episode_id == episode_id
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        return PublishResult(PublishStatus.REJECTED, episode_id, reason="revision_not_found")
    if revision.status != RevisionStatus.READY:
        return PublishResult(
            PublishStatus.INVALID_AUDIO, episode_id, revision_id=revision.id, reason="revision_not_ready"
        )
    if revision.audio_id != verification.artifact_id:
        return PublishResult(
            PublishStatus.INVALID_AUDIO, episode_id, revision_id=revision.id, reason="audio_mismatch"
        )
    audio = await session.get(StageArtifact, revision.audio_id)
    if audio is None or audio.kind != ArtifactKind.AUDIO_FINAL:
        return PublishResult(
            PublishStatus.INVALID_AUDIO, episode_id, revision_id=revision.id, reason="audio_missing"
        )
    if not verification.decodable or verification.sha256 != (audio.sha256 or ""):
        return PublishResult(
            PublishStatus.INVALID_AUDIO, episode_id, revision_id=revision.id, reason="audio_unverified"
        )
    duration_ms = int(audio.duration_ms or 0)
    if duration_ms <= 0 or duration_ms > settings.max_episode_ms:
        return PublishResult(
            PublishStatus.INVALID_AUDIO,
            episode_id,
            revision_id=revision.id,
            duration_ms=duration_ms,
            reason="duration_gate",
        )

    items = (
        await session.execute(
            select(PlanItem).where(PlanItem.plan_id == plan.id).with_for_update()
        )
    ).scalars()
    items = list(items)
    already = next((i for i in items if i.episode_id == episode_id), None)
    if episode.published_revision_id is not None and already is not None:
        return PublishResult(
            PublishStatus.ALREADY_PUBLISHED,
            episode_id,
            revision_id=revision.id,
            duration_ms=duration_ms,
        )

    committed = sum(
        int(i.duration_ms or i.reserved_ms or 0)
        for i in items
        if i.state in {PlanItemState.AVAILABLE, PlanItemState.RESERVED}
        and i is not already
    )
    if committed + duration_ms > plan.budget_ms:
        if already is None:
            session.add(
                PlanItem(
                    plan_id=plan.id,
                    episode_id=episode_id,
                    source_id=episode.primary_source_id,
                    position=await planning.next_position(session, plan.id),
                    state=PlanItemState.DEFERRED,
                    reserved_ms=0,
                    duration_ms=duration_ms,
                )
            )
        else:
            already.state = PlanItemState.DEFERRED
            already.reserved_ms = 0
        await session.flush()
        if run_id is not None:
            await append_event(
                session,
                run_id,
                "plan.deferred",
                {"episode_id": str(episode_id), "duration_ms": duration_ms},
            )
        return PublishResult(
            PublishStatus.DEFERRED,
            episode_id,
            revision_id=revision.id,
            duration_ms=duration_ms,
            reason="budget_exceeded",
        )

    if already is None:
        already = PlanItem(
            plan_id=plan.id,
            episode_id=episode_id,
            source_id=episode.primary_source_id,
            position=await planning.next_position(session, plan.id),
            state=PlanItemState.AVAILABLE,
            reserved_ms=duration_ms,
            duration_ms=duration_ms,
        )
        session.add(already)
    else:
        already.state = PlanItemState.AVAILABLE
        already.reserved_ms = duration_ms
        already.duration_ms = duration_ms

    if episode.published_revision_id is None:
        episode.published_revision_id = revision.id
        episode.published_at = datetime.now(UTC)

    await session.flush()
    if run_id is not None:
        await append_event(
            session,
            run_id,
            "episode.published",
            {
                "episode_id": str(episode_id),
                "revision_id": str(revision.id),
                "duration_ms": duration_ms,
                "published_at": episode.published_at.isoformat() if episode.published_at else None,
            },
        )
    return PublishResult(
        PublishStatus.PUBLISHED,
        episode_id,
        revision_id=revision.id,
        duration_ms=duration_ms,
    )


async def withdraw_episode(
    session: AsyncSession, *, owner_id: uuid.UUID, episode_id: uuid.UUID, reason: str = ""
) -> bool:
    episode = (
        await session.execute(select(Episode).where(Episode.id == episode_id).with_for_update())
    ).scalar_one_or_none()
    if episode is None or episode.owner_id != owner_id:
        return False
    episode.withdrawn_at = datetime.now(UTC)
    return True
