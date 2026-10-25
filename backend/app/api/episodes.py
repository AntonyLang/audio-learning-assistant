"""Episodes: create from a ready source, inspect, and feed back (doc §11, §12)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, idempotency_key, idempotent_replay, payload_hash, remember
from app.config import Settings, get_settings
from app.db import get_session
from app.jobs.dispatch import get_dispatcher
from app.models import (
    Chunk,
    Episode,
    EpisodeRevision,
    FeedbackEvent,
    FeedbackKind,
    GenerationRun,
    PlanItem,
    Quality,
    RevisionStatus,
    Source,
    SourceVersion,
    StageArtifact,
    UserProfile,
)
from app.services import episode_service

router = APIRouter(tags=["episodes"])


class EpisodeCreate(BaseModel):
    source_id: uuid.UUID
    question: str = Field(default="", max_length=500)
    acknowledge_partial: bool = False


class EpisodeCreated(BaseModel):
    episode_id: str
    run_id: str
    state: str
    deferred: bool = False
    status_url: str
    events_url: str


@router.post("/v1/episodes", response_model=EpisodeCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_episode(
    body: EpisodeCreate,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    key: str | None = Depends(idempotency_key),
) -> EpisodeCreated:
    endpoint = "POST /v1/episodes"
    digest = payload_hash(body.model_dump(mode="json"))
    replay = await idempotent_replay(
        session, owner_id=owner.id, endpoint=endpoint, key=key, payload_digest=digest
    )
    if replay is not None:
        return EpisodeCreated(**replay)

    source = await session.get(Source, body.source_id)
    if source is None or source.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="source_not_found")
    version = await episode_service.latest_ready_version(session, source_id=source.id)
    if version is None:
        raise HTTPException(status_code=409, detail="source_not_ready")
    if version.quality != Quality.FULL and not body.acknowledge_partial:
        raise HTTPException(
            status_code=409,
            detail=f"source_partial:{version.quality.value}",
        )

    bundle = await episode_service.create_episode_with_run(
        session, owner=owner, source=source, question=body.question, settings=settings
    )
    response = EpisodeCreated(
        episode_id=str(bundle.episode.id),
        run_id=str(bundle.run.id),
        state=bundle.run.state.value,
        deferred=bundle.deferred,
        status_url=f"/v1/runs/{bundle.run.id}",
        events_url=f"/v1/runs/{bundle.run.id}/events",
    )
    await remember(
        session,
        owner_id=owner.id,
        endpoint=endpoint,
        key=key,
        payload_digest=digest,
        response=response.model_dump(),
        status_code=202,
    )
    await session.commit()
    if not bundle.deferred:
        get_dispatcher().enqueue(bundle.run.id)
    return response


@router.get("/v1/episodes")
async def list_episodes(
    owner: UserProfile = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict:
    rows = await session.execute(
        select(Episode)
        .where(Episode.owner_id == owner.id)
        .order_by(Episode.created_at.desc())
        .limit(50)
    )
    items = []
    for episode in rows.scalars():
        revision = await _current_revision(session, episode)
        items.append(
            {
                "episode_id": str(episode.id),
                "question": episode.question,
                "published": episode.published_revision_id is not None,
                "published_at": episode.published_at.isoformat() if episode.published_at else None,
                "status": revision.status.value if revision else None,
                "duration_ms": revision.duration_ms if revision else None,
            }
        )
    return {"items": items}


@router.get("/v1/episodes/{episode_id}")
async def get_episode(
    episode_id: uuid.UUID,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="episode_not_found")
    revision = await _current_revision(session, episode)
    source = await session.get(Source, episode.primary_source_id)
    review = dict((revision.config_snapshot or {}).get("review") or {}) if revision else {}
    script = None
    cards: list[dict] = []
    audio = None
    if revision is not None and revision.script_id is not None:
        artifact = await session.get(StageArtifact, revision.script_id)
        if artifact is not None:
            payload = artifact.payload
            claims = [dict(claim) for claim in payload.get("claims", [])]
            verdicts = {
                row.get("claim_id"): row.get("verdict")
                for row in (review.get("verdicts") or [])
            }
            for claim in claims:
                claim["review"] = verdicts.get(claim.get("claim_id"), claim.get("review"))
            script = {
                "segments": payload.get("segments", []),
                "claims": claims,
                "char_count": payload.get("char_count", 0),
                "one_line_answer": payload.get("one_line_answer", ""),
            }
            cards = await _evidence_cards(session, payload.get("claims", []))
    if revision is not None and revision.audio_id is not None:
        audio_artifact = await session.get(StageArtifact, revision.audio_id)
        if audio_artifact is not None:
            audio = {
                "duration_ms": audio_artifact.duration_ms,
                "sha256": audio_artifact.sha256,
                "byte_size": audio_artifact.byte_size,
                "web_url": f"/v1/episodes/{episode.id}/audio",
                "feed_path": f"/v1/feeds/{{token}}/episodes/{episode.id}/audio/{revision.id}.mp3",
                "ready": episode.published_revision_id is not None,
            }
    feedback_rows = await session.execute(
        select(FeedbackEvent)
        .where(FeedbackEvent.episode_id == episode.id)
        .order_by(FeedbackEvent.created_at.desc())
        .limit(20)
    )
    return {
        "episode_id": str(episode.id),
        "guid": episode.guid,
        "question": episode.question,
        "published": episode.published_revision_id is not None,
        "published_at": episode.published_at.isoformat() if episode.published_at else None,
        "duration_ms": revision.duration_ms if revision else None,
        "revision": None
        if revision is None
        else {
            "revision_id": str(revision.id),
            "status": revision.status.value,
            "created_at": revision.created_at.isoformat() if revision.created_at else None,
            "config": revision.config_snapshot,
            "review": review,
        },
        "audio": audio,
        "script": script,
        "evidence": cards,
        "source": None
        if source is None
        else {
            "source_id": str(source.id),
            "title": source.title,
            "kind": source.kind.value,
            "canonical_url": source.canonical_url,
        },
        "feedback": [
            {"kind": row.kind.value, "note": row.note, "created_at": row.created_at.isoformat()}
            for row in feedback_rows.scalars()
        ],
    }


class FeedbackIn(BaseModel):
    kind: FeedbackKind
    note: str = Field(default="", max_length=2000)
    client_event_id: str = Field(min_length=4, max_length=80)
    parent_claim_id: str | None = None


@router.post("/v1/episodes/{episode_id}/feedback", status_code=status.HTTP_202_ACCEPTED)
async def post_feedback(
    episode_id: uuid.UUID,
    body: FeedbackIn,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="episode_not_found")
    existing = await session.execute(
        select(FeedbackEvent).where(
            FeedbackEvent.owner_id == owner.id,
            FeedbackEvent.client_event_id == body.client_event_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return {"status": "duplicate", "action": "none"}

    session.add(
        FeedbackEvent(
            owner_id=owner.id,
            episode_id=episode.id,
            kind=body.kind,
            note=body.note,
            parent_claim_id=body.parent_claim_id,
            client_event_id=body.client_event_id,
        )
    )
    action: dict = {"status": "recorded", "action": "preference_updated"}
    prefs = dict(owner.prefs or {})
    follow_up: dict | None = None

    if body.kind == FeedbackKind.SHALLOWER:
        prefs["depth"] = "shallower"
        owner.prefs = prefs
        owner.prefs_version += 1
    elif body.kind == FeedbackKind.LESS_LIKE_THIS:
        down = list(prefs.get("topics_down") or [])
        if episode.question and episode.question not in down:
            down.append(episode.question)
        prefs["topics_down"] = down
        owner.prefs = prefs
        owner.prefs_version += 1
    elif body.kind in {FeedbackKind.EXAMPLE, FeedbackKind.DEEPER}:
        source = await session.get(Source, episode.primary_source_id)
        if source is None:
            raise HTTPException(status_code=409, detail="source_missing")
        prefix = "举例说明：" if body.kind == FeedbackKind.EXAMPLE else "继续深入："
        bundle = await episode_service.create_episode_with_run(
            session,
            owner=owner,
            source=source,
            question=f"{prefix}{episode.question}",
            settings=settings,
            request_key_suffix=f":{body.kind.value}:{body.client_event_id}",
        )
        follow_up = {
            "episode_id": str(bundle.episode.id),
            "run_id": str(bundle.run.id),
            "deferred": bundle.deferred,
            "state": bundle.run.state.value,
        }
        action = {"status": "recorded", "action": "follow_up_created", "follow_up": follow_up}

    await session.commit()
    if follow_up and not follow_up["deferred"]:
        get_dispatcher().enqueue(uuid.UUID(follow_up["run_id"]))
    return action


async def _current_revision(session: AsyncSession, episode: Episode) -> EpisodeRevision | None:
    if episode.published_revision_id is not None:
        return await session.get(EpisodeRevision, episode.published_revision_id)
    rows = await session.execute(
        select(EpisodeRevision)
        .where(EpisodeRevision.episode_id == episode.id)
        .order_by(EpisodeRevision.revision_no.desc())
        .limit(1)
    )
    return rows.scalar_one_or_none()


async def _evidence_cards(session: AsyncSession, claims: list[dict]) -> list[dict]:
    """Attach human-readable source context to each cited span."""
    refs: dict[str, dict] = {}
    chunk_ids: list[uuid.UUID] = []
    for claim in claims:
        for ref in claim.get("evidence", []):
            try:
                chunk_ids.append(uuid.UUID(ref["chunk_id"]))
            except (KeyError, ValueError):
                continue
    if not chunk_ids:
        return []
    rows = await session.execute(
        select(Chunk, SourceVersion, Source)
        .join(SourceVersion, SourceVersion.id == Chunk.source_version_id)
        .join(Source, Source.id == SourceVersion.source_id)
        .where(Chunk.id.in_(chunk_ids))
    )
    for chunk, version, source in rows.all():
        refs[str(chunk.id)] = {
            "chunk_id": str(chunk.id),
            "source_title": source.title or source.canonical_url or "",
            "source_kind": source.kind.value,
            "page_no": chunk.page_no,
            "section_path": chunk.section_path,
            "paragraph_id": chunk.paragraph_id,
            "version_id": str(version.id),
        }
    return list(refs.values())


_ = (PlanItem, GenerationRun, RevisionStatus, datetime, timezone)
