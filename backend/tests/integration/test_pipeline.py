from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.adapters import Adapters, build_adapters
from app.adapters.base import AdapterError
from app.config import get_settings
from app.db import SessionFactory, session_scope
from app.graphs.episode import EpisodeRuntime, run_content_graph
from app.models import (
    DailyPlan,
    Episode,
    EpisodeRevision,
    GenerationRun,
    PlanItem,
    PlanItemState,
    ProviderCall,
    ProviderCallState,
    RevisionStatus,
    RunKind,
    RunState,
    Source,
    SourceVersion,
    StageArtifact,
    UserProfile,
)
from app.schemas.content import EvidencePack
from app.services import budget, publishing, runs
from app.services.validation import validate_evidence_pack
from tests.integration.helpers import (
    article,
    ffprobe_duration_ms,
    generate_episode,
    prepare_ready_source,
)

pytestmark = pytest.mark.integration

ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]


@pytest.fixture
async def adapters_fixture():
    settings = get_settings()
    return build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)


async def test_full_pipeline_measures_and_publishes(owner, adapters_fixture):
    settings = get_settings()
    source = await prepare_ready_source(owner, article())
    generated = await generate_episode(owner, source, "调用超时为什么不能断定远端没有完成？")

    async with SessionFactory() as session:
        run = await session.get(GenerationRun, generated.generate_run_id)
        revision = await session.get(EpisodeRevision, generated.revision_id)
        episode = await session.get(Episode, generated.episode_id)
        audio = await session.get(StageArtifact, revision.audio_id)
        pack_artifact = await session.get(StageArtifact, revision.evidence_id)

        assert run.state == RunState.SUCCEEDED, run.last_error
        assert revision.status == RevisionStatus.READY
        assert revision.duration_ms is not None
        assert 0 < revision.duration_ms <= settings.max_episode_ms
        assert episode.published_revision_id == revision.id
        assert episode.published_at is not None
        assert episode.guid

        pack = EvidencePack.model_validate(pack_artifact.payload)
        assert pack.items
        problems = await validate_evidence_pack(session, owner_id=owner.id, pack=pack)
        assert problems == []

        plan_item = (
            await session.execute(select(PlanItem).where(PlanItem.episode_id == episode.id))
        ).scalar_one()
        assert plan_item.state == PlanItemState.AVAILABLE
        assert plan_item.duration_ms == revision.duration_ms

        checks = await session.execute(
            select(ProviderCall).where(ProviderCall.run_id == generated.generate_run_id)
        )
        calls = list(checks.scalars())
        assert calls
        assert all(call.state in {ProviderCallState.SUCCEEDED, ProviderCallState.FAILED} for call in calls)
        assert any(call.stage == "tts" for call in calls)
        assert run.budget_usage["tts_chars"] > 0

    # The stored object really is a decodable MP3 of the measured length.
    stored = await adapters_fixture.storage.get_bytes(audio.object_key)
    local = ROOT / "_data" / f"verify-{uuid.uuid4().hex}.mp3"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(stored)
    try:
        assert abs(ffprobe_duration_ms(local) - revision.duration_ms) <= 60
    finally:
        local.unlink(missing_ok=True)


async def test_failed_tts_releases_the_reservation_and_retries(owner, adapters_fixture):
    class BrokenTTS:
        info = adapters_fixture.tts.info

        async def synthesize(self, text, voice, language):
            raise AdapterError("tts_failed_test", "boom", retryable=False)

        async def download(self, result):  # pragma: no cover
            raise AdapterError("tts_failed_test", "boom")

        async def download_url(self, url):  # pragma: no cover
            raise AdapterError("tts_failed_test", "boom")

    broken = Adapters(
        llm=adapters_fixture.llm,
        embedding=adapters_fixture.embedding,
        search=adapters_fixture.search,
        tts=BrokenTTS(),
        storage=adapters_fixture.storage,
        mode="fake",
    )
    source = await prepare_ready_source(owner, article())
    async with SessionFactory() as session:
        from app.services import episode_service

        owner_row = await session.get(UserProfile, owner.id)
        source_row = await session.get(Source, source.id)
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner_row, source=source_row, question="测试失败路径", settings=get_settings()
        )
        await session.commit()
        run_id, episode_id = bundle.run.id, bundle.episode.id

    from app.pipeline.runner import execute_run

    await execute_run(run_id, worker_id="test-broken", adapters=broken)

    async with SessionFactory() as session:
        run = await session.get(GenerationRun, run_id)
        item = (
            await session.execute(select(PlanItem).where(PlanItem.episode_id == episode_id))
        ).scalar_one()
        assert run.state == RunState.FAILED
        assert item.state == PlanItemState.DROPPED
        assert item.reserved_ms == 0


async def test_attempt_fencing_blocks_a_stale_worker(owner):
    settings = get_settings()
    source = await prepare_ready_source(owner, article())
    async with SessionFactory() as session:
        from app.services import episode_service

        owner_row = await session.get(UserProfile, owner.id)
        source_row = await session.get(Source, source.id)
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner_row, source=source_row, question="fencing", settings=settings
        )
        await session.commit()
        run_id = bundle.run.id

    async with session_scope() as session:
        first = await runs.claim_attempt(session, run_id=run_id, worker_id="w1", settings=settings)
    assert first is not None

    async with session_scope() as session:
        second = await runs.claim_attempt(session, run_id=run_id, worker_id="w2", settings=settings)
    assert second is None

    # Simulate a lost lea: the lease expires and a new attempt takes over.
    async with SessionFactory() as session:
        run = await session.get(GenerationRun, run_id)
        run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

    async with SessionFactory() as session:
        takeover = await runs.claim_attempt(session, run_id=run_id, worker_id="w2", settings=settings)
    assert takeover is not None
    assert takeover.attempt_no == 2

    async with session_scope() as session:
        with pytest.raises(runs.LostAuthority):
            await runs.finish_succeeded(session, run_id=run_id, attempt_id=first.attempt_id)


async def test_budget_refuses_a_third_search(owner):
    settings = get_settings()
    async with SessionFactory() as session:
        run = GenerationRun(
            owner_id=owner.id,
            kind=RunKind.GENERATE,
            request_key=f"budget:{uuid.uuid4()}",
            state=RunState.QUEUED,
            budget_usage={},
            event_seq=0,
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    await budget.consume(run_id, budget.KEY_SEARCHES)
    await budget.consume(run_id, budget.KEY_SEARCHES)
    with pytest.raises(budget.BudgetExceeded):
        await budget.consume(run_id, budget.KEY_SEARCHES)

    snapshot = await budget.snapshot(run_id)
    assert snapshot.usage[budget.KEY_SEARCHES] == settings.max_searches_per_episode


async def test_evidence_pack_rejects_a_moved_quote(owner):
    source = await prepare_ready_source(owner, article())
    async with SessionFactory() as session:
        version = (
            await session.execute(
                select(SourceVersion).where(SourceVersion.source_id == source.id)
            )
        ).scalar_one()
        from app.models import Chunk

        chunk = (
            await session.execute(
                select(Chunk).where(Chunk.source_version_id == version.id).limit(1)
            )
        ).scalar_one()
        body = version.body_text
        start, end = chunk.start_char + 2, chunk.start_char + 12
        good = EvidencePack(
            pack_id="p",
            primary_version_id=version.id,
            items=[
                {
                    "source_version_id": version.id,
                    "chunk_id": chunk.id,
                    "span_start": start,
                    "span_end": end,
                    "quote": body[start:end],
                }
            ],
        )
        assert await validate_evidence_pack(session, owner_id=owner.id, pack=good) == []

        moved = good.model_copy(
            update={"items": [good.items[0].model_copy(update={"span_start": start + 1, "span_end": end + 1})]}
        )
        problems = await validate_evidence_pack(session, owner_id=owner.id, pack=moved)
        assert any(problem.code == "quote_mismatch" for problem in problems)


async def test_publish_defers_when_the_day_has_no_room(owner):
    """The plan is a budget: a ready episode that does not fit is deferred, not failed."""
    settings = get_settings()
    source = await prepare_ready_source(owner, article())
    first = await generate_episode(owner, source, "第一篇占用预算的节目")

    async with SessionFactory() as session:
        plan = (await session.execute(select(DailyPlan).where(DailyPlan.owner_id == owner.id))).scalar_one()
        plan.budget_ms = 50_000  # smaller than the measured episode
        source_row = await session.get(Source, source.id)
        first_revision = await session.get(EpisodeRevision, first.revision_id)
        episode = Episode(
            owner_id=owner.id,
            primary_source_id=source_row.id,
            question="第二篇放不下",
            guid=uuid.uuid4().hex,
        )
        session.add(episode)
        await session.flush()
        revision = EpisodeRevision(
            episode_id=episode.id,
            revision_no=1,
            status=RevisionStatus.READY,
            audio_id=first_revision.audio_id,
            duration_ms=first_revision.duration_ms,
        )
        session.add(revision)
        await session.flush()
        artifact = await session.get(StageArtifact, revision.audio_id)
        verification = publishing.AudioVerification(
            artifact_id=artifact.id,
            sha256=artifact.sha256 or "",
            duration_ms=artifact.duration_ms or 0,
            decodable=True,
        )
        result = await publishing.publish_revision(
            session,
            owner_id=owner.id,
            plan_id=plan.id,
            episode_id=episode.id,
            revision_id=revision.id,
            verification=verification,
            settings=settings,
        )
        await session.commit()
        assert result.status == publishing.PublishStatus.DEFERRED
        item = (
            await session.execute(select(PlanItem).where(PlanItem.episode_id == episode.id))
        ).scalar_one()
        assert item.state == PlanItemState.DEFERRED
        assert episode.published_revision_id is None

async def test_published_revision_is_fixed(owner):
    source = await prepare_ready_source(owner, article())
    generated = await generate_episode(owner, source, "发布版本能不能被替换？")
    async with SessionFactory() as session:
        episode = await session.get(Episode, generated.episode_id)
        owner_row = await session.get(UserProfile, owner.id)
        plan = (
            await session.execute(select(DailyPlan).where(DailyPlan.owner_id == owner.id))
        ).scalar_one()
        revision = await session.get(EpisodeRevision, generated.revision_id)
        artifact = await session.get(StageArtifact, revision.audio_id)
        verification = publishing.AudioVerification(
            artifact_id=artifact.id, sha256=artifact.sha256 or "", duration_ms=artifact.duration_ms or 0, decodable=True
        )
        other = EpisodeRevision(
            episode_id=episode.id,
            revision_no=2,
            status=RevisionStatus.READY,
            audio_id=artifact.id,
            duration_ms=artifact.duration_ms,
        )
        session.add(other)
        await session.flush()
        result = await publishing.publish_revision(
            session,
            owner_id=owner_row.id,
            plan_id=plan.id,
            episode_id=episode.id,
            revision_id=other.id,
            verification=verification,
            settings=get_settings(),
        )
        assert result.status == publishing.PublishStatus.CONFLICT


async def test_content_graph_marks_uncovered_concepts_as_gaps(owner, adapters_fixture):
    settings = get_settings()
    source = await prepare_ready_source(owner, article())
    async with SessionFactory() as session:
        version = (
            await session.execute(select(SourceVersion).where(SourceVersion.source_id == source.id))
        ).scalar_one()
        from app.services import episode_service

        owner_row = await session.get(UserProfile, owner.id)
        source_row = await session.get(Source, source.id)
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner_row, source=source_row, question="SSE 事件序号为什么不能用全局自增？", settings=settings
        )
        await session.commit()
        revision_id, episode_id = bundle.revision.id, bundle.episode.id
        run_id = bundle.run.id

    runtime = EpisodeRuntime(
        settings=settings,
        adapters=adapters_fixture,
        owner_id=owner.id,
        run_id=run_id,
        attempt_id=uuid.uuid4(),
        episode_id=episode_id,
        revision_id=revision_id,
        primary_version_id=version.id,
        question="SSE 事件序号为什么不能用全局自增？断线重连后如何回放？",
    )
    # The graph requires an owning attempt; claim one so authority checks pass.
    async with session_scope() as session:
        claim = await runs.claim_attempt(session, run_id=run_id, worker_id="graph", settings=settings)
    runtime.attempt_id = claim.attempt_id
    final = await run_content_graph(runtime)
    assert final["queries_used"] >= 1
    assert final["external_version_ids"], "expected at least one external page"
    async with SessionFactory() as session:
        pack_artifact = await session.get(StageArtifact, uuid.UUID(final["evidence_artifact_id"]))
        pack = EvidencePack.model_validate(pack_artifact.payload)
        assert any(item.origin.value == "external" for item in pack.items)
