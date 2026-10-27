import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.desktop.api import create_app
from app.desktop.config import DesktopConfig
from app.desktop.research import CPA, Researcher
from app.desktop.store import Store
from app.desktop.tts import segments


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / "_data/desktop-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.config = DesktopConfig(data=Path(self.directory.name))
        self.app = create_app(self.config, autostart_tts=False)
        self.store = self.app.state.store

    def tearDown(self):
        self.directory.cleanup()

    async def test_multiple_materials_are_one_entry_and_web_can_be_disabled(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1") as client:
            response = await client.post("/local/api/entries", headers={"X-Local-App": "1"},
                                         data={"title": "幂等", "text": "正文材料", "urls": "https://example.com/a\nhttps://example.com/b", "web_enabled": "false"},
                                         files=[("files", ("first.txt", b"first")), ("files", ("second.md", b"second"))])
            self.assertEqual(response.status_code, 201, response.text)
            entry_id = response.json()["id"]
            self.assertEqual(self.store.get(entry_id)["web_enabled"], 0)
            self.assertEqual(len(self.store.sources(entry_id)), 5)
            self.assertEqual(self.store.get(entry_id)["status"], "queued")

    async def test_browser_cross_origin_mutations_are_rejected(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1") as client:
            self.assertEqual((await client.post("/local/api/tts/start")).status_code, 403)
            response = await client.post("/local/api/tts/start", headers={"X-Local-App": "1", "Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual((await client.get("/local/api/health")).status_code, 200)

    async def test_unsupported_file_is_rejected_without_creating_entry(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1") as client:
            response = await client.post("/local/api/entries", headers={"X-Local-App": "1"}, files={"files": ("bad.exe", b"abc")})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.store.list(), [])

    async def test_recovery_preserves_draft_and_completed_entries(self):
        active = self.store.create("active", "", 30, True)
        complete = self.store.create("complete", "", 30, True)
        self.store.update(active, status="voicing", script="已保存讲稿", segment_done=2)
        self.store.update(complete, status="completed", audio_path="done.mp3")
        reopened = Store(self.config.data)
        self.assertEqual(reopened.get(active)["status"], "interrupted")
        self.assertEqual(reopened.get(active)["script"], "已保存讲稿")
        self.assertEqual(reopened.get(complete)["status"], "completed")

    async def test_source_tools_cannot_read_other_entries(self):
        first = self.store.create("first", "", 30, False)
        second = self.store.create("second", "", 30, False)
        source_id = self.store.add_source(second, "private", "text", "", text="另一个条目的资料")
        researcher = Researcher(self.config, self.store, self.app.state.runner.documents)
        with self.assertRaises(ValueError):
            researcher.material(first, source_id)

    async def test_cpa_wrapper_preserves_tool_calls(self):
        message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "type": "function",
                   "function": {"name": "web_search", "arguments": '{"query":"idempotency"}'}}], "reasoning": "research"}
        response = httpx.Response(200, json={"success": True, "data": {"choices": [{"finish_reason": "tool_calls", "message": message}]}})
        with patch("httpx.AsyncClient.post", AsyncMock(return_value=response)):
            parsed = await CPA(self.config).chat([{"role": "user", "content": "test"}])
        self.assertEqual(parsed["tool_calls"], message["tool_calls"])
        self.assertEqual(parsed["reasoning_content"], "research")

    async def test_stopping_tts_pauses_active_job_and_restart_queues_it(self):
        entry_id = self.store.create("active", "", 30, True)
        self.store.update(entry_id, status="voicing", script="现有讲稿", segment_done=1)
        runner = self.app.state.runner
        runner.current = entry_id
        runner.current_task = asyncio.create_task(asyncio.sleep(20))
        await runner.pause_tts()
        self.assertEqual(self.store.get(entry_id)["status"], "waiting_tts")
        runner.resume_waiting()
        self.assertEqual(self.store.get(entry_id)["status"], "queued")
        self.assertEqual(self.store.get(entry_id)["script"], "现有讲稿")
        self.assertEqual(self.store.get(entry_id)["segment_done"], 1)

    def test_utf8_segmentation_handles_chinese_emoji_and_long_sentences(self):
        text = "这是长篇中文材料😀。" * 90 + "A" * 400
        parts = segments(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 240 for part in parts))


if __name__ == "__main__":
    unittest.main()
