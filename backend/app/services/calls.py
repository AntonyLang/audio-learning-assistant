"""Physical provider-call ledger (doc §7.3).

The row is inserted as ``INTENT`` *before* the request leaves the process, then updated
with the receipt or the failure. ``call_seq`` is monotonic per run so an ``UNKNOWN``
record from an earlier attempt is preserved rather than overwritten.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProviderCall, ProviderCallState


async def next_call_seq(session: AsyncSession, run_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.coalesce(func.max(ProviderCall.call_seq), 0)).where(
            ProviderCall.run_id == run_id
        )
    )
    return int(result.scalar_one()) + 1


async def begin_call(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    stage: str,
    provider: str,
    input_fingerprint: str = "",
) -> ProviderCall:
    call = ProviderCall(
        run_id=run_id,
        attempt_id=attempt_id,
        stage=stage,
        provider=provider,
        call_seq=await next_call_seq(session, run_id),
        input_fingerprint=input_fingerprint,
        state=ProviderCallState.INTENT,
    )
    session.add(call)
    await session.flush()
    return call


async def succeed_call(
    session: AsyncSession,
    call: ProviderCall,
    *,
    request_id: str | None = None,
    audio_url: str | None = None,
    expires_at: datetime | None = None,
    usage: dict | None = None,
) -> None:
    call.state = ProviderCallState.SUCCEEDED
    call.request_id = request_id
    call.audio_url = audio_url
    call.expires_at = expires_at
    call.usage = usage or {}
    await session.flush()


async def fail_call(
    session: AsyncSession, call: ProviderCall, *, code: str, unknown: bool = False
) -> None:
    call.state = ProviderCallState.UNKNOWN if unknown else ProviderCallState.FAILED
    call.error_code = code
    await session.flush()
