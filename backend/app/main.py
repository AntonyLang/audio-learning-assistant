"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, episodes, feeds, playback, sources, today
from app.api import runs as runs_api
from app.config import get_settings
from app.db import SessionFactory, dispose_engine
from app.jobs.dispatch import get_dispatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with SessionFactory() as session:
        await auth.ensure_demo_user(session, settings)
    logger.info(
        "started provider_mode=%s storage=%s inline_worker=%s timezone=%s",
        settings.provider_mode,
        settings.storage_backend,
        settings.inline_worker,
        settings.app_timezone,
    )
    yield
    await dispose_engine()


app = FastAPI(
    title="Personal Audio Learning Assistant",
    version="0.1.0",
    description="Turn a reading list into sourced, duration-bounded Chinese micro-lessons.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(sources.router)
app.include_router(episodes.router)
app.include_router(runs_api.router)
app.include_router(today.router)
app.include_router(feeds.router)
app.include_router(playback.router)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "provider_mode": settings.provider_mode,
        "max_episode_ms": settings.max_episode_ms,
        "inline_worker": settings.inline_worker,
        "dispatcher": type(get_dispatcher()).__name__,
    }


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
