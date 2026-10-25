"""Durable run events.

``event_seq`` is allocated while holding the run's row lock, in the same transaction as
the business update it describes (doc §11.1). A global sequence would let a later-committing
transaction appear *before* an earlier one and make clients skip events.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationRun, RunEvent


async def append_event(
    session: AsyncSession, run_id: uuid.UUID, event_type: str, payload: dict | None = None
) -> int:
    run = await session.execute(
        select(GenerationRun).where(GenerationRun.id == run_id).with_for_update()
    )
    run_row = run.scalar_one()
    run_row.event_seq += 1
    seq = run_row.event_seq
    session.add(RunEvent(run_id=run_id, seq=seq, type=event_type, payload=payload or {}))
    await session.flush()
    return seq


async def read_events(
    session: AsyncSession, run_id: uuid.UUID, after_seq: int = 0, limit: int = 200
) -> list[RunEvent]:
    rows = await session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit)
    )
    return list(rows.scalars())
