"""Runtime configuration.

Every model/provider choice is recorded per call (see ``app/adapters/base.py``) so a
regression can be traced back to "which model actually answered on which date".
Environment values here are defaults from the architecture document, §13.2.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env", "../.env.local", ".env.local"), extra="ignore")

    # --- deployment -------------------------------------------------------
    app_env: str = "dev"
    app_timezone: str = "Asia/Shanghai"
    database_url: str = "postgresql+asyncpg://ala:ala@localhost:55432/ala"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "dev-only-change-me"
    session_cookie: str = "ala_session"
    session_ttl_seconds: int = 14 * 24 * 3600
    cookie_secure: bool = False
    public_base_url: str = "http://127.0.0.1:8000"

    # --- providers --------------------------------------------------------
    # "fake" runs the whole pipeline with deterministic local adapters.
    provider_mode: Literal["fake", "cloud"] = "fake"
    dashscope_api_key: str = ""
    llm_provider: str = "dashscope"
    llm_model: str = "qwen-plus"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_thinking: Literal["disabled", "default"] = "disabled"
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    tts_model: str = "qwen3-tts-flash"
    tts_provider: Literal["dashscope", "openai", "qwen-native"] = "dashscope"
    tts_timeout_seconds: float = 120.0
    tts_voice: str = "Cherry"
    tts_language: str = "Chinese"
    tts_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    tts_api_key: str = ""
    tavily_api_key: str = ""
    search_max_results: int = 3

    # --- storage ----------------------------------------------------------
    storage_backend: str = "local"  # local | s3
    storage_dir: Path = Field(default=Path("./_data/objects"))
    work_dir: Path = Field(default=Path("./_data/work"))
    #: Offline web used by the deterministic adapters (`local://` pages). Absolute so it
    #: resolves the same from the repo root, from `backend/`, and inside a container.
    offline_fixtures_dir: Path = Field(default=REPO_ROOT / "evals" / "fixtures" / "web")
    s3_endpoint: str = ""
    s3_bucket: str = "audio-learning"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # --- ingestion limits (doc §4.1) --------------------------------------
    max_pdf_bytes: int = 20 * 1024 * 1024
    max_pdf_pages: int = 100
    max_html_bytes: int = 2 * 1024 * 1024
    fetch_timeout_seconds: float = 20.0
    max_redirects: int = 4
    allow_private_hosts: bool = False

    # --- chunking (doc §4.2) ----------------------------------------------
    chunk_min_chars: int = 400
    chunk_max_chars: int = 700
    chunk_overlap_chars: int = 80
    document_budget_chars: int = 12000
    evidence_budget_chars: int = 4000

    # --- budget (doc §5.2) ------------------------------------------------
    max_searches_per_episode: int = 2
    max_pages_per_episode: int = 4
    max_fact_revisions: int = 1
    #: "llm" runs the semantic citation check; "structural" skips it (evaluation baseline C).
    verify_mode: str = "llm"
    max_audio_revisions: int = 1
    llm_token_budget: int = 24000
    llm_output_token_cap: int = 6000
    max_tavily_results: int = 3

    # --- audio (doc §7) ---------------------------------------------------
    max_segment_chars: int = 400
    max_episode_ms: int = 300_000
    target_episode_ms: int = 270_000
    audio_sample_rate: int = 24_000
    audio_bitrate: str = "64k"
    tts_concurrency: int = 1
    #: Calibration starting point for script length -> spoken seconds (~4.2 Chinese chars/s).
    spoken_chars_per_second: float = 4.2

    # --- planning / jobs (doc §8, §9) -------------------------------------
    daily_budget_ms: int = 600_000
    episode_reservation_ms: int = 300_000
    max_episodes_per_plan: int = 2
    max_attempts: int = 4
    retry_backoff_seconds: tuple[int, ...] = (15, 60, 180)
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    dispatcher_interval_seconds: int = 5
    task_soft_time_limit: int = 540
    task_time_limit: int = 600
    visibility_timeout: int = 900
    event_retention_days: int = 14
    sse_poll_seconds: float = 1.0

    # --- demo bootstrap ---------------------------------------------------
    seed_demo_user: bool = True
    demo_email: str = "demo@local"
    demo_password: str = "demo12345"
    bootstrap_email: str = ""
    bootstrap_password: str = ""

    # --- dev conveniences -------------------------------------------------
    # When true the API executes queued runs inline instead of publishing to Celery.
    # Keeps `docker compose` optional for a local demo; production uses the worker.
    inline_worker: bool = False

    @model_validator(mode="after")
    def configure_providers(self) -> Settings:
        self.llm_api_key = self.llm_api_key or self.dashscope_api_key
        self.embedding_api_key = self.embedding_api_key or self.dashscope_api_key
        if self.tts_provider == "dashscope":
            self.tts_api_key = self.tts_api_key or self.dashscope_api_key
        if self.app_env == "production":
            if self.seed_demo_user:
                raise ValueError("生产环境必须设置 SEED_DEMO_USER=false")
            if not self.cookie_secure or not self.public_base_url.startswith("https://"):
                raise ValueError("生产环境必须使用 HTTPS 和 Secure Cookie")
            if self.bootstrap_password and len(self.bootstrap_password) < 16:
                raise ValueError("初始密码至少需要 16 个字符")
        return self

    @property
    def max_episode_seconds(self) -> float:
        return self.max_episode_ms / 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
