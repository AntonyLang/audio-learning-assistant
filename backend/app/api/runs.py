"""Run inspection, SSE replay and explicit retry (doc §8, §11)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.config import Settings, get_settings
from app.db import get_session, session_scope
from app.jobs.dispatch import get_dispatcher
from app.models import (
    EpisodeRevision,
    GenerationRun,
    RunEvent,
    RunState,
    StageArtifact,
    UserProfile,
)
from app.services import runs
from app.services.events import append_event, read_events

router = APIRouter(tags=["runs"])

TERMINAL = {
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.WAITING_REVIEW,
}


@router.get("/v1/runs/{run_id}")
async def run_snapshot(
    run_id: uuid.UUID,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    run = await _owned_run(session, run_id, owner.id)
    revision = await session.get(EpisodeRevision, run.revision_id) if run.revision_id else None
    audio = (
        await session.get(StageArtifact, revision.audio_id)
        if revision is not None and revision.audio_id
        else None
    )
    last = (
        await session.execute(
            select(RunEvent.seq)
            .where(RunEvent.run_id == run.id)
            .order_by(RunEvent.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "run_id": str(run.id),
        "kind": run.kind.value,
        "state": run.state.value,
        "phase": run.phase.value if run.phase else None,
        "attempt_no": run.attempt_no,
        "stop_reason": run.stop_reason,
        "last_error": run.last_error,
        "budget_usage": run.budget_usage,
        "last_seq": int(last or 0),
        "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
        "revision": None
        if revision is None
        else {
            "revision_id": str(revision.id),
            "status": revision.status.value,
            "duration_ms": revision.duration_ms,
            "audio_ready": revision.audio_id is not None,
            "sha256": audio.sha256 if audio else None,
        },
    }


@router.get("/v1/runs/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    owner: UserProfile = Depends(current_user),
    settings: Settings = Depends(get_settings),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    async with session_scope() as session:
        await _owned_run(session, run_id, owner.id)
    try:
        initial_cursor = int(last_event_id or 0)
    except ValueError:
        initial_cursor = 0

    async def event_stream():
        cursor = initial_cursor
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                return
            async with session_scope() as session:
                events = await read_events(session, run_id, cursor)
                run = await session.get(GenerationRun, run_id)
                terminal = run.state in TERMINAL
            for event in events:
                cursor = event.seq
                payload = json.dumps(event.payload, ensure_ascii=False)
                yield f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"
            if events:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks % 15 == 0:
                    yield ": heartbeat\n\n"
                if terminal:
                    yield "event: stream.end\ndata: {}\n\n"
                    return
            await asyncio.sleep(settings.sse_poll_seconds)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class RetryIn(BaseModel):
    retry_scope: str = "missing"
    expected_attempt: int | None = None


@router.post("/v1/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_run(
    run_id: uuid.UUID,
    body: RetryIn,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manual retry reuses committed artifacts; it never clears the call budget."""
    run = await _owned_run(session, run_id, owner.id)
    if body.expected_attempt is not None and body.expected_attempt != run.attempt_no:
        raise HTTPException(status_code=409, detail="attempt_mismatch")
    if run.state not in {RunState.FAILED, RunState.WAITING_REVIEW, RunState.DEFERRED, RunState.RETRY_WAIT}:
        raise HTTPException(status_code=409, detail=f"retry_not_allowed:{run.state.value}")
    if run.attempt_no >= settings.max_attempts:
        # An explicit human retry is allowed to exceed the automatic cap once, and the
        # fact that it happened is visible in the attempt history.
        run.attempt_no = settings.max_attempts - 1
    run.state = RunState.QUEUED
    run.next_attempt_at = datetime.now(UTC)
    run.last_error = None
    await append_event(
        session,
        run.id,
        "run.queued",
        {"reason": "manual_retry", "retry_scope": body.retry_scope},
    )
    await session.commit()
    get_dispatcher().enqueue(run.id)
    return {"run_id": str(run.id), "state": run.state.value}


async def _owned_run(
    session: AsyncSession, run_id: uuid.UUID, owner_id: uuid.UUID
) -> GenerationRun:
    run = await session.get(GenerationRun, run_id)
    if run is None or run.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="run_not_found")
    return run


_ = runs
