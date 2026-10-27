"""Request-scoped dependencies: session auth, CSRF and idempotency (doc §11)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import IdempotencyKey, Session, UserProfile
from app.security import constant_time_equals, hash_token

MUTATING = {"POST", "PATCH", "PUT", "DELETE"}


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UserProfile:
    # A browser cookie is the only accepted identity source; the header form exists for
    # the CLI/tests and still resolves to a server-side session row.
    token = request.cookies.get(settings.session_cookie) or request.headers.get("x-session-token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_required")
    row = await session.execute(
        select(Session, UserProfile)
        .join(UserProfile, UserProfile.id == Session.owner_id)
        .where(Session.token_hash == hash_token(token))
    )
    pair = row.first()
    if pair is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_invalid")
    auth_session, profile = pair
    expires = auth_session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if auth_session.revoked_at is not None or expires <= datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_expired")
    if request.method in MUTATING:
        header = request.headers.get("x-csrf-token", "")
        if not header or not constant_time_equals(header, auth_session.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_failed")
    request.state.auth_session = auth_session
    return profile


def payload_hash(payload: dict | bytes | None) -> str:
    if payload is None:
        raw = b""
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def idempotent_replay(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    endpoint: str,
    key: str | None,
    payload_digest: str,
) -> dict | None:
    if not key:
        return None
    row = await session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.owner_id == owner_id,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.key == key,
        )
    )
    existing = row.scalar_one_or_none()
    if existing is None:
        return None
    if existing.payload_hash != payload_digest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="idempotency_key_payload_mismatch"
        )
    return existing.response_json


async def remember(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    endpoint: str,
    key: str | None,
    payload_digest: str,
    response: dict,
    status_code: int = 202,
) -> None:
    if not key:
        return
    session.add(
        IdempotencyKey(
            owner_id=owner_id,
            endpoint=endpoint,
            key=key,
            payload_hash=payload_digest,
            status_code=status_code,
            response_json=response,
        )
    )
    await session.flush()


def idempotency_key(header: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    return header
