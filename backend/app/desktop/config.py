from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class DesktopConfig:
    root: Path = ROOT
    data: Path = field(default_factory=lambda: ROOT / "_data/desktop")
    port: int = 18743
    tts_port: int = 18744
    tts_image: str = "audio-learning-qwen-native:ef339be5-cpu1"
    tts_container: str = "ala-desktop-tts"
    model_dir: Path = field(default_factory=lambda: ROOT / "deploy/.private/models/qwen3-tts-0.6b-customvoice")
    cpa_url: str = "http://127.0.0.1:8317/v1"
    model: str = "deepseek-v4.1-flash-1017"
    cpa_config: Path = field(default_factory=lambda: Path.home() / ".pi/agent/models.json")

    @property
    def tts_url(self) -> str:
        return f"http://127.0.0.1:{self.tts_port}/v1"

    @property
    def settings_path(self) -> Path:
        return self.data / "settings.json"

    def load(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        if self.settings_path.exists():
            settings = json.loads(self.settings_path.read_text("utf-8"))
            self.model = settings.get("model", self.model)

    def set_model(self, model: str) -> None:
        self.model = model
        self.settings_path.write_text(json.dumps({"model": model}, ensure_ascii=False), "utf-8")

    def cpa_key(self) -> str:
        if key := os.environ.get("CPA_API_KEY"):
            return key
        if self.cpa_config.exists():
            return json.loads(self.cpa_config.read_text("utf-8"))["providers"]["cpa"]["apiKey"]
        return ""
