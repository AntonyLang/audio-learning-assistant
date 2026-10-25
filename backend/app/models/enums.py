"""Enumerated lifecycle values.

The document requires three independent lifecycles (doc §8.1): a source version's
extraction state, a run's scheduling state, and a revision's artifact state. They are
kept in separate columns so "task succeeded but nothing was published" is representable.
"""

from __future__ import annotations

from enum import StrEnum


class SourceKind(StrEnum):
    URL = "URL"
    TEXT = "TEXT"
    PDF = "PDF"
    RSS = "RSS"


class SourceVersionState(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    READY = "READY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class IndexState(StrEnum):
    PENDING = "PENDING"
    BUILDING = "BUILDING"
    READY = "READY"
    FAILED = "FAILED"


class Quality(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"


class RunKind(StrEnum):
    INGEST = "INGEST"
    GENERATE = "GENERATE"
    PUBLISH = "PUBLISH"


class RunState(StrEnum):
    DEFERRED = "DEFERRED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    WAITING_REVIEW = "WAITING_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Phase(StrEnum):
    INGEST = "INGEST"
    RESEARCH = "RESEARCH"
    SCRIPT = "SCRIPT"
    TTS = "TTS"
    AUDIO_CHECK = "AUDIO_CHECK"
    PUBLISH = "PUBLISH"


class RevisionStatus(StrEnum):
    BUILDING = "BUILDING"
    SCRIPT_READY = "SCRIPT_READY"
    AUDIO_READY = "AUDIO_READY"
    READY = "READY"
    BLOCKED = "BLOCKED"


class ArtifactKind(StrEnum):
    EVIDENCE = "evidence"
    SCRIPT = "script"
    AUDIO_SEGMENT = "audio_segment"
    AUDIO_FINAL = "audio_final"


class ProviderCallState(StrEnum):
    INTENT = "INTENT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ClaimReview(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    CONTRADICTED = "CONTRADICTED"
    NOT_FOUND = "NOT_FOUND"


class PlanItemState(StrEnum):
    RESERVED = "RESERVED"
    AVAILABLE = "AVAILABLE"
    DEFERRED = "DEFERRED"
    DROPPED = "DROPPED"


class FeedbackKind(StrEnum):
    SHALLOWER = "shallower"
    EXAMPLE = "example"
    DEEPER = "deeper"
    LESS_LIKE_THIS = "less_like_this"


class StopReason(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EVIDENCE_BLOCKED = "evidence_blocked"
    REVIEW_REQUIRED = "review_required"
    DEADLINE = "deadline"


#: Terminal states a retry is allowed to move away from.
RETRYABLE_RUN_STATES = frozenset({RunState.RETRY_WAIT})
#: States the dispatcher may pick up.
DISPATCHABLE_RUN_STATES = frozenset({RunState.QUEUED, RunState.RETRY_WAIT})
