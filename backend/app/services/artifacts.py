"""Immutable stage artifacts.

Only a fully validated product is inserted. ``(owner_id, kind, input_fingerprint)`` is
unique, which is how a re-run reuses a previously validated EvidencePack or audio segment
instead of paying for it twice (doc §7.1, §10.3).
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ArtifactKind, EpisodeRevision, StageArtifact


def fingerprint(payload: dict | str | list) -> str:
    if isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def upsert(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    kind: ArtifactKind,
    input_fingerprint: str,
    payload: dict | None = None,
    object_key: str | None = None,
    sha256: str | None = None,
    byte_size: int | None = None,
    duration_ms: int | None = None,
) -> tuple[StageArtifact, bool]:
    existing = await session.execute(
        select(StageArtifact).where(
            StageArtifact.owner_id == owner_id,
            StageArtifact.kind == kind,
            StageArtifact.input_fingerprint == input_fingerprint,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row, False
    artifact = StageArtifact(
        owner_id=owner_id,
        kind=kind,
        input_fingerprint=input_fingerprint,
        payload=payload or {},
        object_key=object_key,
        sha256=sha256,
        byte_size=byte_size,
        duration_ms=duration_ms,
    )
    session.add(artifact)
    await session.flush()
    return artifact, True


async def attach_to_revision(
    session: AsyncSession, revision_id: uuid.UUID, kind: ArtifactKind, artifact_id: uuid.UUID
) -> None:
    revision = await session.get(EpisodeRevision, revision_id, with_for_update=True)
    if revision is None:
        raise ValueError(f"unknown revision {revision_id}")
    if kind == ArtifactKind.EVIDENCE:
        revision.evidence_id = artifact_id
    elif kind == ArtifactKind.SCRIPT:
        revision.script_id = artifact_id
    elif kind == ArtifactKind.AUDIO_FINAL:
        revision.audio_id = artifact_id
    await session.flush()


async def get(session: AsyncSession, artifact_id: uuid.UUID) -> StageArtifact | None:
    return await session.get(StageArtifact, artifact_id)
