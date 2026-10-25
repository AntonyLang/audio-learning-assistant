"""How a committed run row reaches an executor.

Production: the dispatcher process sends ``run_id`` to Celery. Local demo/tests: the same
envelope can be executed inline so the API works without Redis or a worker. Either way the
run row is already committed, so nothing is lost if delivery fails.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)


class Dispatcher(Protocol):
    def enqueue(self, run_id: uuid.UUID) -> None: ...


class CeleryDispatcher:
    def enqueue(self, run_id: uuid.UUID) -> None:
        from app.jobs.tasks import run_generation_task

        run_generation_task.delay(str(run_id))


class InlineDispatcher:
    """Executes in the API process; only for local demo and tests."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def enqueue(self, run_id: uuid.UUID) -> None:
        from app.pipeline.runner import execute_run

        task = asyncio.create_task(execute_run(run_id, worker_id="inline"))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def discard_done(self) -> None:
        self._tasks = {t for t in self._tasks if not t.done()}


_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        settings = get_settings()
        _dispatcher = InlineDispatcher() if settings.inline_worker else CeleryDispatcher()
    return _dispatcher


def set_dispatcher(dispatcher: Dispatcher) -> None:
    """Used by tests to capture or force dispatch."""
    global _dispatcher
    _dispatcher = dispatcher
