"""Run lifecycle: creation, attempt claiming, leases and terminal transitions.

The database, not Celery, is the queue of record (doc §8.2): a message may be delivered
twice, so every execution first claims an ``attempt`` under a row lock and only the
holder of ``active_attempt_id`` may advance the run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    GenerationRun,
    Phase,
    RunAttempt,
    RunKind,
    RunState,
    StopReason,
)
from app.services.events import append_event


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ClaimedAttempt:
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_no: int
    lease_expires_at: datetime


@dataclass(slots=True)
class RunSpec:
    owner_id: uuid.UUID
    kind: RunKind
    request_key: str
    payload_hash: str = ""
    source_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None
    state: RunState = RunState.QUEUED
    phase: Phase | None = None
    deadline_at: datetime | None = None


async def create_run(
    session: AsyncSession, spec: RunSpec, settings: Settings
) -> tuple[GenerationRun, bool]:
    """Insert a run once per ``(owner, kind, request_key)``; replay returns the original."""
    existing = await get_run_by_request(session, spec.owner_id, spec.kind, spec.request_key)
    if existing is not None:
        return existing, False
    run = GenerationRun(
        owner_id=spec.owner_id,
        kind=spec.kind,
        request_key=spec.request_key,
        payload_hash=spec.payload_hash,
        source_id=spec.source_id,
        episode_id=spec.episode_id,
        revision_id=spec.revision_id,
        state=spec.state,
        phase=spec.phase,
        deadline_at=spec.deadline_at
        or utcnow() + timedelta(seconds=settings.task_time_limit),
        budget_usage={},
        event_seq=0,
    )
    session.add(run)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        session.expunge(run)
        existing = await get_run_by_request(session, spec.owner_id, spec.kind, spec.request_key)
        if existing is None:
            raise
        return existing, False
    await append_event(session, run.id, "run.created", {"kind": run.kind.value})
    return run, True


async def get_run_by_request(
    session: AsyncSession, owner_id: uuid.UUID, kind: RunKind, request_key: str
) -> GenerationRun | None:
    result = await session.execute(
        select(GenerationRun).where(
            GenerationRun.owner_id == owner_id,
            GenerationRun.kind == kind,
            GenerationRun.request_key == request_key,
        )
    )
    return result.scalar_one_or_none()


async def claim_attempt(
    session: AsyncSession, *, run_id: uuid.UUID, worker_id: str, settings: Settings
) -> ClaimedAttempt | None:
    """Claim the next attempt for ``run_id``.

    Returns ``None`` when another live attempt holds the lease, the run is terminal, or
    the attempt budget is exhausted (in which case the run is failed here).
    """
    result = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None
    now = utcnow()
    if run.state in {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.WAITING_REVIEW,
        RunState.DEFERRED,
    }:
        return None
    lease = _aware(run.lease_expires_at)
    if run.state == RunState.RUNNING and lease and lease > now:
        return None
    if run.state == RunState.RETRY_WAIT:
        due = _aware(run.next_attempt_at)
        if due and due > now:
            return None
    if run.attempt_no >= settings.max_attempts:
        run.state = RunState.FAILED
        run.stop_reason = "attempts_exhausted"
        run.lease_expires_at = None
        run.active_attempt_id = None
        await append_event(
            session, run.id, "run.failed", {"reason": "attempts_exhausted", "attempts": run.attempt_no}
        )
        return None

    await _close_previous_attempt(session, run)
    attempt_no = run.attempt_no + 1
    lease_expires = now + timedelta(seconds=settings.lease_seconds)
    attempt = RunAttempt(
        run_id=run.id,
        attempt_no=attempt_no,
        worker_id=worker_id,
        state="ACTIVE",
        lease_expires_at=lease_expires,
    )
    session.add(attempt)
    await session.flush()
    run.attempt_no = attempt_no
    run.active_attempt_id = attempt.id
    run.state = RunState.RUNNING
    run.lease_expires_at = lease_expires
    run.next_attempt_at = None
    run.dispatched_at = None
    await append_event(session, run.id, "run.attempt_started", {"attempt_no": attempt_no})
    return ClaimedAttempt(
        run_id=run.id,
        attempt_id=attempt.id,
        attempt_no=attempt_no,
        lease_expires_at=lease_expires,
    )


async def _close_previous_attempt(session: AsyncSession, run: GenerationRun) -> None:
    if run.active_attempt_id is None:
        return
    previous = await session.get(RunAttempt, run.active_attempt_id)
    if previous is not None and previous.state == "ACTIVE":
        previous.state = "EXPIRED"
        previous.finished_at = utcnow()


async def heartbeat(
    session: AsyncSession, *, run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings
) -> bool:
    result = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None or run.active_attempt_id != attempt_id:
        return False
    now = utcnow()
    lease = _aware(run.lease_expires_at)
    if lease is not None and lease <= now:
        return False
    renew = now + timedelta(seconds=settings.lease_seconds)
    run.lease_expires_at = renew
    attempt = await session.get(RunAttempt, attempt_id)
    if attempt is not None:
        attempt.lease_expires_at = renew
    return True


async def has_authority(
    session: AsyncSession, *, run_id: uuid.UUID, attempt_id: uuid.UUID
) -> bool:
    run = await session.get(GenerationRun, run_id)
    if run is None or run.active_attempt_id != attempt_id:
        return False
    lease = _aware(run.lease_expires_at)
    return lease is not None and lease > utcnow()


async def require_authority(
    session: AsyncSession, *, run_id: uuid.UUID, attempt_id: uuid.UUID
) -> GenerationRun:
    """Row-locked, authoritative check used by every phase commit."""
    result = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    run = result.scalar_one()
    lease = _aware(run.lease_expires_at)
    if run.active_attempt_id != attempt_id or lease is None or lease <= utcnow():
        raise LostAuthority(run_id)
    return run


class LostAuthority(RuntimeError):
    """Another attempt took over; the stale worker must stop."""


async def set_phase(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    phase: Phase,
    detail: dict | None = None,
) -> None:
    run = await require_authority(session, run_id=run_id, attempt_id=attempt_id)
    run.phase = phase
    await append_event(session, run_id, "run.phase", {"phase": phase.value, **(detail or {})})


async def finish_succeeded(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    stop_reason: str = StopReason.COMPLETED.value,
) -> None:
    run = await require_authority(session, run_id=run_id, attempt_id=attempt_id)
    run.state = RunState.SUCCEEDED
    run.stop_reason = stop_reason
    run.active_attempt_id = None
    run.lease_expires_at = None
    await _finish_attempt(session, attempt_id, "SUCCEEDED")
    await append_event(session, run_id, "run.succeeded", {"stop_reason": stop_reason})


async def finish_waiting_review(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    reason: str,
    unresolved: list[dict] | None = None,
) -> None:
    run = await require_authority(session, run_id=run_id, attempt_id=attempt_id)
    run.state = RunState.WAITING_REVIEW
    run.stop_reason = reason
    run.active_attempt_id = None
    run.lease_expires_at = None
    await _finish_attempt(session, attempt_id, "WAITING_REVIEW")
    await append_event(
        session, run_id, "run.waiting_review", {"reason": reason, "unresolved": unresolved or []}
    )


async def finish_failed(
    session: AsyncSession, *, run_id: uuid.UUID, attempt_id: uuid.UUID, reason: str
) -> None:
    run = await require_authority(session, run_id=run_id, attempt_id=attempt_id)
    run.state = RunState.FAILED
    run.stop_reason = reason
    run.last_error = reason
    run.active_attempt_id = None
    run.lease_expires_at = None
    await _finish_attempt(session, attempt_id, "FAILED")
    await append_event(session, run_id, "run.failed", {"reason": reason})


async def schedule_retry(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    reason: str,
    settings: Settings,
    retryable: bool = True,
) -> bool:
    run = await require_authority(session, run_id=run_id, attempt_id=attempt_id)
    await _finish_attempt(session, attempt_id, "FAILED")
    run.active_attempt_id = None
    run.lease_expires_at = None
    run.last_error = reason
    if retryable and run.attempt_no < settings.max_attempts:
        backoff = settings.retry_backoff_seconds
        delay = backoff[min(run.attempt_no - 1, len(backoff) - 1)]
        run.state = RunState.RETRY_WAIT
        run.next_attempt_at = utcnow() + timedelta(seconds=delay)
        await append_event(
            session, run_id, "run.retry_scheduled", {"reason": reason, "delay_seconds": delay}
        )
        return True
    run.state = RunState.FAILED
    run.stop_reason = reason
    await append_event(session, run_id, "run.failed", {"reason": reason})
    return False


async def _finish_attempt(session: AsyncSession, attempt_id: uuid.UUID, state: str) -> None:
    attempt = await session.get(RunAttempt, attempt_id)
    if attempt is not None:
        attempt.state = state
        attempt.finished_at = utcnow()


async def requeue_deferred(session: AsyncSession, run_id: uuid.UUID) -> bool:
    """DEFERRED -> QUEUED once the daily plan has room (doc §8.2)."""
    result = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None or run.state != RunState.DEFERRED:
        return False
    run.state = RunState.QUEUED
    run.next_attempt_at = utcnow()
    await append_event(session, run_id, "run.queued", {"from": RunState.DEFERRED.value})
    return True


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
