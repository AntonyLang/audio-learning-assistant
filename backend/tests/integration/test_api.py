from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.db import SessionFactory
from app.jobs.dispatch import set_dispatcher
from app.main import app
from app.models import Episode, GenerationRun
from app.pipeline.runner import execute_run

pytestmark = pytest.mark.integration


class RecordingDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    def enqueue(self, run_id: uuid.UUID) -> None:
        self.enqueued.append(run_id)


@pytest.fixture
async def dispatcher():
    recorder = RecordingDispatcher()
    set_dispatcher(recorder)
    yield recorder
    set_dispatcher(recorder)


@pytest.fixture
async def client(dispatcher):
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver", timeout=60.0
        ) as http:
            response = await http.post(
                "/v1/session", json={"email": "demo@local", "password": "demo12345"}
            )
            assert response.status_code == 200, response.text
            http.headers["x-csrf-token"] = response.json()["csrf_token"]
            http.csrf = response.json()["csrf_token"]  # type: ignore[attr-defined]
            yield http


async def _drain(dispatcher, rounds: int = 6) -> list[uuid.UUID]:
    """Execute everything dispatchable, including runs the pipeline created itself.

    The inline dispatcher only sees API-created runs; a PUBLISH run is inserted by the
    generation pipeline, exactly as it would be picked up by the real dispatcher loop.
    """
    from sqlalchemy import select

    from app.models import RunState

    executed: list[uuid.UUID] = []
    for _round in range(rounds):
        ids = list(dispatcher.enqueued)
        dispatcher.enqueued.clear()
        async with SessionFactory() as session:
            rows = await session.execute(
                select(GenerationRun.id).where(
                    GenerationRun.state.in_([RunState.QUEUED, RunState.RETRY_WAIT])
                )
            )
            pending = [row for row in rows.scalars() if row not in ids]
        ids.extend(pending)
        if not ids:
            break
        for run_id in ids:
            await execute_run(run_id, worker_id="api-test")
            executed.append(run_id)
    return executed


async def _build_published_episode(client, dispatcher) -> dict:
    body = __import__("pathlib").Path(__file__).resolve().parents[3]
    article = (body / "evals/fixtures/articles/distributed-idempotency.md").read_text("utf-8")
    response = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": article, "title": "超时与幂等", "intent_note": "api test"},
        headers={"Idempotency-Key": "src-1"},
    )
    assert response.status_code == 202, response.text
    source = response.json()
    await _drain(dispatcher)
    async with SessionFactory() as session:
        ingest_run = await session.get(GenerationRun, uuid.UUID(source["run_id"]))
        assert ingest_run.state.value == "SUCCEEDED", ingest_run.last_error

    response = await client.post(
        "/v1/episodes",
        json={"source_id": source["source_id"], "question": "调用超时为什么不能断定远端没有完成？"},
        headers={"Idempotency-Key": "ep-1"},
    )
    assert response.status_code == 202, response.text
    episode = response.json()
    await _drain(dispatcher)
    return episode


async def test_source_and_episode_are_idempotent(client, dispatcher):
    first = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": "短文内容。" * 40},
        headers={"Idempotency-Key": "dup"},
    )
    second = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": "短文内容。" * 40},
        headers={"Idempotency-Key": "dup"},
    )
    assert first.status_code == 202
    assert first.json()["source_id"] == second.json()["source_id"]

    conflict = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": "不同的内容。" * 40},
        headers={"Idempotency-Key": "dup"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "idempotency_key_payload_mismatch"

    for run_id in list(dispatcher.enqueued):
        await execute_run(run_id, worker_id="api-test")


async def test_mutating_request_requires_csrf(client):
    response = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": "内容。" * 40},
        headers={"x-csrf-token": "wrong"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "csrf_failed"


async def test_unauthenticated_request_is_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        response = await anon.get("/v1/today")
    assert response.status_code == 401


async def test_run_snapshot_events_and_sse_replay(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    run = await client.get(f"/v1/runs/{episode['run_id']}")
    assert run.status_code == 200
    snapshot = run.json()
    assert snapshot["state"] == "SUCCEEDED"
    assert snapshot["last_seq"] > 0
    assert snapshot["revision"]["status"] == "READY"
    assert 0 < snapshot["revision"]["duration_ms"] <= 300_000

    seen: list[int] = []
    async with client.stream(
        "GET",
        f"/v1/runs/{episode['run_id']}/events",
        headers={"Last-Event-ID": "0"},
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                seen.append(int(line[4:]))
            if line.startswith("event: stream.end"):
                break
    assert seen == sorted(seen)
    assert seen[-1] == snapshot["last_seq"]

    # Replay from a middle cursor only returns later events.
    later: list[int] = []
    async with client.stream(
        "GET",
        f"/v1/runs/{episode['run_id']}/events",
        headers={"Last-Event-ID": str(seen[2])},
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                later.append(int(line[4:]))
            if line.startswith("event: stream.end"):
                break
    assert later and min(later) > seen[2]


async def test_today_and_episode_detail(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    today = await client.get("/v1/today")
    assert today.status_code == 200
    snapshot = today.json()
    assert snapshot["budget_ms"] > 0
    assert snapshot["total_ms"] <= snapshot["budget_ms"]
    item = next(i for i in snapshot["items"] if i["episode_id"] == episode["episode_id"])
    assert item["state"] == "AVAILABLE"
    assert item["published"] is True

    detail = await client.get(f"/v1/episodes/{episode['episode_id']}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["published"] is True
    assert payload["script"]["claims"]
    assert all(claim["review"] == "SUPPORTED" for claim in payload["script"]["claims"])
    assert payload["evidence"]
    assert payload["audio"]["ready"] is True
    assert payload["audio"]["duration_ms"] == payload["duration_ms"]


async def test_private_feed_and_range_media(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    token_response = await client.post("/v1/feed-tokens")
    assert token_response.status_code == 201
    token = token_response.json()["token"]

    feed = await client.get(f"/v1/feeds/{token}.xml")
    assert feed.status_code == 200
    text = feed.text
    assert "<rss" in text and 'version="2.0"' in text
    assert f"/v1/feeds/{token}/episodes/{episode['episode_id']}/audio/" in text
    assert "audio/mpeg" in text

    detail = await client.get(f"/v1/episodes/{episode['episode_id']}")
    revision_id = detail.json()["revision"]["revision_id"]
    media_path = f"/v1/feeds/{token}/episodes/{episode['episode_id']}/audio/{revision_id}.mp3"

    full = await client.get(media_path)
    assert full.status_code == 200
    size = int(full.headers["content-length"])
    assert size == len(full.content)
    assert full.headers["accept-ranges"] == "bytes"

    partial = await client.get(media_path, headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.content) == 100
    assert partial.headers["content-range"] == f"bytes 0-99/{size}"
    assert partial.content == full.content[:100]

    tail = await client.get(media_path, headers={"Range": "bytes=-50"})
    assert tail.status_code == 206
    assert tail.content == full.content[-50:]

    unsatisfiable = await client.get(media_path, headers={"Range": f"bytes={size + 10}-"})
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{size}"

    multi = await client.get(media_path, headers={"Range": "bytes=0-9,20-29"})
    assert multi.status_code == 200

    web = await client.get(f"/v1/episodes/{episode['episode_id']}/audio", headers={"Range": "bytes=0-9"})
    assert web.status_code == 206
    assert len(web.content) == 10

    head = await client.head(media_path)
    assert head.status_code == 200
    assert head.headers["content-length"] == str(size)
    assert head.content == b""

    revoked = await client.delete(f"/v1/feed-tokens/{token_response.json()['feed_token_id']}")
    assert revoked.status_code == 204
    assert (await client.get(f"/v1/feeds/{token}.xml")).status_code == 404


async def test_episode_is_scoped_to_its_owner(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    from app.security import hash_password

    async with SessionFactory() as session:
        from app.models import UserProfile

        other = UserProfile(
            email="other@test",
            password_hash=hash_password("pw-other"),
            timezone="Asia/Shanghai",
            prefs={"daily_budget_ms": 600_000},
        )
        session.add(other)
        await session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        login = await anon.post(
            "/v1/session", json={"email": "other@test", "password": "pw-other"}
        )
        assert login.status_code == 200
        response = await anon.get(f"/v1/episodes/{episode['episode_id']}")
    assert response.status_code == 404


async def test_feedback_creates_a_follow_up_run(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    dispatcher.enqueued.clear()
    response = await client.post(
        f"/v1/episodes/{episode['episode_id']}/feedback",
        json={"kind": "example", "note": "想要一个例子", "client_event_id": "evt-1"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["action"] == "follow_up_created"
    assert body["follow_up"]["episode_id"]

    duplicate = await client.post(
        f"/v1/episodes/{episode['episode_id']}/feedback",
        json={"kind": "example", "note": "重复提交", "client_event_id": "evt-1"},
    )
    assert duplicate.json()["status"] == "duplicate"

    await _drain(dispatcher)


async def test_partial_source_requires_acknowledgement(client, dispatcher):
    response = await client.post(
        "/v1/sources",
        json={"kind": "text", "text": "太短了。", "title": "短文本"},
    )
    assert response.status_code == 202
    await _drain(dispatcher)
    source_id = response.json()["source_id"]

    blocked = await client.post("/v1/episodes", json={"source_id": source_id})
    assert blocked.status_code == 409
    assert blocked.json()["detail"].startswith("source_partial")

    allowed = await client.post(
        "/v1/episodes", json={"source_id": source_id, "acknowledge_partial": True}
    )
    assert allowed.status_code == 202
    await _drain(dispatcher)
    async with SessionFactory() as session:
        episode = await session.get(Episode, uuid.UUID(allowed.json()["episode_id"]))
        assert episode is not None


async def test_new_tab_can_restore_csrf_from_authenticated_profile(client):
    client.headers.pop("x-csrf-token")
    profile = await client.get("/v1/profile")
    assert profile.status_code == 200
    assert profile.headers["cache-control"] == "private, no-store"
    client.headers["x-csrf-token"] = profile.json()["csrf_token"]
    saved = await client.patch("/v1/profile", json={"daily_budget_ms": 900_000})
    assert saved.status_code == 200


async def test_password_change_revokes_other_devices(client):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as other:
        login = await other.post("/v1/session", json={"email": "demo@local", "password": "demo12345"})
        assert login.status_code == 200
        changed = await client.put("/v1/password", json={
            "current_password": "demo12345", "new_password": "new-long-test-password-2026",
        })
        assert changed.status_code == 204
        assert (await other.get("/v1/profile")).status_code == 401
        assert (await client.get("/v1/profile")).status_code == 200
        assert (await other.post("/v1/session", json={"email": "demo@local", "password": "demo12345"})).status_code == 401


async def test_playback_seek_position_does_not_count_as_listening(client, dispatcher):
    episode = await _build_published_episode(client, dispatcher)
    episode_id = episode["episode_id"]
    event = {"episode_id": episode_id, "session_id": "device-one", "client_seq": 1,
             "kind": "progress", "start_ms": 0, "end_ms": 3000}
    assert (await client.post("/v1/playback-events", json=event)).status_code == 202
    duplicate = await client.post("/v1/playback-events", json=event)
    assert duplicate.json()["status"] == "duplicate"
    event.update(client_seq=2, kind="position", start_ms=10_000, end_ms=10_000)
    assert (await client.post("/v1/playback-events", json=event)).status_code == 202
    summary = (await client.get(f"/v1/episodes/{episode_id}/playback")).json()
    assert summary["listened_ms"] == 3000
    assert summary["position_ms"] == 10_000
