"""Inbox: submit a URL, plain text, PDF or RSS feed (doc §4.1, §11)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import build_adapters
from app.adapters.fetch import UrlFetcher
from app.api.deps import current_user, idempotency_key, idempotent_replay, payload_hash, remember
from app.config import Settings, get_settings
from app.db import get_session
from app.jobs.dispatch import get_dispatcher
from app.models import (
    GenerationRun,
    IndexState,
    Quality,
    RunKind,
    Source,
    SourceKind,
    SourceVersion,
    UserProfile,
)
from app.services import runs
from app.services.ingest import EXTRACTOR_VERSION

router = APIRouter(tags=["sources"])


class SourceCreate(BaseModel):
    kind: Literal["url", "text", "rss"]
    url: str | None = None
    text: str | None = Field(default=None, max_length=500_000)
    title: str = ""
    intent_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _payload_for_kind(self) -> SourceCreate:
        if self.kind in {"url", "rss"}:
            if not self.url or not self.url.lower().startswith(("http://", "https://")):
                raise ValueError("url must be http(s)")
        if self.kind == "text" and not (self.text and self.text.strip()):
            raise ValueError("text is required")
        return self


class SourceCreated(BaseModel):
    source_id: str
    run_id: str
    state: str
    status_url: str
    events_url: str


@router.post("/v1/sources", response_model=SourceCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_source(
    body: SourceCreate,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    key: str | None = Depends(idempotency_key),
) -> SourceCreated:
    endpoint = "POST /v1/sources"
    digest = payload_hash(body.model_dump())
    replay = await idempotent_replay(
        session, owner_id=owner.id, endpoint=endpoint, key=key, payload_digest=digest
    )
    if replay is not None:
        return SourceCreated(**replay)

    adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    kind = {
        "url": SourceKind.URL,
        "text": SourceKind.TEXT,
        "rss": SourceKind.RSS,
    }[body.kind]
    source = Source(
        owner_id=owner.id,
        kind=kind,
        canonical_url=body.url if body.kind in {"url", "rss"} else None,
        title=body.title[:200],
        intent_note=body.intent_note,
    )
    session.add(source)
    await session.flush()

    raw_bytes: bytes | None = None
    if body.kind == "text":
        raw_bytes = (body.text or "").encode("utf-8")
        suffix = "txt"
    elif body.kind == "rss":
        page = await UrlFetcher(settings, fixtures_dir=settings.offline_fixtures_dir).fetch(body.url or "")
        raw_bytes = page.body
        suffix = "xml"
        source.title = source.title or page.title
    else:
        raw_bytes = None
        suffix = "html"
    if raw_bytes is not None:
        stored = await adapters.storage.put_bytes(
            f"raw/{owner.id}/{source.id}/input.{suffix}", raw_bytes, "application/octet-stream"
        )
        source.input_ref = stored.key

    run, _created = await runs.create_run(
        session,
        runs.RunSpec(
            owner_id=owner.id,
            kind=RunKind.INGEST,
            request_key=f"ingest:{source.id}:{EXTRACTOR_VERSION}",
            payload_hash=digest,
            source_id=source.id,
        ),
        settings,
    )
    response = SourceCreated(
        source_id=str(source.id),
        run_id=str(run.id),
        state=run.state.value,
        status_url=f"/v1/runs/{run.id}",
        events_url=f"/v1/runs/{run.id}/events",
    )
    await remember(
        session,
        owner_id=owner.id,
        endpoint=endpoint,
        key=key,
        payload_digest=digest,
        response=response.model_dump(),
        status_code=202,
    )
    await session.commit()
    get_dispatcher().enqueue(run.id)
    return response


@router.post(
    "/v1/sources/upload", response_model=SourceCreated, status_code=status.HTTP_202_ACCEPTED
)
async def upload_pdf(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    intent_note: str = Form(default=""),
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SourceCreated:
    data = await file.read()
    if len(data) > settings.max_pdf_bytes:
        raise HTTPException(status_code=413, detail="pdf_too_large")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="not_a_pdf")
    adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    source = Source(
        owner_id=owner.id,
        kind=SourceKind.PDF,
        title=(title or file.filename or "PDF")[:200],
        intent_note=intent_note,
    )
    session.add(source)
    await session.flush()
    stored = await adapters.storage.put_bytes(
        f"raw/{owner.id}/{source.id}/input.pdf", data, "application/pdf"
    )
    source.input_ref = stored.key
    run, _created = await runs.create_run(
        session,
        runs.RunSpec(
            owner_id=owner.id,
            kind=RunKind.INGEST,
            request_key=f"ingest:{source.id}:{EXTRACTOR_VERSION}",
            payload_hash=stored.sha256,
            source_id=source.id,
        ),
        settings,
    )
    await session.commit()
    get_dispatcher().enqueue(run.id)
    return SourceCreated(
        source_id=str(source.id),
        run_id=str(run.id),
        state=run.state.value,
        status_url=f"/v1/runs/{run.id}",
        events_url=f"/v1/runs/{run.id}/events",
    )


@router.get("/v1/sources")
async def list_sources(
    owner: UserProfile = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict:
    rows = await session.execute(
        select(Source).where(Source.owner_id == owner.id).order_by(Source.created_at.desc()).limit(50)
    )
    items = []
    for source in rows.scalars():
        version = (
            await session.execute(
                select(SourceVersion)
                .where(SourceVersion.source_id == source.id)
                .order_by(SourceVersion.fetched_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        items.append(_source_card(source, version))
    return {"items": items}


@router.get("/v1/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    source = await session.get(Source, source_id)
    if source is None or source.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="source_not_found")
    version = (
        await session.execute(
            select(SourceVersion)
            .where(SourceVersion.source_id == source.id)
            .order_by(SourceVersion.fetched_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    runs_rows = await session.execute(
        select(GenerationRun)
        .where(GenerationRun.source_id == source.id)
        .order_by(GenerationRun.created_at.desc())
        .limit(5)
    )
    card = _source_card(source, version)
    card["runs"] = [
        {"run_id": str(run.id), "kind": run.kind.value, "state": run.state.value} for run in runs_rows.scalars()
    ]
    return card


def _source_card(source: Source, version: SourceVersion | None) -> dict:
    return {
        "source_id": str(source.id),
        "kind": source.kind.value,
        "title": source.title,
        "canonical_url": source.canonical_url,
        "intent_note": source.intent_note,
        "created_at": source.created_at.isoformat() if source.created_at else None,
        "version": None
        if version is None
        else {
            "source_version_id": str(version.id),
            "quality": version.quality.value,
            "state": version.state.value,
            "index_state": version.index_state.value,
            "chars": version.char_count,
            "pages": version.page_count,
            "extractor_version": version.extractor_version,
            "fetched_at": version.fetched_at.isoformat() if version.fetched_at else None,
        },
        "ready": bool(
            version is not None
            and version.index_state == IndexState.READY
            and version.quality in {Quality.FULL, Quality.PARTIAL}
        ),
    }


