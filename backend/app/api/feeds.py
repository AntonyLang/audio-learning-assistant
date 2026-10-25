"""Private RSS and the stable media route (doc §12).

The feed is served by our own API on a stable URL; the object key behind it can move.
Mounted via a token whose hash is stored, so the feed can be rotated without changing any
episode GUID. Media supports exactly one byte range, because that is what podcast clients
actually request, and a proxied S3 response would not translate ranges for us.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import build_adapters
from app.api.deps import current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.models import (
    Episode,
    EpisodeRevision,
    FeedToken,
    Source,
    StageArtifact,
    UserProfile,
)
from app.security import hash_token, new_token

router = APIRouter(tags=["feeds"])

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


class FeedTokenCreated(BaseModel):
    feed_token_id: str
    token: str
    url: str


@router.post("/v1/feed-tokens", response_model=FeedTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_feed_token(
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FeedTokenCreated:
    token = new_token(24)
    record = FeedToken(owner_id=owner.id, token_hash=hash_token(token))
    session.add(record)
    await session.commit()
    return FeedTokenCreated(
        feed_token_id=str(record.id),
        token=token,
        url=f"{settings.public_base_url.rstrip('/')}/v1/feeds/{token}.xml",
    )


@router.get("/v1/feed-tokens")
async def list_feed_tokens(
    owner: UserProfile = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict:
    rows = await session.execute(
        select(FeedToken).where(FeedToken.owner_id == owner.id).order_by(FeedToken.created_at.desc())
    )
    return {
        "items": [
            {
                "feed_token_id": str(row.id),
                "created_at": row.created_at.isoformat(),
                "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            }
            for row in rows.scalars()
        ]
    }


@router.delete("/v1/feed-tokens/{feed_token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_feed_token(
    feed_token_id: uuid.UUID,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    record = await session.get(FeedToken, feed_token_id)
    if record is None or record.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="feed_token_not_found")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/v1/feeds/{token}.xml")
async def feed_xml(
    token: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    owner_id = await _owner_for_token(session, token)
    rows = await session.execute(
        select(Episode, EpisodeRevision, Source, StageArtifact)
        .join(EpisodeRevision, EpisodeRevision.id == Episode.published_revision_id)
        .join(Source, Source.id == Episode.primary_source_id)
        .join(StageArtifact, StageArtifact.id == EpisodeRevision.audio_id)
        .where(Episode.owner_id == owner_id, Episode.withdrawn_at.is_(None))
        .order_by(Episode.published_at.desc())
        .limit(100)
    )
    items = []
    for episode, revision, source, artifact in rows.all():
        items.append(
            {
                "episode": episode,
                "revision": revision,
                "source": source,
                "artifact": artifact,
            }
        )
    xml = build_rss(items, token=token, settings=settings)
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


def build_rss(items: list[dict], *, token: str, settings: Settings) -> bytes:
    ET.register_namespace("itunes", ITUNES)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "个人音频学习助手"
    ET.SubElement(channel, "link").text = settings.public_base_url
    ET.SubElement(channel, "description").text = "把阅读清单变成有来源、时长受控的中文短讲。"
    ET.SubElement(channel, "language").text = "zh-cn"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(
        datetime.now(UTC), usegmt=True
    )
    base = settings.public_base_url.rstrip("/")
    for item in items:
        episode: Episode = item["episode"]
        revision: EpisodeRevision = item["revision"]
        artifact: StageArtifact = item["artifact"]
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = episode.question or f"第 {episode.guid[:6]} 期"
        ET.SubElement(entry, "guid", {"isPermaLink": "false"}).text = episode.guid
        published = episode.published_at or revision.created_at or datetime.now(UTC)
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        ET.SubElement(entry, "pubDate").text = format_datetime(published, usegmt=True)
        duration_ms = int(artifact.duration_ms or 0)
        ET.SubElement(entry, "description").text = (
            f"单集时长 {duration_ms / 1000:.1f} 秒；每个要点都可回到原文片段核对。"
        )
        ET.SubElement(
            entry,
            "enclosure",
            {
                "url": f"{base}/v1/feeds/{token}/episodes/{episode.id}/audio/{revision.id}.mp3",
                "length": str(artifact.byte_size or 0),
                "type": "audio/mpeg",
            },
        )
        total_seconds = round(duration_ms / 1000)
        ET.SubElement(entry, f"{{{ITUNES}}}duration").text = (
            f"{total_seconds // 60}:{total_seconds % 60:02d}"
        )
        ET.SubElement(entry, f"{{{ITUNES}}}explicit").text = "false"
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


@router.api_route("/v1/episodes/{episode_id}/audio", methods=["GET", "HEAD"])
async def episode_audio(
    episode_id: uuid.UUID,
    request: Request,
    owner: UserProfile = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """The web player uses the session, not a feed token; range semantics are identical."""
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.owner_id != owner.id or episode.withdrawn_at is not None:
        raise HTTPException(status_code=404, detail="episode_not_found")
    if episode.published_revision_id is None:
        raise HTTPException(status_code=409, detail="episode_not_published")
    revision = await session.get(EpisodeRevision, episode.published_revision_id)
    if revision is None or revision.audio_id is None:
        raise HTTPException(status_code=404, detail="audio_not_found")
    artifact = await session.get(StageArtifact, revision.audio_id)
    if artifact is None or not artifact.object_key:
        raise HTTPException(status_code=404, detail="audio_not_found")
    adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    return await _serve_audio(
        adapters=adapters,
        artifact=artifact,
        request=request,
        range_header=range_header,
        filename=f"{episode.guid}.mp3",
    )


@router.api_route(
    "/v1/feeds/{token}/episodes/{episode_id}/audio/{revision_file}",
    methods=["GET", "HEAD"],
)
async def media(
    token: str,
    episode_id: uuid.UUID,
    revision_file: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    revision_part = revision_file.removesuffix(".mp3")
    try:
        revision_id = uuid.UUID(revision_part)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="audio_not_found") from exc

    owner_id = await _owner_for_token(session, token)
    episode = await session.get(Episode, episode_id)
    if episode is None or episode.owner_id != owner_id or episode.withdrawn_at is not None:
        raise HTTPException(status_code=404, detail="episode_not_found")
    if episode.published_revision_id != revision_id:
        raise HTTPException(status_code=404, detail="revision_not_published")
    revision = await session.get(EpisodeRevision, revision_id)
    if revision is None or revision.audio_id is None:
        raise HTTPException(status_code=404, detail="audio_not_found")
    artifact = await session.get(StageArtifact, revision.audio_id)
    if artifact is None or not artifact.object_key:
        raise HTTPException(status_code=404, detail="audio_not_found")

    adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    return await _serve_audio(
        adapters=adapters,
        artifact=artifact,
        request=request,
        range_header=range_header,
        filename=f"{episode.guid}.mp3",
    )


async def _serve_audio(
    *,
    adapters,
    artifact: StageArtifact,
    request: Request,
    range_header: str | None,
    filename: str,
) -> Response:
    stored = await adapters.storage.stat(artifact.object_key)
    size = stored.size
    try:
        window = parse_single_range(range_header, size)
    except ValueError:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
        )
    if window is None:
        start, end = 0, size - 1
        status_code = 200
    else:
        start, end = window
        status_code = 206
    length = end - start + 1
    headers = {
        "Content-Type": "audio/mpeg",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)
    return StreamingResponse(
        adapters.storage.stream(artifact.object_key, start, length),
        status_code=status_code,
        headers=headers,
        media_type="audio/mpeg",
    )

def parse_single_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive (start, end) window, ``None`` for a full 200 response.

    Multiple ranges are deliberately not implemented: the document requires ignoring
    them and returning the whole object rather than half-supporting multipart/byteranges.
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec:
        return None
    if "-" not in spec:
        raise ValueError("malformed range")
    start_text, end_text = spec.split("-", 1)
    if size <= 0:
        raise ValueError("empty object")
    if start_text == "":
        try:
            suffix = int(end_text)
        except ValueError as exc:
            raise ValueError("malformed suffix") from exc
        if suffix <= 0:
            raise ValueError("malformed suffix")
        start = max(0, size - suffix)
        return start, size - 1
    try:
        start = int(start_text)
    except ValueError as exc:
        raise ValueError("malformed start") from exc
    if start >= size or start < 0:
        raise ValueError("unsatisfiable start")
    if end_text == "":
        return start, size - 1
    try:
        end = int(end_text)
    except ValueError as exc:
        raise ValueError("malformed end") from exc
    if end < start:
        raise ValueError("inverted range")
    return start, min(end, size - 1)


async def _owner_for_token(session: AsyncSession, token: str) -> uuid.UUID:
    row = await session.execute(
        select(FeedToken).where(FeedToken.token_hash == hash_token(token))
    )
    record = row.scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        raise HTTPException(status_code=404, detail="feed_token_invalid")
    return record.owner_id
