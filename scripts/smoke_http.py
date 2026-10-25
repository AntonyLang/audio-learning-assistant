"""Smoke test against a running deployment (docker compose or local uvicorn).

    python scripts/smoke_http.py [base_url]

It logs in with the seeded demo account, submits a text source, waits for the ingest run,
creates an episode, waits for generation and publication, then checks the episode detail,
the private feed, a media Range request and the web shell.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
WEB = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
ARTICLE = ROOT / "evals/fixtures/articles/distributed-idempotency.md"


async def wait_run(client: httpx.AsyncClient, run_id: str, *, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    seen: list[str] = []
    while time.time() < deadline:
        snapshot = (await client.get(f"/v1/runs/{run_id}")).json()
        marker = f"{snapshot['state']}/{snapshot.get('phase')}"
        if not seen or seen[-1] != marker:
            seen.append(marker)
            print(f"    {marker}")
        if snapshot["state"] in {"SUCCEEDED", "FAILED", "WAITING_REVIEW", "CANCELLED", "DEFERRED"}:
            return snapshot
        await asyncio.sleep(1.5)
    raise TimeoutError(f"run {run_id} did not finish: {seen}")


async def main() -> int:
    async with httpx.AsyncClient(base_url=API, timeout=60.0) as client:
        health = (await client.get("/healthz")).json()
        print(f"[health] {health}")

        login = await client.post(
            "/v1/session", json={"email": "demo@local", "password": "demo12345"}
        )
        login.raise_for_status()
        client.headers["x-csrf-token"] = login.json()["csrf_token"]
        print("[auth] demo session ok")

        source = await client.post(
            "/v1/sources",
            json={
                "kind": "text",
                "text": ARTICLE.read_text("utf-8"),
                "title": "超时、重试与幂等（compose 冒烟）",
            },
            headers={"Idempotency-Key": f"smoke-{time.time_ns()}"},
        )
        source.raise_for_status()
        payload = source.json()
        print(f"[ingest] run {payload['run_id']} state={payload['state']}")
        ingest = await wait_run(client, payload["run_id"])
        if ingest["state"] != "SUCCEEDED":
            print(f"    ingest failed: {ingest}")
            return 1

        episode = await client.post(
            "/v1/episodes",
            json={
                "source_id": payload["source_id"],
                "question": "调用超时为什么不能断定远端没有完成？出现未知结果时应该先做什么？",
            },
            headers={"Idempotency-Key": f"smoke-ep-{time.time_ns()}"},
        )
        episode.raise_for_status()
        created = episode.json()
        print(f"[generate] run {created['run_id']} deferred={created['deferred']}")
        generated = await wait_run(client, created["run_id"])
        if generated["state"] != "SUCCEEDED":
            print(f"    generation ended as {generated}")
            return 1
        print(
            f"    duration_ms={generated['revision']['duration_ms']} "
            f"budget={generated['budget_usage']}"
        )

        # Publication is handled by a second run (PUBLISH) picked up by the dispatcher.
        deadline = time.time() + 120
        item = None
        while time.time() < deadline:
            today = (await client.get("/v1/today")).json()
            item = next(
                (i for i in today["items"] if i["episode_id"] == created["episode_id"]), None
            )
            if item and item["state"] == "AVAILABLE":
                break
            await asyncio.sleep(1.5)
        if not item or item["state"] != "AVAILABLE":
            print(f"    episode was not published: {item}")
            return 1
        print(f"[publish] plan_item={item['state']} duration={item['duration_ms']}ms")

        detail = (await client.get(f"/v1/episodes/{created['episode_id']}")).json()
        claims = detail["script"]["claims"]
        assert detail["published"], detail
        assert all(claim["review"] == "SUPPORTED" for claim in claims), claims
        print(
            f"[detail] claims={len(claims)} all_supported=True "
            f"segments={len(detail['script']['segments'])} sources={len(detail['evidence'])}"
        )

        token = (await client.post("/v1/feed-tokens")).json()
        feed = await client.get(token["url"].replace(API, ""))
        feed.raise_for_status()
        enclosure = re.search(r'url="([^"]+\.mp3)"', feed.text)
        assert enclosure, feed.text[:400]
        media_path = urlsplit(enclosure.group(1)).path
        media = await client.get(media_path)
        media.raise_for_status()
        size = int(media.headers["content-length"])
        partial = await client.get(media_path, headers={"Range": "bytes=0-99"})
        print(
            f"[feed] items ok, audio={size} bytes, range_status={partial.status_code}, "
            f"range_bytes={len(partial.content)}"
        )
        assert partial.status_code == 206 and len(partial.content) == 100

    async with httpx.AsyncClient(timeout=20.0) as web:
        shell = await web.get(WEB)
        print(f"[web] {WEB} -> {shell.status_code}, {len(shell.content)} bytes")
        assert shell.status_code == 200 and "root" in shell.text

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
