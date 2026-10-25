"""Dispatcher: PostgreSQL rows are the queue of record (doc §8.2).

Every tick it finds due ``QUEUED``/``RETRY_WAIT`` rows, takes a short dispatch lease, and
*after* committing sends the id to Celery. It never holds a database lock while talking to
the broker, and a message lost by Redis is retried on the next tick because the row is
still there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.jobs.dispatch import CeleryDispatcher
from app.models import GenerationRun, RunState

logger = logging.getLogger("dispatcher")
DISPATCH_LEASE_SECONDS = 60


async def tick(*, limit: int = 20, dispatcher=None) -> list[uuid.UUID]:
    settings = get_settings()
    now = datetime.now(UTC)
    dispatcher = dispatcher or CeleryDispatcher()
    sent: list[uuid.UUID] = []
    async with session_scope() as session:
        rows = await session.execute(
            select(GenerationRun)
            .where(
                GenerationRun.state.in_([RunState.QUEUED, RunState.RETRY_WAIT]),
                (GenerationRun.next_attempt_at.is_(None))
                | (GenerationRun.next_attempt_at <= now),
                (GenerationRun.dispatched_at.is_(None))
                | (
                    GenerationRun.dispatched_at
                    <= now - timedelta(seconds=DISPATCH_LEASE_SECONDS)
                ),
            )
            .order_by(GenerationRun.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for run in rows.scalars():
            run.dispatched_at = now
            sent.append(run.id)
    for run_id in sent:
        try:
            dispatcher.enqueue(run_id)
        except Exception:  # noqa: BLE001 - broker failure is retried next tick
            logger.exception("failed to enqueue run %s", run_id)
    if sent:
        logger.info("dispatched %s runs (interval=%ss)", len(sent), settings.dispatcher_interval_seconds)
    return sent


async def loop(interval: float | None = None) -> None:  # pragma: no cover - runtime loop
    settings = get_settings()
    delay = interval or settings.dispatcher_interval_seconds
    logger.info("dispatcher started, interval=%ss", delay)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            logger.exception("dispatcher tick failed")
        await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="dispatch due generation runs to Celery")
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.once:
        asyncio.run(tick())
    else:
        asyncio.run(loop())


if __name__ == "__main__":  # pragma: no cover
    main()
