"""Playback telemetry from the web player (doc §12).

Only observable web playback is recorded: merged time intervals with a monotonic client
sequence. Seeking to the end is therefore not the same as having listened, and RSS
downloads recorded elsewhere are never turned into completion claims.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_session
from app.models import Episode, EpisodeRevision, PlaybackEvent, UserProfile

router = APIRouter(tags=["playback"])


class PlaybackIn(BaseModel):
    episode_id: uuid.UUID
    session_id: str = Field(min_length=4, max_length=80)
    client_seq: int = Field(ge=0)
    kind: Literal["progress", "position"] = "progress"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


@router.post("/v1/playback-events", status_code=status.HTTP_202_ACCEPTED)
async def record_playback(
    body: PlaybackIn,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    episode = await session.get(Episode, body.episode_id)
    if episode is None or episode.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="episode_not_found")
    if body.end_ms < body.start_ms:
        raise HTTPException(status_code=422, detail="invalid_interval")
    revision = await session.get(EpisodeRevision, episode.published_revision_id) if episode.published_revision_id else None
    if revision is None or body.end_ms > (revision.duration_ms or 0) + 1000:
        raise HTTPException(status_code=422, detail="playback_out_of_range")
    try:
        async with session.begin_nested():
            session.add(PlaybackEvent(
            owner_id=owner.id,
            episode_id=body.episode_id,
            session_id=body.session_id,
            client_seq=body.client_seq,
            kind=body.kind,
            start_ms=body.start_ms,
            end_ms=body.end_ms,
            ))
            await session.flush()
    except IntegrityError:
        return {"status": "duplicate"}
    await session.commit()
    return {"status": "recorded"}


@router.get("/v1/episodes/{episode_id}/playback")
async def playback_summary(
    episode_id: uuid.UUID,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="episode_not_found")
    rows = await session.execute(
        select(PlaybackEvent.start_ms, PlaybackEvent.end_ms).where(
            PlaybackEvent.episode_id == episode_id
        ).where(
            PlaybackEvent.owner_id == owner.id,
            PlaybackEvent.kind == "progress",
        )
    )
    intervals = sorted((int(start), int(end)) for start, end in rows.all())
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    listened = sum(end - start for start, end in merged)
    latest = await session.scalar(
        select(PlaybackEvent.end_ms).where(
            PlaybackEvent.episode_id == episode_id,
            PlaybackEvent.owner_id == owner.id,
        ).order_by(PlaybackEvent.created_at.desc(), PlaybackEvent.client_seq.desc()).limit(1)
    )
    return {"merged_intervals": merged, "listened_ms": listened, "position_ms": latest or 0}
