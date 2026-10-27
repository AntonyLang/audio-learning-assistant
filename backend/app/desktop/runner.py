from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

from app.desktop.config import DesktopConfig
from app.desktop.documents import Documents
from app.desktop.research import Researcher
from app.desktop.store import Store
from app.desktop.tts import TTSManager, segments


def assemble(paths: list[Path], output: Path) -> float:
    """Join PCM WAV parts using the stdlib; the final compressed file uses existing FFmpeg."""
    temporary = output.with_suffix(".wav")
    with wave.open(str(paths[0]), "rb") as first:
        parameters = first.getparams()
    with wave.open(str(temporary), "wb") as target:
        target.setparams(parameters)
        for path in paths:
            with wave.open(str(path), "rb") as part:
                if (part.getnchannels(), part.getsampwidth(), part.getframerate()) != (
                        parameters.nchannels, parameters.sampwidth, parameters.framerate):
                    raise ValueError("语音片段格式不一致，无法拼接。")
                target.writeframes(part.readframes(part.getnframes()))
    with wave.open(str(temporary), "rb") as audio:
        seconds = audio.getnframes() / audio.getframerate()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到本机 FFmpeg，无法输出 MP3。")
    result = subprocess.run([ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(temporary),
                             "-codec:a", "libmp3lame", "-b:a", "64k", str(output)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=120)
    if result.returncode:
        raise ValueError("音频拼接失败：" + result.stderr.decode("utf-8", errors="replace")[-400:])
    temporary.unlink()
    return seconds


class Runner:
    def __init__(self, config: DesktopConfig, store: Store, tts: TTSManager):
        self.config, self.store, self.tts = config, store, tts
        self.documents = Documents(config)
        self.researcher = Researcher(config, store, self.documents)
        self.wake = asyncio.Event()
        self.current: str | None = None
        self.current_task: asyncio.Task | None = None

    async def loop(self) -> None:
        while True:
            self.wake.clear()
            pending = self.store.queued()
            if not pending:
                await self.wake.wait()
                continue
            self.current = pending[0]
            self.current_task = asyncio.create_task(self.process(self.current))
            try:
                await self.current_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.current, self.current_task = None, None

    async def pause_tts(self) -> None:
        if self.current and self.store.get(self.current)["status"] == "voicing":
            self.store.update(self.current, status="waiting_tts", stage="TTS 已停止，等待启动模型")
            self.current_task.cancel()
            await asyncio.gather(self.current_task, return_exceptions=True)

    async def cancel(self, entry_id: str) -> None:
        self.store.update(entry_id, status="cancelled", stage="已取消")
        if self.current == entry_id and self.current_task:
            self.current_task.cancel()
            await asyncio.gather(self.current_task, return_exceptions=True)

    def resume_waiting(self) -> None:
        for entry in self.store.list():
            if entry["status"] == "waiting_tts":
                self.store.update(entry["id"], status="queued", stage="等待语音生成", error="")
        self.wake.set()

    async def process(self, entry_id: str) -> None:
        try:
            entry = self.store.get(entry_id)
            if not entry["script"]:
                self.store.update(entry_id, status="parsing", stage="解析投递资料", error="", model=self.config.model)
                for source in self.store.sources(entry_id):
                    if source["status"] == "ready":
                        continue
                    self.store.event(entry_id, "解析：" + source["name"])
                    try:
                        if source["kind"] == "url":
                            _, text, _ = await self.documents.read_url(source["location"])
                        else:
                            text = await self.documents.parse_file(Path(source["location"]))
                        self.store.update_source(source["id"], text=text)
                    except Exception as exc:
                        self.store.update_source(source["id"], error=str(exc)[:700])
                        raise ValueError(f"资料“{source['name']}”解析失败：{exc}") from exc
                self.store.update(entry_id, status="researching", stage="CPA Agent 正在研究与撰写")
                self.store.event(entry_id, "开始研究：检索、阅读资料并组织讲稿")
                draft = await self.researcher.draft(self.store.get(entry_id))
                self.store.update(entry_id, title=draft["title"][:120], script=draft["script"],
                                  source_ids=json.dumps(draft["source_ids"]), summary=draft["summary"])
                self.store.event(entry_id, "讲稿已完成")
                entry = self.store.get(entry_id)
            if self.tts.state != "running":
                self.store.update(entry_id, status="waiting_tts", stage="讲稿已就绪，等待启动 TTS")
                return
            parts = segments(entry["script"])
            self.store.update(entry_id, status="voicing", stage="正在生成语音", segment_total=len(parts), segment_done=0)
            cache = self.config.data / "cache/tts"
            cache.mkdir(parents=True, exist_ok=True)
            paths = []
            for index, text in enumerate(parts):
                fingerprint = hashlib.sha256(("qwen-ef339be5-cpu1-serena-42\0" + text).encode()).hexdigest()
                path = cache / (fingerprint + ".wav")
                if not path.exists():
                    raw = await self.tts.synthesize(text)
                    # Validate the response before making it a reusable segment.
                    import io
                    with wave.open(io.BytesIO(raw), "rb") as audio:
                        if audio.getnframes() == 0:
                            raise ValueError("TTS 返回了空音频。")
                    await asyncio.to_thread(path.write_bytes, raw)
                paths.append(path)
                self.store.update(entry_id, segment_done=index + 1, stage=f"生成语音 {index + 1}/{len(parts)} 段")
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", entry["title"]).strip(". ")[:60] or "audio"
            folder = self.config.data / "audio" / (stem + "_" + entry_id[:8])
            folder.mkdir(parents=True, exist_ok=True)
            output = folder / (stem + ".mp3")
            seconds = await asyncio.to_thread(assemble, paths, output)
            (folder / "讲稿.txt").write_text(entry["script"], "utf-8")
            (folder / "来源.json").write_text(json.dumps([
                {"id": source["id"], "name": source["name"], "location": source["location"]}
                for source in self.store.sources(entry_id)], ensure_ascii=False, indent=2), "utf-8")
            self.store.update(entry_id, status="completed", stage="已完成", audio_path=str(output), duration_seconds=seconds)
            self.store.event(entry_id, "音频已保存到本机")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.update(entry_id, status="failed", stage="生成中断，可继续", error=str(exc)[:1200])
            self.store.event(entry_id, "错误：" + str(exc)[:500])
