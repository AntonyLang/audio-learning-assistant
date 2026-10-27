"""Session, profile and account bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Session, UserProfile
from app.security import constant_time_equals, hash_password, hash_token, new_token, verify_password

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=16, max_length=256)


class LoginResponse(BaseModel):
    owner_id: str
    email: str
    csrf_token: str
    expires_at: datetime


class ProfilePatch(BaseModel):
    timezone: str | None = None
    daily_budget_ms: int | None = Field(default=None, ge=60_000, le=3_600_000)
    depth: str | None = None
    goals: list[str] | None = None
    topics_down: list[str] | None = None


@router.post("/v1/session", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    if settings.app_env == "production" and request.headers.get("origin") not in (
        None, settings.public_base_url.rstrip("/")
    ):
        raise HTTPException(status_code=403, detail="origin_rejected")
    row = await session.execute(select(UserProfile).where(UserProfile.email == body.email.lower()))
    profile = row.scalar_one_or_none()
    if profile is None or not verify_password(body.password, profile.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials")
    token = new_token()
    csrf = new_token(16)
    expires = datetime.now(UTC) + timedelta(seconds=settings.session_ttl_seconds)
    session.add(
        Session(
            owner_id=profile.id,
            token_hash=hash_token(token),
            csrf_token=csrf,
            expires_at=expires,
        )
    )
    await session.commit()
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_seconds,
        path="/",
    )
    return LoginResponse(
        owner_id=str(profile.id), email=profile.email, csrf_token=csrf, expires_at=expires
    )


@router.delete("/v1/session", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    token = request.cookies.get(settings.session_cookie) or request.headers.get("x-session-token")
    if token:
        row = await session.execute(select(Session).where(Session.token_hash == hash_token(token)))
        existing = row.scalar_one_or_none()
        if existing is not None and existing.revoked_at is None:
            # Logout is state-changing; a live session must present its CSRF token.
            header = request.headers.get("x-csrf-token", "")
            if not header or not constant_time_equals(header, existing.csrf_token):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_failed")
            existing.revoked_at = datetime.now(UTC)
            await session.commit()
    response.delete_cookie(settings.session_cookie, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/v1/profile")
async def get_profile(request: Request, owner: UserProfile = Depends(current_user)) -> dict:
    return {
        "owner_id": str(owner.id),
        "email": owner.email,
        "display_name": owner.display_name,
        "timezone": owner.timezone,
        "prefs": owner.prefs,
        "prefs_version": owner.prefs_version,
        "csrf_token": request.state.auth_session.csrf_token,
    }


@router.put("/v1/password", status_code=204)
async def change_password(
    body: PasswordChange,
    request: Request,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not verify_password(body.current_password, owner.password_hash):
        raise HTTPException(status_code=400, detail="current_password_incorrect")
    owner.password_hash = hash_password(body.new_password)
    await session.execute(
        update(Session).where(
            Session.owner_id == owner.id,
            Session.id != request.state.auth_session.id,
            Session.revoked_at.is_(None),
        ).values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    return Response(status_code=204)


@router.patch("/v1/profile")
async def patch_profile(
    body: ProfilePatch,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Preference edits apply to plans created *after* the edit (doc §9.1)."""
    prefs = dict(owner.prefs or {})
    if body.timezone:
        owner.timezone = body.timezone
    if body.daily_budget_ms is not None:
        prefs["daily_budget_ms"] = body.daily_budget_ms
    if body.depth:
        prefs["depth"] = body.depth
    if body.goals is not None:
        prefs["goals"] = body.goals
    if body.topics_down is not None:
        prefs["topics_down"] = body.topics_down
    owner.prefs = prefs
    owner.prefs_version += 1
    await session.commit()
    return {
        "prefs": prefs,
        "prefs_version": owner.prefs_version,
        "timezone": owner.timezone,
    }


def demo_prefs(settings: Settings) -> dict:
    return {
        "depth": "normal",
        "daily_budget_ms": settings.daily_budget_ms,
        "goals": ["理解机制而不是记住术语"],
        "topics_down": [],
        "spoken_style": "single_host_zh",
    }


async def ensure_demo_user(session: AsyncSession, settings: Settings) -> UserProfile | None:
    bootstrap = bool(settings.bootstrap_email and settings.bootstrap_password)
    if not bootstrap and not settings.seed_demo_user:
        return None
    email = (settings.bootstrap_email if bootstrap else settings.demo_email).lower()
    row = await session.execute(select(UserProfile).where(UserProfile.email == email))
    profile = row.scalar_one_or_none()
    if profile is not None:
        return profile
    profile = UserProfile(
        email=email,
        password_hash=hash_password(settings.bootstrap_password if bootstrap else settings.demo_password),
        display_name="个人学习空间" if bootstrap else "Demo",
        timezone=settings.app_timezone,
        prefs=demo_prefs(settings),
        prefs_version=1,
    )
    session.add(profile)
    await session.commit()
    return profile
