"""Generate a listening sample and measure CPU-service wall time and audio duration."""

import argparse
import io
import json
import time
import wave
from pathlib import Path

import httpx

TEXT = (
    "今天我们用一个生活中的例子，理解什么叫幂等。你在手机上付款，网络突然断了，"
    "页面提示请求超时。这时候，超时并不等于付款失败：银行可能已经扣款，只是回复没有送到。"
    "如果你再点一次，系统应该识别这是同一笔订单，而不是再扣一次钱。"
    "常见的做法，是给这次操作一个固定编号。无论重试几次，都携带同一个编号。"
    "服务端先检查这个编号是否处理过，处理过就返回之前的结果。"
    "但这里有个关键条件：检查编号和记录结果，要放在同一个可靠的事务里。"
    "否则，两个请求同时进来，仍可能各自执行一次。"
    "所以，幂等解决的不是网络不会出错，而是出错后可以安全地重试。"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18081")
    parser.add_argument("--voice", default="Serena")
    parser.add_argument("--model", default="qwen3-tts-0.6b-customvoice")
    parser.add_argument("--text", default=TEXT)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--environment", default="local Docker, 1.5 CPU quota, 5 GiB limit; not a VPS benchmark")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "deploy" / ".private" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    durations = []
    wall = 0.0
    chunks = []
    sample_rate = 0
    with httpx.Client(timeout=args.timeout, trust_env=False) as client:
        for _ in range(args.repeat):
            start = time.perf_counter()
            response = client.post(f"{args.url}/v1/audio/speech", json={
                "model": args.model, "input": args.text, "voice": args.voice,
                "response_format": "wav",
            })
            response.raise_for_status()
            wall += time.perf_counter() - start
            with wave.open(io.BytesIO(response.content)) as audio:
                sample_rate = audio.getframerate()
                durations.append(audio.getnframes() / sample_rate)
                chunks.append(audio.readframes(audio.getnframes()))
    path = output / f"{args.model}-{args.voice}-{len(args.text)}chars-x{args.repeat}.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        for chunk in chunks:
            audio.writeframes(chunk)
    metrics = {
        "model": args.model, "voice": args.voice,
        "characters": len(args.text) * args.repeat, "wall_seconds": round(wall, 3),
        "audio_seconds": round(sum(durations), 3), "rtf": round(wall / sum(durations), 3),
        "sample_rate": sample_rate, "channels": 1, "output": str(path),
        "environment": args.environment,
    }
    path.with_suffix(".json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(metrics, ensure_ascii=True))


if __name__ == "__main__":
    main()
