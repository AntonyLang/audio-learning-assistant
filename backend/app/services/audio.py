"""Segmented TTS, deterministic assembly and real-duration acceptance (doc §7).

Four rules drive this module:

* a segment ≤ 400 characters (the provider's 600-char limit minus margin), counted after
  the spoken text is normalised;
* a segment cache keyed by content *and* configuration, so "same text, different voice"
  never collides;
* the final MP3 is re-decoded and measured with ffprobe — a script's character count is
  an estimate, never a guarantee;
* audio that exceeds 300 000 ms is rejected here, not adjusted later.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from app.adapters import Adapters
from app.adapters.base import AdapterError, ProviderUnknown
from app.config import Settings
from app.db import session_scope
from app.models import ArtifactKind, ProviderCall, ProviderCallState, StageArtifact
from app.services import artifacts, budget, calls
from app.services.textproc import sha256_bytes, sha256_text, split_sentences

logger = logging.getLogger(__name__)

PRONUNCIATION_TABLE_VERSION = "zh-v1"
MAX_SEGMENT_CHARS = 400


class AudioError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class SegmentPlan:
    index: int
    text: str
    start_char: int
    end_char: int
    fingerprint: str = ""


@dataclass(slots=True)
class SegmentAudio:
    index: int
    artifact_id: uuid.UUID
    object_key: str
    sha256: str
    byte_size: int
    duration_ms: int | None


@dataclass(slots=True)
class AssembledAudio:
    object_key: str
    sha256: str
    byte_size: int
    duration_ms: int
    segment_count: int


@dataclass(frozen=True, slots=True)
class AudioMeasure:
    """Result of a successful measure-and-store pass over the final audio."""

    artifact_id: uuid.UUID
    duration_ms: int
    object_key: str
    sha256: str
    segment_count: int


def normalize_spoken(text: str) -> str:
    """Strip markdown noise that a TTS engine would read out loud."""
    cleaned = text.replace("**", "").replace("`", "").replace("#", "")
    cleaned = cleaned.replace("（", "，前括号，").replace("）", "，后括号，")
    cleaned = cleaned.replace("\r", "\n")
    lines = [" ".join(line.split()) for line in cleaned.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def plan_segments(spoken: str, max_chars: int = MAX_SEGMENT_CHARS) -> list[SegmentPlan]:
    """Split normalised spoken text into provider-sized segments, preserving order."""
    text = normalize_spoken(spoken)
    plans: list[SegmentPlan] = []
    buffer = ""
    buffer_start = 0
    cursor = 0
    for sentence in split_sentences(text):
        position = text.find(sentence, cursor)
        if position < 0:
            position = cursor
        if len(sentence) > max_chars:
            pieces = _hard_split(sentence, max_chars)
        else:
            pieces = [sentence]
        for piece in pieces:
            offset = text.find(piece, position)
            if offset < 0:
                offset = position
            position = offset + len(piece)
            if buffer and len(buffer) + len(piece) > max_chars:
                plans.append(
                    SegmentPlan(
                        index=len(plans),
                        text=buffer,
                        start_char=buffer_start,
                        end_char=buffer_start + len(buffer),
                    )
                )
                buffer = ""
            if not buffer:
                buffer_start = offset
            buffer += piece
            cursor = max(cursor, position)
    if buffer:
        plans.append(
            SegmentPlan(
                index=len(plans),
                text=buffer,
                start_char=buffer_start,
                end_char=buffer_start + len(buffer),
            )
        )
    return plans


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    remaining = sentence
    while len(remaining) > max_chars:
        cut = max(
            remaining.rfind("，", 0, max_chars),
            remaining.rfind("、", 0, max_chars),
            remaining.rfind(",", 0, max_chars),
        )
        if cut < max_chars // 2:
            cut = max_chars - 1
        pieces.append(remaining[: cut + 1])
        remaining = remaining[cut + 1 :]
    if remaining:
        pieces.append(remaining)
    return pieces


def segment_fingerprint(
    *, owner_id: uuid.UUID, spoken: str, settings: Settings, voice: str, language: str
) -> str:
    payload = {
        "owner": str(owner_id),
        "spoken_hash": sha256_text(normalize_spoken(spoken)),
        "model": settings.tts_model,
        "voice": voice,
        "language": language,
        "pronunciation": PRONUNCIATION_TABLE_VERSION,
        "sample_rate": settings.audio_sample_rate,
        "bitrate": settings.audio_bitrate,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


async def synthesize_segments(
    *,
    settings: Settings,
    adapters: Adapters,
    owner_id: uuid.UUID,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID,
    plans: list[SegmentPlan],
) -> list[SegmentAudio]:
    """Synthesize (or reuse) one audio artifact per planned segment.

    Order of preference: an immutable segment artifact, then a successful provider call
    from an earlier attempt (download only), then a new synthesis call.
    """
    results: list[SegmentAudio] = []
    for plan in plans:
        fingerprint = segment_fingerprint(
            owner_id=owner_id,
            spoken=plan.text,
            settings=settings,
            voice=settings.tts_voice,
            language=settings.tts_language,
        )
        async with session_scope() as session:
            cached = await session.execute(
                select(StageArtifact).where(
                    StageArtifact.owner_id == owner_id,
                    StageArtifact.kind == ArtifactKind.AUDIO_SEGMENT,
                    StageArtifact.input_fingerprint == fingerprint,
                )
            )
            hit = cached.scalar_one_or_none()
            if hit is not None:
                await budget.consume(run_id, budget.KEY_TTS_CHARS, len(plan.text))
                results.append(
                    SegmentAudio(
                        index=plan.index,
                        artifact_id=hit.id,
                        object_key=hit.object_key or "",
                        sha256=hit.sha256 or "",
                        byte_size=hit.byte_size or 0,
                        duration_ms=hit.duration_ms,
                    )
                )
                continue
            pending = await session.execute(
                select(ProviderCall).where(
                    ProviderCall.run_id == run_id,
                    ProviderCall.input_fingerprint == fingerprint,
                    ProviderCall.state == ProviderCallState.SUCCEEDED,
                    ProviderCall.audio_url.is_not(None),
                )
            )
            reusable = pending.scalars().first()
            call = await calls.begin_call(
                session,
                run_id=run_id,
                attempt_id=attempt_id,
                stage="tts",
                provider=tts_provider(adapters),
                input_fingerprint=fingerprint,
            )
            call_id = call.id
            reusable_url = reusable.audio_url if reusable is not None else None

        if reusable_url:
            try:
                raw = await adapters.tts.download_url(reusable_url)
            except AdapterError as exc:
                raise AudioError(exc.code, "resume download", retryable=True) from exc
            async with session_scope() as session:
                await calls.succeed_call(
                    session,
                    await session.get(ProviderCall, call_id),
                    audio_url=reusable_url,
                    usage={"resumed": True},
                )
            stored = await _store_segment(adapters, owner_id, raw, "audio/mpeg")
            artifact_id = await _record_segment_artifact(
                settings=settings,
                owner_id=owner_id,
                plan=plan,
                fingerprint=fingerprint,
                stored=stored,
                request_id=f"resume_{fingerprint[:12]}",
                sessionmaker=session_scope,
            )
            results.append(
                SegmentAudio(
                    index=plan.index,
                    artifact_id=artifact_id,
                    object_key=stored.key,
                    sha256=stored.sha256,
                    byte_size=stored.size,
                    duration_ms=None,
                )
            )
            continue

        try:
            tts_result = await adapters.tts.synthesize(
                plan.text, settings.tts_voice, settings.tts_language
            )
        except AdapterError as exc:
            async with session_scope() as session:
                await calls.fail_call(
                    session,
                    await session.get(ProviderCall, call_id),
                    code=exc.code,
                    unknown=isinstance(exc, ProviderUnknown),
                )
            raise AudioError(exc.code, plan.text[:32], retryable=exc.retryable) from exc

        async with session_scope() as session:
            await calls.succeed_call(
                session,
                await session.get(ProviderCall, call_id),
                request_id=tts_result.provider_request_id,
                audio_url=tts_result.audio_url,
                expires_at=tts_result.expires_at,
                usage=tts_result.usage.to_dict(),
            )
        raw = tts_result.audio
        if raw is None:
            try:
                raw = await adapters.tts.download(tts_result)
            except AdapterError as exc:
                raise AudioError(exc.code, "download after synthesis", retryable=True) from exc
        await budget.consume(run_id, budget.KEY_TTS_CHARS, len(plan.text))
        suffix = "wav" if tts_result.content_type.endswith("wav") else "mp3"
        stored = await _store_segment(adapters, owner_id, raw, tts_result.content_type, suffix)
        artifact_id = await _record_segment_artifact(
            settings=settings,
            owner_id=owner_id,
            plan=plan,
            fingerprint=fingerprint,
            stored=stored,
            request_id=tts_result.provider_request_id,
            sessionmaker=session_scope,
        )
        results.append(
            SegmentAudio(
                index=plan.index,
                artifact_id=artifact_id,
                object_key=stored.key,
                sha256=stored.sha256,
                byte_size=stored.size,
                duration_ms=None,
            )
        )
    return results


def tts_provider(adapters: Adapters) -> str:
    info = getattr(adapters.tts, "info", None)
    return getattr(info, "provider", "tts")


async def _store_segment(adapters: Adapters, owner_id: uuid.UUID, raw: bytes, content_type: str, suffix: str = "mp3"):
    digest = sha256_bytes(raw)
    key = f"audio/{owner_id}/segments/{digest}.{suffix}"
    return await adapters.storage.put_bytes(key, raw, content_type)


async def _record_segment_artifact(
    *, settings, owner_id, plan, fingerprint, stored, request_id, sessionmaker
) -> uuid.UUID:
    async with sessionmaker() as session:
        artifact, _created = await artifacts.upsert(
            session,
            owner_id=owner_id,
            kind=ArtifactKind.AUDIO_SEGMENT,
            input_fingerprint=fingerprint,
            payload={
                "segment_index": plan.index,
                "spoken_hash": sha256_text(plan.text),
                "model": settings.tts_model,
                "voice": settings.tts_voice,
                "language": settings.tts_language,
                "pronunciation": PRONUNCIATION_TABLE_VERSION,
                "start_char": plan.start_char,
                "end_char": plan.end_char,
                "request_id": request_id,
            },
            object_key=stored.key,
            sha256=stored.sha256,
            byte_size=stored.size,
        )
        return artifact.id

def final_fingerprint(segments: list[SegmentAudio], settings: Settings) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "segments": [s.sha256 for s in segments],
                "sample_rate": settings.audio_sample_rate,
                "bitrate": settings.audio_bitrate,
                "format": "mp3",
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


async def assemble(
    *,
    settings: Settings,
    adapters: Adapters,
    owner_id: uuid.UUID,
    segments: list[SegmentAudio],
    work_dir: Path,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> AssembledAudio:
    work_dir.mkdir(parents=True, exist_ok=True)
    job_dir = work_dir / f"job_{uuid.uuid4().hex[:12]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        normalized: list[Path] = []
        for segment in segments:
            source = job_dir / f"src_{segment.index:04d}.bin"
            source.write_bytes(await adapters.storage.get_bytes(segment.object_key))
            target = job_dir / f"norm_{segment.index:04d}.wav"
            await _ffmpeg(
                [
                    ffmpeg, "-y", "-v", "error",
                    "-i", str(source),
                    "-ac", "1",
                    "-ar", str(settings.audio_sample_rate),
                    "-c:a", "pcm_s16le",
                    str(target),
                ]
            )
            normalized.append(target)
        if not normalized:
            raise AudioError("no_segments")
        listing = job_dir / "concat.txt"
        # absolute paths: the concat demuxer resolves relative entries against its own dir
        listing.write_text(
            "\n".join(f"file '{path.resolve().as_posix()}'" for path in normalized),
            encoding="utf-8",
        )
        final = job_dir / "final.mp3"
        await _ffmpeg(
            [
                ffmpeg, "-y", "-v", "error",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:a", "libmp3lame", "-b:a", settings.audio_bitrate,
                "-ar", str(settings.audio_sample_rate), "-ac", "1",
                str(final),
            ]
        )
        await _verify_decodable(final, ffmpeg)
        duration_ms = await probe_duration_ms(final, ffprobe)
        raw = final.read_bytes()
        digest = sha256_bytes(raw)
        key = f"audio/{owner_id}/final/{digest}.mp3"
        stored = await adapters.storage.put_bytes(key, raw, "audio/mpeg")
        return AssembledAudio(
            object_key=stored.key,
            sha256=stored.sha256,
            byte_size=stored.size,
            duration_ms=duration_ms,
            segment_count=len(normalized),
        )
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def probe_duration_ms(path: Path, ffprobe: str = "ffprobe") -> int:
    """Read the *measured* duration and round up to whole milliseconds."""
    proc = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AudioError("ffprobe_failed", stderr.decode("utf-8", "replace")[:300])
    value = stdout.decode("utf-8", "replace").strip()
    return duration_to_ms(value)


def duration_to_ms(value: str) -> int:
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise AudioError("duration_unparseable", value) from exc
    if seconds <= 0:
        raise AudioError("duration_zero", value)
    return int((seconds * 1000).to_integral_value(rounding=ROUND_CEILING))


async def _verify_decodable(path: Path, ffmpeg: str = "ffmpeg") -> None:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AudioError("decode_failed", stderr.decode("utf-8", "replace")[:300])


async def _ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise AudioError("ffmpeg_failed", stderr.decode("utf-8", "replace")[:300])


def enforce_duration(duration_ms: int, settings: Settings) -> None:
    if duration_ms > settings.max_episode_ms:
        raise AudioError(
            "duration_exceeded", f"{duration_ms}ms > {settings.max_episode_ms}ms", retryable=False
        )


def revision_target_seconds(current_chars: int, measured_ms: int, settings: Settings) -> int:
    """Shrink the next draft proportionally, keeping the 270 s target as the ceiling."""
    if measured_ms <= 0:
        return settings.target_episode_ms // 1000
    ratio = settings.target_episode_ms / measured_ms
    target_chars = current_chars * ratio
    return max(60, int(target_chars / settings.spoken_chars_per_second))
