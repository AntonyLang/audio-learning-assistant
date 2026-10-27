from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from app.desktop.config import DesktopConfig


class TTSManager:
    def __init__(self, config: DesktopConfig):
        self.config = config
        self.state = "stopped"
        self.error = ""
        self.lock = asyncio.Lock()
        self.start_task: asyncio.Task | None = None

    def status(self) -> dict:
        return {"state": self.state, "error": self.error, "model": "Qwen3-TTS 0.6B · Serena · INT8",
                "memory_limit_gib": 3, "cpu_limit": 1.5}

    async def docker(self, *arguments: str, timeout: float = 35) -> str:
        executable = shutil.which("docker")
        if not executable:
            raise ValueError("未找到 Docker Desktop。当前 TTS 镜像需要 Docker Desktop。")
        process = await asyncio.create_subprocess_exec(
            executable, *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("Docker 操作超时，请检查 Docker Desktop 状态。") from None
        if process.returncode:
            raise ValueError(stderr.decode("utf-8", errors="replace")[-900:])
        return stdout.decode("utf-8", errors="replace").strip()

    async def ensure_docker(self) -> None:
        try:
            await self.docker("info", "--format", "{{.ServerVersion}}", timeout=10)
            return
        except ValueError:
            desktop = Path("C:/Program Files/Docker/Docker/Docker Desktop.exe")
            if not desktop.exists():
                raise ValueError("请启动 Docker Desktop 后再点击启动 TTS。") from None
        subprocess.Popen([str(desktop)], creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(30):
            await asyncio.sleep(3)
            try:
                await self.docker("info", "--format", "{{.ServerVersion}}", timeout=5)
                return
            except ValueError:
                pass
        raise ValueError("Docker Desktop 尚未就绪，请稍后再点击启动 TTS。")

    async def start(self) -> None:
        async with self.lock:
            if self.state == "running":
                return
            self.state, self.error = "starting", ""
            try:
                if not (self.config.model_dir / "model.safetensors").exists():
                    raise ValueError("没有找到 Qwen 模型文件，请检查模型目录。")
                await self.ensure_docker()
                name = self.config.tts_container
                existing = await self.docker("ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}")
                if existing:
                    label = await self.docker("inspect", name, "--format", '{{index .Config.Labels "audio-learning.desktop"}}')
                    if label != "true":
                        raise ValueError("同名容器不属于本地学习助手，请修改本地 TTS 容器名。")
                    await self.docker("start", name)
                else:
                    await self.docker("image", "inspect", self.config.tts_image, "--format", "{{.Id}}")
                    await self.docker(
                        "run", "-d", "--name", name, "--label", "audio-learning.desktop=true",
                        "--read-only", "--tmpfs", "/tmp:size=128m,mode=1777", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges:true", "--pids-limit", "128",
                        "--memory", "3g", "--memory-swap", "3g", "--cpus", "1.5",
                        "-p", f"127.0.0.1:{self.config.tts_port}:8001",
                        "--mount", f"type=bind,source={self.config.model_dir},target=/models/qwen,readonly",
                        self.config.tts_image, "-d", "/models/qwen", "--int8", "-j", "2", "--serve", "8001",
                        "--max-request-seconds", "900", "--max-text-chars", "300",
                    )
                deadline = time.monotonic() + 240
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    while time.monotonic() < deadline:
                        try:
                            result = await client.get(self.config.tts_url + "/health")
                            if result.status_code == 200:
                                self.state = "running"
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(2)
                raise ValueError("TTS 模型加载超过 4 分钟，请查看 Docker 日志后重试。")
            except (ValueError, OSError) as exc:
                self.state, self.error = "error", str(exc)

    async def stop(self) -> None:
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        async with self.lock:
            self.state = "stopping"
            try:
                existing = await self.docker("ps", "--filter", f"name=^/{self.config.tts_container}$",
                                             "--filter", "label=audio-learning.desktop=true", "--format", "{{.Names}}")
                if existing:
                    await self.docker("stop", "--time", "3", self.config.tts_container)
                self.state, self.error = "stopped", ""
            except (ValueError, OSError) as exc:
                self.state, self.error = "error", str(exc)

    async def synthesize(self, text: str) -> bytes:
        if self.state != "running":
            raise ValueError("TTS 已停止，请在页面启动模型后继续。")
        async with httpx.AsyncClient(timeout=httpx.Timeout(960, connect=5), trust_env=False) as client:
            try:
                response = await client.post(self.config.tts_url + "/audio/speech", json={
                    "model": "qwen3-tts-0.6b-customvoice-int8-ef339be5", "input": text,
                    "voice": "serena", "language": "Chinese", "response_format": "wav",
                    "temperature": 0.9, "top_k": 50, "top_p": 1.0, "rep_penalty": 1.05, "seed": 42,
                })
            except httpx.HTTPError as exc:
                self.state = "error"
                self.error = "TTS 服务连接中断，请重新启动模型后继续；已完成的语音片段会保留。"
                raise ValueError(self.error) from exc
            if response.status_code >= 400:
                raise ValueError(f"TTS 生成失败（{response.status_code}）：{response.text[:300]}")
            return response.content


def segments(text: str, byte_limit: int = 240) -> list[str]:
    """The C server counts UTF-8 bytes, so split by bytes and prefer sentence boundaries."""
    import re

    text = re.sub(r"[`#*]", "", text).strip()
    result, buffer = [], ""
    for sentence in re.split(r"(?<=[。！？!?；;，,\n])", text):
        if not sentence.strip():
            continue
        if buffer and len((buffer + sentence).encode("utf-8")) > byte_limit:
            result.append(buffer.strip())
            buffer = ""
        for character in sentence:
            if len((buffer + character).encode("utf-8")) > byte_limit:
                result.append(buffer.strip())
                buffer = ""
            buffer += character
    if buffer.strip():
        result.append(buffer.strip())
    return result
