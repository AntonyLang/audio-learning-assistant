"""Celery task entry points.

Each task owns its event loop: the async engine pool is disposed *inside* that loop, so a
connection pool created by a previous prefork task is never reused against a closed loop
(doc §8.4). Business state is always read from PostgreSQL; the broker only carries ids.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

from app.jobs.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="ala.run", bind=True)
def run_generation_task(self, run_id: str) -> str:  # pragma: no cover - needs a worker
    worker_id = f"celery:{os.getpid()}:{self.request.id or 'task'}"
    asyncio.run(_execute(uuid.UUID(run_id), worker_id))
    return run_id


async def _execute(run_id: uuid.UUID, worker_id: str) -> None:  # pragma: no cover
    from app.db import dispose_engine, engine
    from app.pipeline.runner import execute_run

    await engine.dispose()
    try:
        await execute_run(run_id, worker_id=worker_id)
    finally:
        await dispose_engine()
