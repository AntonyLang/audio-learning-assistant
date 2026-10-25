"""Scheduler: daily plans and deferred promotion (doc §13.2).

Every tick it makes sure today's plan exists for each user whose local date has advanced,
then promotes deferred runs while the plan still has room. Missing several days does not
create a backlog: only the current plan is staged.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.jobs.dispatcher import tick as dispatch_tick
from app.models import UserProfile
from app.services import planning

logger = logging.getLogger("scheduler")


async def tick() -> dict:
    settings = get_settings()
    promoted: list[str] = []
    async with session_scope() as session:
        users = list((await session.execute(select(UserProfile))).scalars())
        for owner in users:
            prefs = dict(owner.prefs or {})
            local_date = planning.local_date_for(owner)
            plan = await planning.get_or_create_plan(
                session,
                owner_id=owner.id,
                local_date=local_date,
                timezone_name=owner.timezone,
                budget_ms=int(prefs.get("daily_budget_ms", settings.daily_budget_ms)),
                prefs=prefs,
            )
            promoted.extend(
                str(run_id)
                for run_id in await planning.promote_deferred_runs(
                    session, plan=plan, settings=settings
                )
            )
    dispatched = await dispatch_tick()
    logger.info(
        "scheduler tick users=%s promoted=%s dispatched=%s at %s",
        len(users),
        len(promoted),
        len(dispatched),
        datetime.now(UTC).isoformat(),
    )
    return {"users": len(users), "promoted": promoted, "dispatched": len(dispatched)}


async def loop(interval: float = 60.0) -> None:  # pragma: no cover - runtime loop
    logger.info("scheduler started, interval=%ss", interval)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler tick failed")
        await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="stage daily plans and promote deferred runs")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(tick() if args.once else loop())


if __name__ == "__main__":  # pragma: no cover
    main()
