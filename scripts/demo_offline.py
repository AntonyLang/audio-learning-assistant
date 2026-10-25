"""End-to-end offline demo: text source -> episode -> audio -> publish -> RSS.

Run from the repository root:

    python scripts/demo_offline.py

It uses the deterministic adapters (``PROVIDER_MODE=fake``) and a real ffmpeg/ffprobe, so
the audio you get is a real, decodable MP3 whose duration was measured, not estimated.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.auth import demo_prefs
from app.config import get_settings
from app.db import SessionFactory, dispose_engine
from app.models import (
    Episode,
    EpisodeRevision,
    GenerationRun,
    RunKind,
    Source,
    SourceKind,
    StageArtifact,
    UserProfile,
)
from app.models import Session as _Session  # noqa: F401
from app.pipeline.runner import execute_run
from app.services import episode_service, runs
from app.services.devtools import reset_owner
from sqlalchemy import select


async def main() -> int:
    settings = get_settings()
    settings.provider_mode = "fake"
    settings.inline_worker = False
    article = (ROOT / "evals/fixtures/articles/distributed-idempotency.md").read_text("utf-8")
    question = os.environ.get(
        "DEMO_QUESTION", "调用超时为什么不能断定远端没有完成？出现未知结果时应该先做什么？"
    )

    async with SessionFactory() as session:
        owner = (
            await session.execute(
                select(UserProfile).where(UserProfile.email == "demo@local")
            )
        ).scalar_one_or_none()
        if owner is not None:
            await reset_owner(session, owner.id)
        if owner is None:
            from app.security import hash_password

            owner = UserProfile(
                email="demo@local",
                password_hash=hash_password("demo12345"),
                display_name="Demo",
                timezone=settings.app_timezone,
                prefs=demo_prefs(settings),
            )
            session.add(owner)
            await session.commit()
        source = Source(
            owner_id=owner.id,
            kind=SourceKind.TEXT,
            title="超时、重试与幂等（演示资料）",
            intent_note="离线演示",
        )
        session.add(source)
        await session.flush()
        stored = await __import__("app.adapters", fromlist=["build_adapters"]).build_adapters(
            settings, fixtures_dir=settings.offline_fixtures_dir
        ).storage.put_bytes(
            f"raw/{owner.id}/{source.id}/input.txt", article.encode("utf-8"), "text/plain"
        )
        source.input_ref = stored.key
        ingest_run, _ = await runs.create_run(
            session,
            runs.RunSpec(
                owner_id=owner.id,
                kind=RunKind.INGEST,
                request_key=f"ingest:{source.id}:ingest-1",
                source_id=source.id,
            ),
            settings,
        )
        await session.commit()
        source_id, owner_id, ingest_run_id = source.id, owner.id, ingest_run.id

    print(f"[1/4] ingest run {ingest_run_id}")
    await execute_run(ingest_run_id, worker_id="demo")

    async with SessionFactory() as session:
        owner = await session.get(UserProfile, owner_id)
        source = await session.get(Source, source_id)
        version = await episode_service.latest_ready_version(session, source_id=source_id)
        print(f"      version={version.id} chars={version.char_count} quality={version.quality.value}")
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner, source=source, question=question, settings=settings
        )
        await session.commit()
        episode_id, run_id = bundle.episode.id, bundle.run.id

    print(f"[2/4] generate run {run_id}")
    await execute_run(run_id, worker_id="demo")

    async with SessionFactory() as session:
        run = await session.get(GenerationRun, run_id)
        revision = (
            await session.execute(
                select(EpisodeRevision).where(EpisodeRevision.episode_id == episode_id)
            )
        ).scalar_one()
        print(
            f"      state={run.state.value} stop={run.stop_reason} "
            f"revision={revision.status.value} duration_ms={revision.duration_ms}"
        )
        print(f"      budget={run.budget_usage}")
        publish_run = (
            await session.execute(
                select(GenerationRun).where(
                    GenerationRun.revision_id == revision.id, GenerationRun.kind == RunKind.PUBLISH
                )
            )
        ).scalar_one_or_none()
        publish_run_id = publish_run.id if publish_run else None
        artifact = await session.get(StageArtifact, revision.audio_id) if revision.audio_id else None
        if artifact:
            print(f"      audio={artifact.object_key} bytes={artifact.byte_size} sha256={artifact.sha256[:16]}")

    if publish_run_id is None:
        print("      no publish run was scheduled")
        return 1
    print(f"[3/4] publish run {publish_run_id}")
    await execute_run(publish_run_id, worker_id="demo")

    async with SessionFactory() as session:
        episode = await session.get(Episode, episode_id)
        run = await session.get(GenerationRun, publish_run_id)
        print(
            f"      publish state={run.state.value} reason={run.stop_reason} "
            f"published={episode.published_revision_id is not None}"
        )
        print(f"      guid={episode.guid}")
        print(f"[4/4] RSS: {settings.public_base_url}/v1/feeds/<token>.xml")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
