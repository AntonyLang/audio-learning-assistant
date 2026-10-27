"""Private CPU-only Chinese TTS service. No public port or cloud credentials."""

import io
import os
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from qwen_tts import Qwen3TTSModel

MODEL_ID = "qwen3-tts-0.6b-customvoice"
model = None
lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    root = Path(os.environ.get("MODEL_DIR", "/models/qwen"))
    torch.set_num_threads(int(os.environ.get("TTS_CPU_THREADS", "2")))
    torch.set_num_interop_threads(1)
    model = Qwen3TTSModel.from_pretrained(
        str(root), device_map="cpu", dtype=torch.bfloat16,
        attn_implementation="eager", local_files_only=True,
    )
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Speech(BaseModel):
    model: str = MODEL_ID
    input: str = Field(min_length=1, max_length=300)
    voice: str = "Serena"
    response_format: str = "wav"


@app.get("/healthz")
def health():
    return {"ok": model is not None, "model": MODEL_ID, "provider": "cpu"}


@app.post("/v1/audio/speech")
def speech(body: Speech):
    if body.model != MODEL_ID or body.response_format != "wav":
        raise HTTPException(400, "unsupported_model_or_format")
    if body.voice.lower() not in {s.lower() for s in model.get_supported_speakers()}:
        raise HTTPException(400, "unknown_voice")
    if not lock.acquire(blocking=False):
        raise HTTPException(429, "speech_worker_busy")
    try:
        start = time.monotonic()
        with torch.inference_mode():
            wavs, sample_rate = model.generate_custom_voice(
                text=body.input, language="Chinese", speaker=body.voice,
                non_streaming_mode=True, max_new_tokens=2048,
            )
        samples = wavs[0]
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return Response(buffer.getvalue(), media_type="audio/wav", headers={
            "X-Generation-Seconds": f"{time.monotonic() - start:.3f}",
            "X-Audio-Seconds": f"{len(samples) / sample_rate:.3f}",
        })
    finally:
        lock.release()
