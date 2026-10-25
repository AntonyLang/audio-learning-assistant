"""Shared helpers for integration tests (real PostgreSQL + real ffmpeg)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionFactory  # noqa: E402
from app.models import (  # noqa: E402
    EpisodeRevision,
    GenerationRun,
    RunKind,
    Source,
    SourceKind,
    SourceVersion,
    UserProfile,
)
from app.pipeline.runner import execute_run  # noqa: E402
from app.services import episode_service, runs  # noqa: E402
from app.services.ingest import EXTRACTOR_VERSION  # noqa: E402


async def create_text_source(
    session, owner: UserProfile, body: str, *, title: str = "测试资料"
) -> Source:
    from app.adapters import build_adapters

    settings = get_settings()
    adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    source = Source(owner_id=owner.id, kind=SourceKind.TEXT, title=title, intent_note="test")
    session.add(source)
    await session.flush()
    stored = await adapters.storage.put_bytes(
        f"raw/{owner.id}/{source.id}/input.txt", body.encode("utf-8"), "text/plain"
    )
    source.input_ref = stored.key
    run, _created = await runs.create_run(
        session,
        runs.RunSpec(
            owner_id=owner.id,
            kind=RunKind.INGEST,
            request_key=f"ingest:{source.id}:{EXTRACTOR_VERSION}",
            source_id=source.id,
        ),
        settings,
    )
    await session.commit()
    return source


async def ingest(source_id) -> None:
    async with SessionFactory() as session:
        run = (
            await session.execute(
                select(GenerationRun).where(
                    GenerationRun.source_id == source_id,
                    GenerationRun.kind == RunKind.INGEST,
                )
            )
        ).scalar_one()
        run_id = run.id
    await execute_run(run_id, worker_id="test")


async def prepare_ready_source(owner: UserProfile, body: str) -> Source:
    async with SessionFactory() as session:
        source = await create_text_source(session, owner, body)
        source_id = source.id
    await ingest(source_id)
    async with SessionFactory() as session:
        return await session.get(Source, source_id)


@dataclass(slots=True)
class Generated:
    episode_id: object
    revision_id: object
    generate_run_id: object
    publish_run_id: object | None


async def generate_episode(owner: UserProfile, source: Source, question: str = "") -> Generated:
    async with SessionFactory() as session:
        owner = await session.get(UserProfile, owner.id)
        source = await session.get(Source, source.id)
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner, source=source, question=question, settings=get_settings()
        )
        await session.commit()
        episode_id, revision_id, run_id = bundle.episode.id, bundle.revision.id, bundle.run.id
    await execute_run(run_id, worker_id="test")
    async with SessionFactory() as session:
        run = await session.get(GenerationRun, run_id)
        revision = await session.get(EpisodeRevision, revision_id)
        publish_run = (
            await session.execute(
                select(GenerationRun).where(
                    GenerationRun.revision_id == revision_id,
                    GenerationRun.kind == RunKind.PUBLISH,
                )
            )
        ).scalar_one_or_none()
        publish_run_id = publish_run.id if publish_run else None
        assert run.state.value in {"SUCCEEDED", "WAITING_REVIEW", "FAILED"}
        assert revision is not None
    if publish_run_id is not None:
        await execute_run(publish_run_id, worker_id="test")
    return Generated(episode_id, revision_id, run_id, publish_run_id)


async def latest_version(source_id) -> SourceVersion:
    from sqlalchemy import select

    async with SessionFactory() as session:
        return (
            await session.execute(
                select(SourceVersion)
                .where(SourceVersion.source_id == source_id)
                .order_by(SourceVersion.fetched_at.desc())
            )
        ).scalars().first()


def article() -> str:
    return (ROOT / "evals/fixtures/articles/distributed-idempotency.md").read_text("utf-8")


def ffprobe_duration_ms(path: Path) -> int:
    import subprocess

    output = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return round(float(output.stdout.decode().strip()) * 1000)


