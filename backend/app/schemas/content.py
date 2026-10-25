"""Content contracts shared by the graph, the validators and the API.

These models are the "data contract" of doc §6: a claim is only publishable when every
evidence reference resolves to an immutable ``source_version``/``chunk`` and the quoted
span can be relocated in that version's stored text.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import ClaimReview


def utcnow() -> datetime:
    return datetime.now(UTC)


class EvidenceOrigin(StrEnum):
    PRIMARY = "primary"
    EXTERNAL = "external"
    HISTORY = "history"


class Relation(StrEnum):
    SUPPORTS = "supports"
    CONTRASTS = "contrasts"
    DEFINES = "defines"


class ClaimKind(StrEnum):
    EXTERNAL_FACT = "external_fact"
    INTERNAL_FACT = "internal_fact"
    EXAMPLE = "example"
    INFERENCE = "inference"
    RECOMMENDATION = "recommendation"


class EvidenceRef(BaseModel):
    """One located quotation inside an immutable source version."""

    source_version_id: uuid.UUID
    chunk_id: uuid.UUID
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=4000)
    relation: Relation = Relation.SUPPORTS

    @model_validator(mode="after")
    def _span_is_forward(self) -> EvidenceRef:
        if self.span_end <= self.span_start:
            raise ValueError("span_end must be greater than span_start")
        return self


class EvidenceItem(EvidenceRef):
    """An evidence reference plus why it was retrieved."""

    origin: EvidenceOrigin = EvidenceOrigin.PRIMARY
    retrieved_for: str = ""
    source_title: str = ""
    fetched_at: datetime | None = None


class EvidencePack(BaseModel):
    pack_id: str
    primary_version_id: uuid.UUID
    core_question: str = ""
    items: list[EvidenceItem]
    gaps: list[dict] = Field(default_factory=list)
    unresolved: list[dict] = Field(default_factory=list)
    queries_used: int = 0
    pages_fetched: int = 0
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def critical_shortfall(self) -> bool:
        return not self.items


class Claim(BaseModel):
    claim_id: str
    text: str
    kind: ClaimKind = ClaimKind.EXTERNAL_FACT
    critical: bool = True
    evidence: list[EvidenceRef] = Field(default_factory=list)
    review: ClaimReview = ClaimReview.NOT_FOUND

    @field_validator("claim_id")
    @classmethod
    def _claim_id_shape(cls, value: str) -> str:
        if not value or len(value) > 40:
            raise ValueError("claim_id must be 1..40 characters")
        return value

    @model_validator(mode="after")
    def _critical_claims_need_evidence(self) -> Claim:
        if self.critical and self.kind in (ClaimKind.EXTERNAL_FACT, ClaimKind.INTERNAL_FACT):
            if not self.evidence:
                raise ValueError(f"critical claim {self.claim_id} has no evidence reference")
        return self


class ScriptSegment(BaseModel):
    segment_id: str
    heading: str = ""
    spoken_text: str
    claim_ids: list[str] = Field(default_factory=list)


class ScriptDoc(BaseModel):
    episode_id: uuid.UUID
    revision_id: uuid.UUID
    core_question: str
    one_line_answer: str
    segments: list[ScriptSegment]
    claims: list[Claim]
    spoken_text: str
    char_count: int
    revision_no: int = 1
    prompt_version: str = ""
    model: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _claims_are_referenced(self) -> ScriptDoc:
        known = {c.claim_id for c in self.claims}
        for segment in self.segments:
            for claim_id in segment.claim_ids:
                if claim_id not in known:
                    raise ValueError(f"segment {segment.segment_id} references unknown claim {claim_id}")
        return self


class ClaimVerdict(BaseModel):
    claim_id: str
    verdict: ClaimReview
    reason: str = ""


class ReviewReport(BaseModel):
    verdicts: list[ClaimVerdict]
    critical_ok: bool
    blocking_claims: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utcnow)
    model: str = ""


class Gap(BaseModel):
    gap_id: str
    question: str
    why_needed: str = ""
    domain_hints: list[str] = Field(default_factory=list)


class GapPlan(BaseModel):
    gaps: list[Gap] = Field(default_factory=list)
    rationale: str = ""

    @model_validator(mode="after")
    def _bounded(self) -> GapPlan:
        if len(self.gaps) > 2:
            raise ValueError("a gap plan may declare at most two gaps")
        return self


class FocusPoint(BaseModel):
    point: str
    why: str = ""
    #: Set by the focus node when the point is grounded in retrieved document text;
    #: ``None`` means "unknown" (a cloud model answer) rather than "grounded".
    matched: bool | None = None


class FocusResult(BaseModel):
    core_question: str
    critical_points: list[FocusPoint] = Field(default_factory=list)
    document_mode: Literal["full", "outline"] = "full"
    rationale: str = ""


class RetrievalHit(BaseModel):
    chunk_id: uuid.UUID
    source_version_id: uuid.UUID
    score: float
    channels: list[str] = Field(default_factory=list)
    rank_keyword: int | None = None
    rank_semantic: int | None = None


class ScriptRequest(BaseModel):
    """Input to the drafting node; kept free of ORM objects so it can be logged."""

    core_question: str
    one_line_answer_hint: str = ""
    critical_points: list[str] = Field(default_factory=list)
    preference_snapshot: dict = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    target_seconds: int = 240
    revision_no: int = 1
    previous_script: ScriptDoc | None = None
