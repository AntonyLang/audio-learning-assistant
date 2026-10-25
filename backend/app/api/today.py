"""Today's listening plan (doc §9.1)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Episode, UserProfile
from app.services import planning

router = APIRouter(tags=["plan"])


@router.get("/v1/today")
async def today(
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    prefs = dict(owner.prefs or {})
    plan = await planning.get_or_create_plan(
        session,
        owner_id=owner.id,
        local_date=planning.local_date_for(owner),
        timezone_name=owner.timezone,
        budget_ms=int(prefs.get("daily_budget_ms", settings.daily_budget_ms)),
        prefs=prefs,
    )
    snapshot = await planning.today_snapshot(session, owner_id=owner.id, plan=plan)
    for item in snapshot["items"]:
        episode = await session.get(Episode, uuid.UUID(item["episode_id"]))
        if episode is not None:
            item["guid"] = episode.guid
            item["published_at"] = (
                episode.published_at.isoformat() if episode.published_at else None
            )
    snapshot["timezone"] = owner.timezone
    promoted = await planning.promote_deferred_runs(session, plan=plan, settings=settings)
    snapshot["promoted_runs"] = [str(run_id) for run_id in promoted]
    await session.commit()
    return snapshot


