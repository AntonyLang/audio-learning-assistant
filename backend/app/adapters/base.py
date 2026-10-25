"""Adapter contracts.

Every provider call is recorded with the *actual* provider, model name and parameters
that produced it (doc §3.1), so a later regression can be attributed to a model or
prompt change rather than guessed at.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ModelInfo:
    provider: str
    model: str
    kind: str
    returned_model: str | None = None
    prompt_version: str = ""
    params: dict = field(default_factory=dict)
    recorded_at: str = field(
        default_factory=lambda: datetime.now(UTC).date().isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "returned_model": self.returned_model,
            "prompt_version": self.prompt_version,
            "params": self.params,
            "recorded_at": self.recorded_at,
        }


@dataclass(slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    characters: int = 0
    searches: int = 0
    pages: int = 0

    def to_dict(self) -> dict:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "characters": self.characters,
            "searches": self.searches,
            "pages": self.pages,
        }

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            self.characters + other.characters,
            self.searches + other.searches,
            self.pages + other.pages,
        )


@dataclass(slots=True)
class LLMResponse:
    data: dict
    usage: Usage
    model: ModelInfo


@dataclass(slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    score: float = 0.0


@dataclass(slots=True)
class TTSResult:
    provider_request_id: str
    audio: bytes | None
    expires_at: datetime | None
    usage: Usage
    model: ModelInfo
    content_type: str = "audio/wav"
    audio_url: str | None = None


@dataclass(slots=True)
class FetchedPage:
    url: str
    status: int
    content_type: str
    body: bytes
    text: str
    title: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class StoredObject:
    key: str
    size: int
    sha256: str
    content_type: str


@runtime_checkable
class LLMAdapter(Protocol):
    async def complete_json(
        self, *, task: str, payload: dict, system: str = "", max_output_tokens: int | None = None
    ) -> LLMResponse: ...


@runtime_checkable
class EmbeddingAdapter(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class SearchAdapter(Protocol):
    async def search(self, query: str, *, max_results: int = 3) -> list[SearchResult]: ...


@runtime_checkable
class TTSAdapter(Protocol):
    async def synthesize(self, text: str, voice: str, language: str) -> TTSResult: ...

    async def download(self, result: TTSResult) -> bytes: ...

    async def download_url(self, url: str) -> bytes: ...


@runtime_checkable
class StorageAdapter(Protocol):
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> StoredObject: ...

    async def get_bytes(self, key: str) -> bytes: ...

    async def stat(self, key: str) -> StoredObject: ...

    async def read_range(self, key: str, start: int, end_inclusive: int) -> bytes: ...

    def stream(self, key: str, start: int, length: int) -> AsyncIterator[bytes]: ...


class AdapterError(RuntimeError):
    """A provider failure with a stable, loggable code."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.retryable = retryable


class ProviderTimeout(AdapterError):
    def __init__(self, provider: str) -> None:
        super().__init__("provider_timeout", provider, retryable=True)


class ProviderUnknown(AdapterError):
    """The request may or may not have been executed (doc §7.3)."""

    def __init__(self, provider: str, request_id: str | None = None) -> None:
        super().__init__("provider_unknown", f"{provider} request_id={request_id}")
        self.provider = provider
        self.request_id = request_id


def new_request_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
