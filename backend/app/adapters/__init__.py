"""Adapter selection.

``PROVIDER_MODE=fake`` (default) builds deterministic local adapters so the pipeline,
the tests and the demo run without any API key. ``cloud`` builds the pinned provider set
from doc §3.1 and fails fast when a key is missing, because a half-configured provider is
worse than a stopped generation entry point (doc §13.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters.base import (
    AdapterError,
    EmbeddingAdapter,
    LLMAdapter,
    SearchAdapter,
    StorageAdapter,
    TTSAdapter,
)
from app.adapters.fake import FakeEmbedding, FakeLLM, FakeSearch, FakeTTS
from app.adapters.storage import LocalStorage, S3Storage
from app.config import Settings


@dataclass(slots=True)
class Adapters:
    llm: LLMAdapter
    embedding: EmbeddingAdapter
    search: SearchAdapter
    tts: TTSAdapter
    storage: StorageAdapter
    mode: str

    @property
    def embedding_profile(self) -> str:
        info = getattr(self.embedding, "info", None)
        if info is None:
            return "unknown"
        return f"{info.model}:{info.params.get('dim', 0)}"


def load_offline_corpus(fixtures_dir: Path) -> dict[str, tuple[str, str]]:
    corpus: dict[str, tuple[str, str]] = {}
    if not fixtures_dir.exists():
        return corpus
    for path in sorted(fixtures_dir.glob("*.md")):
        body = path.read_text("utf-8")
        title = path.stem
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        corpus[path.stem] = (title, body)
    return corpus


def build_adapters(settings: Settings, *, fixtures_dir: Path | None = None) -> Adapters:
    storage: StorageAdapter
    if settings.storage_backend == "s3":
        storage = S3Storage(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
        )
    else:
        storage = LocalStorage(settings.storage_dir)

    if settings.provider_mode == "fake":
        corpus = load_offline_corpus(fixtures_dir) if fixtures_dir else {}
        return Adapters(
            llm=FakeLLM(),
            embedding=FakeEmbedding(),
            search=FakeSearch(corpus),
            tts=FakeTTS(settings.audio_sample_rate),
            storage=storage,
            mode="fake",
        )

    missing = [
        name
        for name, value in (
            ("LLM_API_KEY", settings.llm_api_key),
            ("EMBEDDING_API_KEY", settings.embedding_api_key),
            ("TTS_API_KEY", settings.tts_api_key),
        )
        if not value
    ]
    if missing:
        raise AdapterError("provider_config_incomplete", ",".join(missing))

    from app.adapters.cloud import DashScopeEmbedding, DashScopeLLM, DashScopeTTS, TavilySearch

    return Adapters(
        llm=DashScopeLLM(settings),
        embedding=DashScopeEmbedding(settings),
        search=TavilySearch(settings),
        tts=DashScopeTTS(settings),
        storage=storage,
        mode="cloud",
    )
