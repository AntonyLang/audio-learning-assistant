"""Evidence and script validation (doc §6.2).

Layer 1 is structural and fully deterministic: every reference must resolve to a version
the owner may read, the quoted span must be *relocatable* in that version's stored text,
and the chunk must actually contain it. Layer 2 is the semantic check, which the LLM
performs and which never replaces layer 1.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Source, SourceVersion
from app.schemas.content import Claim, EvidencePack, ScriptDoc


@dataclass(slots=True)
class Problem:
    code: str
    detail: str
    index: int = -1

    def as_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "index": self.index}


async def validate_evidence_pack(
    session: AsyncSession, *, owner_id: uuid.UUID, pack: EvidencePack
) -> list[Problem]:
    problems: list[Problem] = []
    if not pack.items:
        problems.append(Problem("empty_pack", "no evidence items"))
        return problems

    version_ids = {item.source_version_id for item in pack.items}
    rows = await session.execute(
        select(SourceVersion.id, SourceVersion.body_text)
        .join(Source, Source.id == SourceVersion.source_id)
        .where(SourceVersion.id.in_(version_ids), Source.owner_id == owner_id)
    )
    bodies = {row[0]: row[1] for row in rows}
    for item in pack.items:
        if item.source_version_id not in bodies:
            problems.append(
                Problem("version_not_owned", f"version {item.source_version_id} not readable", -1)
            )

    chunk_ids = {item.chunk_id for item in pack.items}
    chunk_rows = await session.execute(
        select(Chunk.id, Chunk.source_version_id, Chunk.start_char, Chunk.end_char).where(
            Chunk.id.in_(chunk_ids)
        )
    )
    chunks = {row[0]: (row[1], row[2], row[3]) for row in chunk_rows}

    for index, item in enumerate(pack.items):
        body = bodies.get(item.source_version_id)
        if body is None:
            continue
        if not (0 <= item.span_start < item.span_end <= len(body)):
            problems.append(
                Problem("span_out_of_range", f"{item.span_start}..{item.span_end} / {len(body)}", index)
            )
            continue
        actual = body[item.span_start : item.span_end]
        if actual != item.quote:
            problems.append(
                Problem("quote_mismatch", f"stored text differs at {item.span_start}..{item.span_end}", index)
            )
        chunk = chunks.get(item.chunk_id)
        if chunk is None:
            problems.append(Problem("chunk_missing", str(item.chunk_id), index))
            continue
        chunk_version, chunk_start, chunk_end = chunk
        if chunk_version != item.source_version_id:
            problems.append(Problem("chunk_version_mismatch", str(item.chunk_id), index))
        if not (chunk_start <= item.span_start and item.span_end <= chunk_end):
            problems.append(Problem("span_outside_chunk", str(item.chunk_id), index))
    return problems


def validate_script_structure(script: ScriptDoc, pack: EvidencePack) -> list[Problem]:
    problems: list[Problem] = []
    known = {
        (str(item.source_version_id), str(item.chunk_id), item.span_start, item.span_end)
        for item in pack.items
    }
    segment_claim_ids: set[str] = set()
    for segment in script.segments:
        segment_claim_ids.update(segment.claim_ids)
    for claim in script.claims:
        if claim.critical and claim.kind.value in {"external_fact", "internal_fact"} and not claim.evidence:
            problems.append(Problem("critical_without_evidence", claim.claim_id))
        for ref in claim.evidence:
            key = (str(ref.source_version_id), str(ref.chunk_id), ref.span_start, ref.span_end)
            if key not in known:
                problems.append(Problem("ref_not_in_pack", claim.claim_id))
        if claim.claim_id not in segment_claim_ids and claim.critical:
            problems.append(Problem("claim_not_spoken", claim.claim_id))
    if not script.spoken_text.strip():
        problems.append(Problem("empty_script", "spoken_text is empty"))
    return problems


def merge_review(
    script: ScriptDoc, verdicts: list[dict], *, blocking_only_critical: bool = True
) -> tuple[ScriptDoc, bool, list[str]]:
    """Apply semantic verdicts to a copy of the script and report publishability."""
    by_id = {v["claim_id"]: v for v in verdicts}
    blocking: list[str] = []
    claims: list[Claim] = []
    for claim in script.claims:
        verdict = by_id.get(claim.claim_id)
        if verdict is None:
            updated = claim.model_copy(update={"review": "NOT_FOUND"})
        else:
            updated = claim.model_copy(update={"review": verdict["verdict"]})
        claims.append(updated)
        if claim.critical and updated.review != "SUPPORTED":
            blocking.append(claim.claim_id)
        elif not blocking_only_critical and updated.review == "CONTRADICTED":
            blocking.append(claim.claim_id)
    updated_script = script.model_copy(update={"claims": claims})
    return updated_script, not blocking, blocking


def non_critical_claims_to_drop(script: ScriptDoc) -> list[str]:
    return [c.claim_id for c in script.claims if not c.critical and c.review == "CONTRADICTED"]
