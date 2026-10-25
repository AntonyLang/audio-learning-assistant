from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ala:ala@localhost:55432/ala_test"
)
_workdir = Path(tempfile.mkdtemp(prefix="ala-test-"))

# Configure before any app module imports ``get_settings()``.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["PROVIDER_MODE"] = "fake"
os.environ["INLINE_WORKER"] = "false"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["STORAGE_DIR"] = str(_workdir / "objects")
os.environ["WORK_DIR"] = str(_workdir / "work")
os.environ["SEED_DEMO_USER"] = "true"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["COOKIE_SECURE"] = "false"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionFactory, dispose_engine, engine  # noqa: E402
from app.models import Base, UserProfile  # noqa: E402
from app.security import hash_password  # noqa: E402


async def _reset_schema() -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


async def _truncate_all() -> None:
    tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session", autouse=True)
async def schema():
    await _reset_schema()
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
async def clean_db(schema):
    await _truncate_all()
    yield


@pytest.fixture
async def owner() -> UserProfile:
    async with SessionFactory() as session:
        profile = UserProfile(
            email="owner@test",
            password_hash=hash_password("pw-123456"),
            display_name="Owner",
            timezone="Asia/Shanghai",
            prefs={"depth": "normal", "daily_budget_ms": 600_000, "goals": []},
            prefs_version=1,
        )
        session.add(profile)
        await session.commit()
        return profile


@pytest.fixture
async def session():
    async with SessionFactory() as db_session:
        yield db_session


@pytest.fixture
async def adapters():
    from app.adapters import build_adapters
    from app.config import get_settings

    settings = get_settings()
    return build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
