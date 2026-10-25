"""ORM tables.

Constraints mirror doc §10: identity/state/ordering columns are explicit, large
structured products live in JSONB, and cross-row rules (budget, publication, run
uniqueness) are enforced by unique indexes plus transactions rather than by
single-row CHECKs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import (
    ArtifactKind,
    ClaimReview,
    FeedbackKind,
    IndexState,
    Phase,
    PlanItemState,
    ProviderCallState,
    Quality,
    RevisionStatus,
    RunKind,
    RunState,
    SourceKind,
    SourceVersionState,
)


def enum_column(python_enum: type, length: int = 32) -> Enum:
    return Enum(
        python_enum,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class UserProfile(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "user_profiles"

    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    prefs: Mapped[dict] = mapped_column(JSONB, default=dict)
    prefs_version: Mapped[int] = mapped_column(Integer, default=1)
    next_due_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Session(UUIDPrimaryKey, Base):
    __tablename__ = "sessions"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Source(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "sources"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind, 16))
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, default="")
    intent_note: Mapped[str] = mapped_column(Text, default="")
    parent_source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0)
    input_ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceVersion(UUIDPrimaryKey, Base):
    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "content_hash", "extractor_version", name="uq_source_version_identity"
        ),
        CheckConstraint("char_count >= 0", name="char_count_non_negative"),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    extractor_version: Mapped[str] = mapped_column(String(40))
    raw_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    quality: Mapped[Quality] = mapped_column(enum_column(Quality, 16), default=Quality.FULL)
    state: Mapped[SourceVersionState] = mapped_column(
        enum_column(SourceVersionState, 16), default=SourceVersionState.RECEIVED
    )
    index_state: Mapped[IndexState] = mapped_column(
        enum_column(IndexState, 16), default=IndexState.PENDING
    )
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    chunks: Mapped[list[Chunk]] = relationship(back_populates="source_version")


class Chunk(UUIDPrimaryKey, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("source_version_id", "ordinal", name="uq_chunk_ordinal"),
        CheckConstraint("end_char > start_char", name="chunk_span_positive"),
        Index("ix_chunk_scope", "owner_id", "source_version_id"),
        Index("ix_chunk_fts", "lexemes_tsv", postgresql_using="gin"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_versions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[str] = mapped_column(Text, default="")
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_id: Mapped[str] = mapped_column(String(40), default="")
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    lexemes: Mapped[str] = mapped_column(Text, default="")
    lexemes_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(lexemes, ''))", persisted=True),
        nullable=True,
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_profile: Mapped[str] = mapped_column(String(80), default="")
    content_hash: Mapped[str] = mapped_column(String(64))

    source_version: Mapped[SourceVersion] = relationship(back_populates="chunks")


class DailyPlan(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "daily_plans"
    __table_args__ = (
        UniqueConstraint("owner_id", "local_date", name="uq_daily_plan"),
        CheckConstraint("budget_ms > 0", name="budget_positive"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    local_date: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64))
    budget_ms: Mapped[int] = mapped_column(BigInteger)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    prefs_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class PlanItem(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "episode_id", name="uq_plan_episode"),
        Index("ix_plan_item_state", "plan_id", "state"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("daily_plans.id", ondelete="CASCADE"))
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[PlanItemState] = mapped_column(
        enum_column(PlanItemState, 16), default=PlanItemState.RESERVED
    )
    reserved_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Episode(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "episodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["id", "published_revision_id"],
            ["episode_revisions.episode_id", "episode_revisions.id"],
            name="fk_episodes_published_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "published_at IS NULL OR published_revision_id IS NOT NULL",
            name="published_at_requires_revision",
        ),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    primary_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    question: Mapped[str] = mapped_column(Text, default="")
    published_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(nullable=True)
    guid: Mapped[str] = mapped_column(String(64), unique=True)
    supersedes_episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )


class EpisodeRevision(UUIDPrimaryKey, Base):
    __tablename__ = "episode_revisions"
    __table_args__ = (
        UniqueConstraint("episode_id", "revision_no", name="uq_episode_revision"),
        UniqueConstraint("episode_id", "id", name="uq_episode_revision_identity"),
    )

    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[RevisionStatus] = mapped_column(
        enum_column(RevisionStatus, 16), default=RevisionStatus.BUILDING
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stage_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    script_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stage_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    audio_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stage_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class StageArtifact(UUIDPrimaryKey, Base):
    __tablename__ = "stage_artifacts"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "input_fingerprint", name="uq_artifact_input"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    kind: Mapped[ArtifactKind] = mapped_column(enum_column(ArtifactKind, 24))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class GenerationRun(UUIDPrimaryKey, Base):
    __tablename__ = "generation_runs"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "request_key", name="uq_run_request"),
        Index(
            "uq_generate_revision",
            "revision_id",
            unique=True,
            postgresql_where=text("kind = 'GENERATE'"),
        ),
        Index("ix_run_due", "state", "next_attempt_at"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    kind: Mapped[RunKind] = mapped_column(enum_column(RunKind, 16))
    request_key: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("episode_revisions.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[RunState] = mapped_column(enum_column(RunState, 16), default=RunState.QUEUED)
    phase: Mapped[Phase | None] = mapped_column(enum_column(Phase, 16), nullable=True)
    active_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run_attempts.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    event_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    budget_usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    deadline_at: Mapped[datetime | None] = mapped_column(nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )


class RunAttempt(UUIDPrimaryKey, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt_no"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_runs.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(24), default="ACTIVE")
    lease_expires_at: Mapped[datetime] = mapped_column()
    started_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ProviderCall(UUIDPrimaryKey, Base):
    __tablename__ = "provider_calls"
    __table_args__ = (UniqueConstraint("run_id", "call_seq", name="uq_provider_call_seq"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_runs.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32), default="")
    call_seq: Mapped[int] = mapped_column(Integer)
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[ProviderCallState] = mapped_column(
        enum_column(ProviderCallState, 16), default=ProviderCallState.INTENT
    )
    request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    usage: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_event_created", "run_id", "created_at"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class FeedbackEvent(UUIDPrimaryKey, Base):
    __tablename__ = "feedback_events"
    __table_args__ = (
        UniqueConstraint("owner_id", "client_event_id", name="uq_feedback_client_event"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    kind: Mapped[FeedbackKind] = mapped_column(enum_column(FeedbackKind, 24))
    note: Mapped[str] = mapped_column(Text, default="")
    parent_claim_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_event_id: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class PlaybackEvent(UUIDPrimaryKey, Base):
    __tablename__ = "playback_events"
    __table_args__ = (UniqueConstraint("session_id", "client_seq", name="uq_playback_seq"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    episode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"))
    session_id: Mapped[str] = mapped_column(String(80))
    client_seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(24), default="progress")
    start_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    end_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class FeedToken(UUIDPrimaryKey, Base):
    __tablename__ = "feed_tokens"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class IdempotencyKey(UUIDPrimaryKey, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("owner_id", "endpoint", "key", name="uq_idempotency_key"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user_profiles.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer, default=202)
    response_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


__all__ = [
    "ArtifactKind",
    "Chunk",
    "ClaimReview",
    "DailyPlan",
    "Episode",
    "EpisodeRevision",
    "FeedbackEvent",
    "FeedToken",
    "GenerationRun",
    "IdempotencyKey",
    "PlanItem",
    "PlaybackEvent",
    "ProviderCall",
    "RunAttempt",
    "RunEvent",
    "Session",
    "Source",
    "SourceVersion",
    "StageArtifact",
    "UserProfile",
]
