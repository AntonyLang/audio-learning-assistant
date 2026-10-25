"""Ingestion: fetch -> snapshot -> immutable version -> chunks -> index (doc §4).

Nothing is overwritten: a new extraction of the same URL produces a new ``source_version``
because the uniqueness key includes the extractor version and the content hash.
"""

from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import feedparser
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import Adapters
from app.adapters.base import AdapterError
from app.adapters.fetch import UrlFetcher
from app.config import Settings
from app.models import (
    Chunk,
    IndexState,
    Quality,
    Source,
    SourceKind,
    SourceVersion,
    SourceVersionState,
)
from app.services.chunking import build_blocks, chunk_blocks
from app.services.textproc import lexemes_for, normalize_text, sha256_bytes, sha256_text

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "ingest-1"
MIN_USEFUL_CHARS = 200
MIN_PAGE_TEXT_RATIO = 0.3


class IngestError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class IngestResult:
    source_version_id: uuid.UUID | None
    state: SourceVersionState
    quality: Quality
    char_count: int
    chunk_count: int
    reused: bool = False


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = parts.path or "/"
    return urlunsplit((scheme, host + port, path, parts.query, ""))


def classify_quality(kind: SourceKind, text_chars: int, pages_with_text: int, page_count: int) -> Quality:
    if kind == SourceKind.PDF and text_chars == 0:
        return Quality.NEEDS_INPUT
    if text_chars < MIN_USEFUL_CHARS:
        return Quality.PARTIAL
    if page_count and pages_with_text / page_count < MIN_PAGE_TEXT_RATIO:
        return Quality.PARTIAL
    return Quality.FULL


async def ingest_source(    session: AsyncSession,
    *,
    settings: Settings,
    adapters: Adapters,
    source_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> IngestResult:
    source = await session.get(Source, source_id)
    if source is None or source.owner_id != owner_id:
        raise IngestError("source_not_found", str(source_id))

    fetcher = UrlFetcher(settings, fixtures_dir=settings.offline_fixtures_dir)
    if source.kind == SourceKind.RSS:
        created = await _ingest_feed(session, adapters, source)
        return IngestResult(None, SourceVersionState.READY, Quality.FULL, 0, created)

    try:
        if source.kind == SourceKind.URL:
            page = await fetcher.fetch(source.canonical_url or "")
            text = page.text
            title = page.title or source.title
            if page.body:
                key = f"raw/{owner_id}/{source.id}/{sha256_bytes(page.body)[:16]}.bin"
                await adapters.storage.put_bytes(key, page.body, page.content_type or "text/html")
                source.input_ref = key
            char_map: list[tuple[int, int, int]] = []
        elif source.kind == SourceKind.PDF:
            raw = await adapters.storage.get_bytes(source.input_ref or "")
            if len(raw) > settings.max_pdf_bytes:
                raise IngestError("pdf_too_large", f"{len(raw)} bytes")
            text, title, char_map = _extract_pdf(raw, settings.max_pdf_pages)
        else:  # TEXT
            raw = await adapters.storage.get_bytes(source.input_ref or "")
            text = normalize_text(raw.decode("utf-8", errors="replace"))
            title = source.title
            char_map = []
    except (AdapterError, IngestError) as exc:
        code = getattr(exc, "code", "ingest_failed")
        retryable = getattr(exc, "retryable", False)
        await _record_failed_version(session, source, code)
        raise IngestError(code, str(exc), retryable=retryable) from exc

    if not text.strip():
        await _record_failed_version(session, source, "empty_text")
        raise IngestError("empty_text", "no readable body text")

    pages_with_text = len({p for p, _s, _e in char_map if True}) if char_map else 0
    page_count = len(char_map)
    quality = classify_quality(source.kind, len(text), pages_with_text, page_count)
    state = {
        Quality.FULL: SourceVersionState.READY,
        Quality.PARTIAL: SourceVersionState.PARTIAL,
        Quality.NEEDS_INPUT: SourceVersionState.PARTIAL,
        Quality.FAILED: SourceVersionState.FAILED,
    }[quality]

    content_hash = sha256_text(text)
    version = await _get_or_create_version(
        session,
        source_id=source.id,
        content_hash=content_hash,
        text=text,
        title=title or source.title,
        quality=quality,
        state=state,
        page_count=page_count,
    )
    source.title = title or source.title
    if version.index_state == IndexState.READY:
        chunk_count = await _count_chunks(session, version.id)
        return IngestResult(version.id, state, quality, version.char_count, chunk_count, reused=True)

    chunk_count = await _build_index(
        session, adapters, owner_id=owner_id, source_version=version, text=text,
        pages=char_map, settings=settings,
    )
    version.state = state
    version.index_state = IndexState.READY
    await session.flush()
    return IngestResult(version.id, state, quality, version.char_count, chunk_count)


async def _get_or_create_version(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    content_hash: str,
    text: str,
    title: str,
    quality: Quality,
    state: SourceVersionState,
    page_count: int,
) -> SourceVersion:
    stmt = select(SourceVersion).where(
        SourceVersion.source_id == source_id,
        SourceVersion.content_hash == content_hash,
        SourceVersion.extractor_version == EXTRACTOR_VERSION,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    version = SourceVersion(
        source_id=source_id,
        content_hash=content_hash,
        extractor_version=EXTRACTOR_VERSION,
        body_text=text,
        title=title,
        quality=quality,
        state=SourceVersionState.EXTRACTING,
        index_state=IndexState.PENDING,
        char_count=len(text),
        page_count=page_count,
    )
    session.add(version)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        session.expunge(version)
        return (await session.execute(stmt)).scalar_one()
    return version


async def _record_failed_version(session: AsyncSession, source: Source, code: str) -> None:
    session.add(
        SourceVersion(
            source_id=source.id,
            content_hash=sha256_text(f"{source.id}:{code}:{uuid.uuid4()}"),
            extractor_version=EXTRACTOR_VERSION,
            body_text="",
            title=source.title,
            quality=Quality.FAILED,
            state=SourceVersionState.FAILED,
            index_state=IndexState.FAILED,
        )
    )
    await session.flush()


async def _build_index(
    session: AsyncSession,
    adapters: Adapters,
    *,
    owner_id: uuid.UUID,
    source_version: SourceVersion,
    text: str,
    pages: list[tuple[int, int, int]],
    settings: Settings,
) -> int:
    source_version.index_state = IndexState.BUILDING
    blocks = build_blocks(text, pages)
    drafts = chunk_blocks(
        text,
        blocks,
        min_chars=settings.chunk_min_chars,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    if not drafts:
        drafts = []
    embeddings = await adapters.embedding.embed(
        [d.text for d in drafts]
    ) if drafts else []
    for draft, vector in zip(drafts, embeddings, strict=True):
        session.add(
            Chunk(
                owner_id=owner_id,
                source_version_id=source_version.id,
                ordinal=draft.ordinal,
                section_path=draft.section_path,
                page_no=draft.page_no,
                paragraph_id=draft.paragraph_id,
                start_char=draft.start_char,
                end_char=draft.end_char,
                text=draft.text,
                lexemes=lexemes_for(draft.text),
                embedding=vector,
                embedding_profile=adapters.embedding_profile,
                content_hash=draft.content_hash,
            )
        )
    await session.flush()
    return len(drafts)


async def _count_chunks(session: AsyncSession, version_id: uuid.UUID) -> int:
    rows = await session.execute(select(Chunk.id).where(Chunk.source_version_id == version_id))
    return len(list(rows.scalars()))


async def _ingest_feed(session: AsyncSession, adapters: Adapters, source: Source) -> int:
    if not source.input_ref:
        raise IngestError("feed_missing_payload", str(source.id))
    raw = await adapters.storage.get_bytes(source.input_ref)
    parsed = feedparser.parse(raw)
    created = 0
    for entry in parsed.entries[:20]:
        link = getattr(entry, "link", "")
        if not link:
            continue
        canonical = canonicalize_url(link)
        exists = await session.execute(
            select(Source.id).where(
                Source.owner_id == source.owner_id,
                Source.parent_source_id == source.id,
                Source.canonical_url == canonical,
            )
        )
        if exists.scalar_one_or_none():
            continue
        session.add(
            Source(
                owner_id=source.owner_id,
                kind=SourceKind.URL,
                canonical_url=canonical,
                title=(getattr(entry, "title", "") or "")[:200],
                parent_source_id=source.id,
                intent_note="from RSS",
            )
        )
        created += 1
    await session.flush()
    return created


def _extract_pdf(raw: bytes, max_pages: int) -> tuple[str, str, list[tuple[int, int, int]]]:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:  # pypdf raises a variety of errors
        raise IngestError("pdf_unreadable", str(exc)) from exc
    page_texts: list[str] = []
    for index, page in enumerate(reader.pages):
        if index >= max_pages:
            break
        page_texts.append(normalize_text(page.extract_text() or ""))
    body_parts: list[str] = []
    char_map: list[tuple[int, int, int]] = []
    cursor = 0
    for page_no, page_text in enumerate(page_texts, start=1):
        if not page_text:
            continue
        if body_parts:
            cursor += 2
        start = cursor
        body_parts.append(page_text)
        cursor += len(page_text)
        char_map.append((page_no, start, cursor))
    title = ""
    metadata = getattr(reader, "metadata", None)
    if metadata is not None and metadata.title:
        title = str(metadata.title)
    return "\n\n".join(body_parts), title, char_map

async def ingest_external_page(
    session: AsyncSession,
    *,
    settings: Settings,
    adapters: Adapters,
    owner_id: uuid.UUID,
    page,
) -> SourceVersion:
    """Store a fetched external page as its own immutable version + chunks.

    External evidence is only citable if it is snapshotted: a search snippet is never
    allowed into an EvidencePack (doc §5.2).
    """
    canonical = canonicalize_url(page.url)
    existing_source = (
        await session.execute(
            select(Source).where(
                Source.owner_id == owner_id,
                Source.canonical_url == canonical,
                Source.kind == SourceKind.URL,
            )
        )
    ).scalar_one_or_none()
    if existing_source is None:
        existing_source = Source(
            owner_id=owner_id,
            kind=SourceKind.URL,
            canonical_url=canonical,
            title=(page.title or canonical)[:200],
            intent_note="external evidence",
        )
        session.add(existing_source)
        await session.flush()

    text = normalize_text(page.text)
    if not text.strip():
        raise IngestError("external_empty_text", canonical)
    content_hash = sha256_text(text)
    if page.body:
        key = f"raw/{owner_id}/{existing_source.id}/{content_hash[:16]}.bin"
        await adapters.storage.put_bytes(key, page.body, page.content_type or "text/html")
        existing_source.input_ref = key

    version = await _get_or_create_version(
        session,
        source_id=existing_source.id,
        content_hash=content_hash,
        text=text,
        title=page.title or existing_source.title,
        quality=Quality.FULL,
        state=SourceVersionState.READY,
        page_count=0,
    )
    if version.index_state != IndexState.READY:
        await _build_index(
            session,
            adapters,
            owner_id=owner_id,
            source_version=version,
            text=text,
            pages=[],
            settings=settings,
        )
        version.state = SourceVersionState.READY
        version.quality = Quality.FULL
        version.index_state = IndexState.READY
        await session.flush()
    return version
