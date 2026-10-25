"""Run executor: phase-by-phase, session-per-transaction, lease-renewed.

The Celery task, the inline dev worker and the tests all call :func:`execute_run`; nothing
about the pipeline depends on how the message arrived.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from app.adapters import Adapters, build_adapters
from app.adapters.base import AdapterError
from app.config import Settings, get_settings
from app.db import session_scope
from app.graphs.episode import EpisodeRuntime, EpisodeState, run_content_graph
from app.models import (
    ArtifactKind,
    Episode,
    EpisodeRevision,
    GenerationRun,
    Phase,
    RevisionStatus,
    RunKind,
    RunState,
    Source,
    SourceVersion,
    StageArtifact,
    UserProfile,
)
from app.models.enums import IndexState, StopReason
from app.services import artifacts, audio, budget, ingest, planning, publishing, runs
from app.services.events import append_event

logger = logging.getLogger(__name__)


class RunFailed(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retryable = retryable


async def execute_run(
    run_id: uuid.UUID, *, worker_id: str = "inline", adapters: Adapters | None = None
) -> None:
    """Execute one claimed attempt.

    ``adapters`` is injectable so tests can substitute a failing or slow provider without
    touching global configuration.
    """
    settings = get_settings()
    adapters = adapters or build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
    async with session_scope() as session:
        claim = await runs.claim_attempt(
            session, run_id=run_id, worker_id=worker_id, settings=settings
        )
        if claim is None:
            logger.info("run %s not claimable", run_id)
            return
        run = await session.get(GenerationRun, run_id)
        kind = run.kind

    heartbeat = asyncio.create_task(_heartbeat_loop(run_id, claim.attempt_id, settings))
    try:
        if kind == RunKind.INGEST:
            await _execute_ingest(run_id, claim.attempt_id, settings, adapters)
        elif kind == RunKind.GENERATE:
            await _execute_generate(run_id, claim.attempt_id, settings, adapters)
        elif kind == RunKind.PUBLISH:
            await _execute_publish(run_id, claim.attempt_id, settings, adapters)
        else:  # pragma: no cover - enum is closed
            raise RunFailed("unknown_run_kind", kind)
    except runs.LostAuthority:
        logger.warning("run %s lost its lease during attempt; stale worker stops", run_id)
        return
    except (ingest.IngestError, audio.AudioError, RunFailed) as exc:
        await _handle_failure(run_id, claim.attempt_id, settings, exc)
    except AdapterError as exc:
        await _handle_failure(
            run_id, claim.attempt_id, settings, RunFailed(exc.code, str(exc), retryable=exc.retryable)
        )
    except budget.BudgetExceeded as exc:
        await _handle_failure(
            run_id, claim.attempt_id, settings, RunFailed("budget_exceeded", str(exc))
        )
    except Exception as exc:  # noqa: BLE001 - recorded and surfaced, never swallowed
        logger.exception("run %s crashed", run_id)
        await _handle_failure(
            run_id, claim.attempt_id, settings, RunFailed("internal_error", repr(exc))
        )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _heartbeat_loop(run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.heartbeat_seconds)
        try:
            async with session_scope() as session:
                ok = await runs.heartbeat(
                    session, run_id=run_id, attempt_id=attempt_id, settings=settings
                )
            if not ok:
                logger.warning("lease lost for run %s", run_id)
                return
        except Exception:  # noqa: BLE001
            logger.exception("heartbeat failed for run %s", run_id)
            return


async def _handle_failure(
    run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings, exc: Exception
) -> None:
    code = getattr(exc, "code", "run_failed")
    retryable = bool(getattr(exc, "retryable", False))
    async with session_scope() as session:
        retrying = await runs.schedule_retry(
            session,
            run_id=run_id,
            attempt_id=attempt_id,
            reason=code,
            settings=settings,
            retryable=retryable,
        )
        if not retrying:
            run = await session.get(GenerationRun, run_id)
            if run.kind == RunKind.GENERATE and run.episode_id is not None:
                released = await planning.release_reservation(
                    session, episode_id=run.episode_id
                )
                if released:
                    await append_event(
                        session, run_id, "plan.released", {"items": released, "reason": code}
                    )


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


async def _execute_ingest(
    run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings, adapters: Adapters
) -> None:
    async with session_scope() as session:
        run = await session.get(GenerationRun, run_id)
        source_id = run.source_id
        owner_id = run.owner_id
        await runs.set_phase(
            session, run_id=run_id, attempt_id=attempt_id, phase=Phase.INGEST
        )
        await append_event(session, run_id, "ingest.started", {"source_id": str(source_id)})
    if source_id is None:
        raise RunFailed("ingest_missing_source")

    async with session_scope() as session:
        result = await ingest.ingest_source(
            session,
            settings=settings,
            adapters=adapters,
            source_id=source_id,
            owner_id=owner_id,
        )
        await append_event(
            session,
            run_id,
            "ingest.done",
            {
                "source_version_id": str(result.source_version_id) if result.source_version_id else None,
                "quality": result.quality.value,
                "chunks": result.chunk_count,
                "chars": result.char_count,
                "reused": result.reused,
            },
        )

    async with session_scope() as session:
        await runs.finish_succeeded(session, run_id=run_id, attempt_id=attempt_id)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


async def _execute_generate(
    run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings, adapters: Adapters
) -> None:
    async with session_scope() as session:
        run = await session.get(GenerationRun, run_id)
        if run.revision_id is None or run.episode_id is None:
            raise RunFailed("run_missing_revision")
        revision = await session.get(EpisodeRevision, run.revision_id)
        episode = await session.get(Episode, run.episode_id)
        source = await session.get(Source, episode.primary_source_id)
        versions = await _ready_versions(session, source.id)
        if not versions:
            raise RunFailed("source_not_ready", "run ingest first", retryable=False)
        primary = versions[0]
        owner = await session.get(UserProfile, run.owner_id)
        preference_snapshot = dict(owner.prefs or {})
        question = episode.question

    runtime = EpisodeRuntime(
        settings=settings,
        adapters=adapters,
        owner_id=run.owner_id,
        run_id=run_id,
        attempt_id=attempt_id,
        episode_id=episode.id,
        revision_id=revision.id,
        primary_version_id=primary.id,
        question=question,
        preference_snapshot=preference_snapshot,
        target_seconds=settings.target_episode_ms // 1000,
    )

    final: EpisodeState = await run_content_graph(runtime)
    if final.get("stop_reason") == "evidence_blocked":
        async with session_scope() as session:
            await append_event(
                session,
                run_id,
                "run.needs_input",
                {"reason": "evidence_blocked", "unresolved": final.get("unresolved", [])},
            )
            await runs.finish_waiting_review(
                session,
                run_id=run_id,
                attempt_id=attempt_id,
                reason="evidence_blocked",
                unresolved=final.get("unresolved", []),
            )
        return
    if final.get("stop_reason") == "review_required" or not final.get("script_artifact_id"):
        async with session_scope() as session:
            await runs.finish_waiting_review(
                session,
                run_id=run_id,
                attempt_id=attempt_id,
                reason="verification_failed",
                unresolved=final.get("unresolved", []),
            )
        return

    script_artifact_id = uuid.UUID(final["script_artifact_id"])
    duration = await _synthesize_and_measure(
        run_id=run_id,
        attempt_id=attempt_id,
        owner_id=run.owner_id,
        revision_id=revision.id,
        script_artifact_id=script_artifact_id,
        settings=settings,
        adapters=adapters,
        runtime=runtime,
    )
    if duration is None:
        return

    if duration.duration_ms > settings.max_episode_ms:
        await _handle_overlong(
            run_id=run_id,
            attempt_id=attempt_id,
            owner_id=run.owner_id,
            revision_id=revision.id,
            duration=duration,
            settings=settings,
            adapters=adapters,
            runtime=runtime,
            script_artifact_id=script_artifact_id,
            reason="duration_exceeded",
        )
        return

    await _finalize_generation(
        run_id=run_id,
        attempt_id=attempt_id,
        owner_id=run.owner_id,
        revision_id=revision.id,
        duration=duration,
        settings=settings,
        adapters=adapters,
    )


async def _ready_versions(session, source_id: uuid.UUID) -> list[SourceVersion]:
    from sqlalchemy import select

    rows = await session.execute(
        select(SourceVersion)
        .where(
            SourceVersion.source_id == source_id,
            SourceVersion.index_state == IndexState.READY,
        )
        .order_by(SourceVersion.fetched_at.desc())
    )
    return list(rows.scalars())


async def _synthesize_and_measure(
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    owner_id: uuid.UUID,
    revision_id: uuid.UUID,
    script_artifact_id: uuid.UUID,
    settings: Settings,
    adapters: Adapters,
    runtime: EpisodeRuntime,
):
    """TTS -> assemble -> decode -> ffprobe. Returns the measured artifact or ``None``."""
    async with session_scope() as session:
        await runs.set_phase(session, run_id=run_id, attempt_id=attempt_id, phase=Phase.TTS)
        artifact = await session.get(StageArtifact, script_artifact_id)
        from app.schemas.content import ScriptDoc

        script = ScriptDoc.model_validate(artifact.payload)
    plans = audio.plan_segments(script.spoken_text, settings.max_segment_chars)
    async with session_scope() as session:
        await append_event(
            session,
            run_id,
            "tts.planned",
            {"segments": len(plans), "chars": script.char_count},
        )
    try:
        segments = await audio.synthesize_segments(
            settings=settings,
            adapters=adapters,
            owner_id=owner_id,
            run_id=run_id,
            attempt_id=attempt_id,
            plans=plans,
        )
    except audio.AudioError:
        raise
    assembled = await audio.assemble(
        settings=settings,
        adapters=adapters,
        owner_id=owner_id,
        segments=segments,
        work_dir=settings.work_dir,
    )
    async with session_scope() as session:
        await runs.set_phase(
            session, run_id=run_id, attempt_id=attempt_id, phase=Phase.AUDIO_CHECK
        )
        artifact, _created = await artifacts.upsert(
            session,
            owner_id=owner_id,
            kind=ArtifactKind.AUDIO_FINAL,
            input_fingerprint=audio.final_fingerprint(segments, settings),
            payload={
                "segments": [s.sha256 for s in segments],
                "segment_count": len(segments),
                "duration_ms": assembled.duration_ms,
                "sample_rate": settings.audio_sample_rate,
                "bitrate": settings.audio_bitrate,
                "model": settings.tts_model,
                "voice": settings.tts_voice,
            },
            object_key=assembled.object_key,
            sha256=assembled.sha256,
            byte_size=assembled.byte_size,
            duration_ms=assembled.duration_ms,
        )
        revision_row = await session.get(EpisodeRevision, revision_id, with_for_update=True)
        revision_row.audio_id = artifact.id
        revision_row.duration_ms = assembled.duration_ms
        revision_row.status = (
            RevisionStatus.READY
            if assembled.duration_ms <= settings.max_episode_ms
            else RevisionStatus.AUDIO_READY
        )
        await append_event(
            session,
            run_id,
            "audio.measured",
            {
                "artifact_id": str(artifact.id),
                "duration_ms": assembled.duration_ms,
                "segments": len(segments),
                "within_limit": assembled.duration_ms <= settings.max_episode_ms,
            },
        )
    return audio.AudioMeasure(
        artifact_id=artifact.id,
        duration_ms=assembled.duration_ms,
        object_key=assembled.object_key,
        sha256=assembled.sha256,
        segment_count=len(segments),
    )


async def _handle_overlong(
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    owner_id: uuid.UUID,
    revision_id: uuid.UUID,
    duration,
    settings: Settings,
    adapters: Adapters,
    runtime: EpisodeRuntime,
    script_artifact_id: uuid.UUID,
    reason: str,
) -> None:
    """One shortened re-draft is allowed; a second failure goes to review (doc §7.2)."""
    if not await budget.can_consume(run_id, budget.KEY_AUDIO_REVISIONS, 1):
        async with session_scope() as session:
            await runs.finish_waiting_review(
                session,
                run_id=run_id,
                attempt_id=attempt_id,
                reason="audio_too_long",
                unresolved=[{"kind": "audio", "duration_ms": duration.duration_ms}],
            )
        return
    await budget.consume(run_id, budget.KEY_AUDIO_REVISIONS, 1)
    async with session_scope() as session:
        await append_event(
            session,
            run_id,
            "audio.revision_started",
            {"measured_ms": duration.duration_ms, "limit_ms": settings.max_episode_ms},
        )
        artifact = await session.get(StageArtifact, script_artifact_id)
        from app.schemas.content import ScriptDoc

        script = ScriptDoc.model_validate(artifact.payload)
    runtime.target_seconds = audio.revision_target_seconds(
        script.char_count, duration.duration_ms, settings
    )
    final = await run_content_graph(runtime)
    if final.get("stop_reason") or not final.get("script_artifact_id"):
        async with session_scope() as session:
            await runs.finish_waiting_review(
                session, run_id=run_id, attempt_id=attempt_id, reason="revision_blocked"
            )
        return
    second = await _synthesize_and_measure(
        run_id=run_id,
        attempt_id=attempt_id,
        owner_id=owner_id,
        revision_id=revision_id,
        script_artifact_id=uuid.UUID(final["script_artifact_id"]),
        settings=settings,
        adapters=adapters,
        runtime=runtime,
    )
    if second is None:
        return
    if second.duration_ms > settings.max_episode_ms:
        async with session_scope() as session:
            await runs.finish_waiting_review(
                session,
                run_id=run_id,
                attempt_id=attempt_id,
                reason="audio_still_too_long",
                unresolved=[{"kind": "audio", "duration_ms": second.duration_ms}],
            )
        return
    await _finalize_generation(
        run_id=run_id,
        attempt_id=attempt_id,
        owner_id=owner_id,
        revision_id=revision_id,
        duration=second,
        settings=settings,
        adapters=adapters,
    )


async def _finalize_generation(
    *,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    owner_id: uuid.UUID,
    revision_id: uuid.UUID,
    duration,
    settings: Settings,
    adapters: Adapters,
) -> None:
    async with session_scope() as session:
        revision = await session.get(EpisodeRevision, revision_id, with_for_update=True)
        revision.status = RevisionStatus.READY
        revision.audio_id = duration.artifact_id
        revision.duration_ms = duration.duration_ms
        run = await session.get(GenerationRun, run_id)
        publish_run, created = await runs.create_run(
            session,
            runs.RunSpec(
                owner_id=owner_id,
                kind=RunKind.PUBLISH,
                request_key=f"publish:{revision_id}",
                episode_id=run.episode_id,
                revision_id=revision_id,
            ),
            settings,
        )
        await append_event(
            session,
            run_id,
            "audio.ready",
            {
                "revision_id": str(revision_id),
                "duration_ms": duration.duration_ms,
                "publish_run_id": str(publish_run.id),
                "publish_run_created": created,
            },
        )
        await runs.finish_succeeded(
            session, run_id=run_id, attempt_id=attempt_id, stop_reason=StopReason.COMPLETED.value
        )


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


async def _execute_publish(
    run_id: uuid.UUID, attempt_id: uuid.UUID, settings: Settings, adapters: Adapters
) -> None:
    async with session_scope() as session:
        run = await session.get(GenerationRun, run_id)
        episode_id = run.episode_id
        revision_id = run.revision_id
        owner_id = run.owner_id
        if episode_id is None or revision_id is None:
            raise RunFailed("publish_missing_episode")
        revision = await session.get(EpisodeRevision, revision_id)
        audio_artifact = (
            await session.get(StageArtifact, revision.audio_id) if revision.audio_id else None
        )
        if audio_artifact is None:
            raise RunFailed("publish_missing_audio")
        owner = await session.get(UserProfile, owner_id)
        local_date = planning.local_date_for(owner)
        plan = await planning.get_or_create_plan(
            session,
            owner_id=owner_id,
            local_date=local_date,
            timezone_name=owner.timezone,
            budget_ms=int((owner.prefs or {}).get("daily_budget_ms", settings.daily_budget_ms)),
            prefs=owner.prefs or {},
        )
        plan_id = plan.id

    verification = await publishing.verify_audio(adapters, audio_artifact)
    async with session_scope() as session:
        await runs.set_phase(session, run_id=run_id, attempt_id=attempt_id, phase=Phase.PUBLISH)
        result = await publishing.publish_revision(
            session,
            owner_id=owner_id,
            plan_id=plan_id,
            episode_id=episode_id,
            revision_id=revision_id,
            verification=verification,
            settings=settings,
            run_id=run_id,
        )
        if result.published:
            await runs.finish_succeeded(
                session, run_id=run_id, attempt_id=attempt_id, stop_reason=result.status.value
            )
        elif result.status == publishing.PublishStatus.DEFERRED:
            run = await session.get(GenerationRun, run_id, with_for_update=True)
            run.state = RunState.DEFERRED
            run.stop_reason = "plan_deferred"
            run.active_attempt_id = None
            run.lease_expires_at = None
            await append_event(
                session, run_id, "run.deferred", {"reason": result.reason}
            )
        elif result.status == publishing.PublishStatus.ALREADY_PUBLISHED:
            await runs.finish_succeeded(
                session, run_id=run_id, attempt_id=attempt_id, stop_reason="already_published"
            )
        else:
            await runs.finish_failed(
                session, run_id=run_id, attempt_id=attempt_id, reason=result.reason or result.status.value
            )
