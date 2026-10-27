from __future__ import annotations

import asyncio
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.desktop.config import DesktopConfig
from app.desktop.documents import FORMATS
from app.desktop.research import CPA
from app.desktop.runner import Runner
from app.desktop.store import Store
from app.desktop.tts import TTSManager


class ModelSelection(BaseModel):
    model: str = Field(min_length=1, max_length=200)


def create_app(config: DesktopConfig | None = None, *, autostart_tts: bool = True) -> FastAPI:
    config = config or DesktopConfig()
    config.load()
    store = Store(config.data)
    tts = TTSManager(config)
    runner = Runner(config, store, tts)
    cpa = CPA(config)
    cpa_status = {"connected": False, "models": [], "error": "正在连接", "checked": 0.0}

    async def start_tts():
        await tts.start()
        if tts.state == "running":
            runner.resume_waiting()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = asyncio.create_task(runner.loop())
        if autostart_tts:
            tts.start_task = asyncio.create_task(start_tts())
        runner.wake.set()
        yield
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await tts.stop()

    app = FastAPI(title="音频学习助手 · 本地版", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.config, app.state.store, app.state.tts, app.state.runner = config, store, tts, runner

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.path.startswith("/local/api/") and request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-local-app") != "1" or (origin and origin != str(request.base_url).rstrip("/")):
                from starlette.responses import JSONResponse
                return JSONResponse({"detail": "此操作仅允许本地应用页面发起。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/local/") else "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    def require_entry(entry_id: str) -> dict:
        entry = store.get(entry_id)
        if not entry:
            raise HTTPException(404, "条目不存在。")
        return entry

    @app.get("/local/api/health")
    async def health():
        return {"app": "audio-learning-desktop", "ok": True, "port": config.port}

    @app.get("/local/api/status")
    async def status():
        if time.monotonic() - cpa_status["checked"] > 10:
            try:
                cpa_status.update(connected=True, models=await cpa.models(), error="")
            except (httpx.HTTPError, OSError, KeyError) as exc:
                cpa_status.update(connected=False, models=[], error="请手动启动 CPA" if isinstance(exc, httpx.ConnectError) else str(exc)[:180])
            cpa_status["checked"] = time.monotonic()
        return {"tts": tts.status(), "cpa": {key: value for key, value in cpa_status.items() if key != "checked"},
                "model": config.model, "cpa_url": config.cpa_url, "active_id": runner.current,
                "data_dir": str(config.data), "formats": sorted(FORMATS)}

    @app.put("/local/api/model")
    async def set_model(selection: ModelSelection):
        models = await cpa.models()
        if selection.model not in models:
            raise HTTPException(400, "CPA 中没有此模型。")
        config.set_model(selection.model)
        return {"model": config.model}

    @app.post("/local/api/tts/start", status_code=202)
    async def tts_start():
        if tts.state not in {"running", "starting"}:
            tts.state, tts.error = "starting", ""
            tts.start_task = asyncio.create_task(start_tts())
        return tts.status()

    @app.post("/local/api/tts/stop")
    async def tts_stop():
        await runner.pause_tts()
        await tts.stop()
        return tts.status()

    @app.get("/local/api/entries")
    async def list_entries():
        return store.list()

    @app.post("/local/api/entries", status_code=201)
    async def create_entry(
        title: Annotated[str, Form(max_length=120)] = "",
        instructions: Annotated[str, Form(max_length=5000)] = "",
        text: Annotated[str, Form(max_length=300_000)] = "",
        urls: Annotated[str, Form(max_length=20_000)] = "",
        target_seconds: Annotated[int, Form(ge=15, le=600)] = 180,
        web_enabled: Annotated[bool, Form()] = True,
        files: Annotated[list[UploadFile] | None, File()] = None,
    ):
        uploads = files or []
        links = [line.strip() for line in urls.splitlines() if line.strip()]
        if not text.strip() and not uploads and not links:
            raise HTTPException(400, "请至少投递一份文本、文件或链接。")
        if len(uploads) > 20 or len(links) > 20:
            raise HTTPException(400, "每个条目最多 20 个文件与 20 个链接。")
        for upload in uploads:
            if Path(upload.filename or "").suffix.lower() not in FORMATS:
                raise HTTPException(400, f"不支持文件格式：{upload.filename}")
        if any(not link.startswith(("http://", "https://")) for link in links):
            raise HTTPException(400, "链接必须以 http:// 或 https:// 开头，每行一个。")
        title = title.strip() or (uploads[0].filename if uploads else links[0] if links else text[:30])
        entry_id = store.create(title[:120], instructions.strip(), target_seconds, web_enabled)
        folder = config.data / "uploads" / entry_id
        folder.mkdir(parents=True)
        try:
            if text.strip():
                store.add_source(entry_id, "投递文本", "text", "", text=text.strip())
            for link in links:
                store.add_source(entry_id, link, "url", link)
            for index, upload in enumerate(uploads):
                extension = Path(upload.filename).suffix.lower()
                path = folder / f"{index + 1}{extension}"
                size = 0
                with path.open("wb") as target:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > 30 * 1024 * 1024:
                            raise ValueError(f"{upload.filename} 超过 30MB。")
                        target.write(chunk)
                store.add_source(entry_id, upload.filename, "file", str(path))
            store.update(entry_id, status="queued", stage="等待处理")
            store.event(entry_id, f"已投递 {len(store.sources(entry_id))} 份资料")
            runner.wake.set()
            return require_entry(entry_id)
        except Exception as exc:
            store.update(entry_id, status="failed", stage="投递失败", error=str(exc))
            raise HTTPException(400, str(exc)) from exc

    @app.get("/local/api/entries/{entry_id}")
    async def detail(entry_id: str):
        entry = require_entry(entry_id)
        entry["sources"] = [{**source, "chars": len(source["text"]), "text": source["text"][:1800]}
                            for source in store.sources(entry_id)]
        entry["events"] = store.events(entry_id)
        return entry

    @app.post("/local/api/entries/{entry_id}/retry")
    async def retry(entry_id: str):
        entry = require_entry(entry_id)
        if entry["status"] not in {"failed", "interrupted", "waiting_tts", "cancelled"}:
            raise HTTPException(409, "此条目正在处理或已经完成。")
        store.update(entry_id, status="queued", stage="等待继续", error="")
        runner.wake.set()
        return {"ok": True}

    @app.post("/local/api/entries/{entry_id}/cancel")
    async def cancel(entry_id: str):
        entry = require_entry(entry_id)
        if entry["status"] == "completed":
            raise HTTPException(409, "音频已经完成。")
        await runner.cancel(entry_id)
        return {"ok": True}

    @app.get("/local/api/entries/{entry_id}/audio")
    async def audio(entry_id: str):
        entry = require_entry(entry_id)
        if entry["status"] != "completed" or not Path(entry["audio_path"]).is_file():
            raise HTTPException(404, "音频尚未生成。")
        return FileResponse(entry["audio_path"], media_type="audio/mpeg", filename=Path(entry["audio_path"]).name,
                            content_disposition_type="inline")

    @app.post("/local/api/entries/{entry_id}/reveal")
    async def reveal(entry_id: str):
        entry = require_entry(entry_id)
        if entry["status"] != "completed" or not Path(entry["audio_path"]).is_file():
            raise HTTPException(404, "音频文件不存在。")
        subprocess.Popen(["explorer.exe", "/select,", entry["audio_path"]])
        return {"path": entry["audio_path"]}

    @app.post("/local/api/shutdown")
    async def shutdown(background: BackgroundTasks):
        async def stop_server():
            app.state.server.should_exit = True
        background.add_task(stop_server)
        return {"ok": True}

    @app.get("/")
    async def root():
        return RedirectResponse("/desktop/")

    dist = config.root / "apps/web/dist"
    if dist.exists():
        app.mount("/desktop", StaticFiles(directory=dist, html=True), name="desktop")
    return app
