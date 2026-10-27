import json

import httpx
import pytest

from app.adapters import build_adapters, cloud
from app.adapters.base import AdapterError
from app.config import Settings


async def test_real_llm_receives_output_contract_and_disables_thinking(monkeypatch):
    def handle(request):
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        assert "critical_points" in body["messages"][0]["content"]
        assert "JSON" in body["messages"][0]["content"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"core_question":"什么是幂等？"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "model": "qwen-plus-snapshot",
        })

    monkeypatch.setattr(cloud, "_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = await cloud.DashScopeLLM(Settings()).complete_json(task="focus", payload={"question": "幂等"})
    assert result.data["core_question"] == "什么是幂等？"
    assert result.model.returned_model == "qwen-plus-snapshot"


async def test_embedding_response_order_follows_input(monkeypatch):
    def handle(request):
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]},
        ]})

    monkeypatch.setattr(cloud, "_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    result = await cloud.DashScopeEmbedding(Settings(embedding_dim=2)).embed(["甲", "乙"])
    assert result == [[1.0, 0.0], [0.0, 1.0]]


async def test_missing_optional_search_is_explicit_and_does_not_send_request():
    with pytest.raises(AdapterError, match="search_not_configured"):
        await cloud.TavilySearch(Settings(tavily_api_key="")).search("查询")


def test_cpu_tts_does_not_require_a_cloud_tts_key():
    settings = Settings(provider_mode="cloud", dashscope_api_key="shared-cloud-key", llm_api_key="test",
                        embedding_api_key="test", tts_provider="openai", tts_api_key="")
    adapters = build_adapters(settings)
    assert isinstance(adapters.tts, cloud.SpeechAPI)
    assert settings.tts_api_key == ""


def test_production_rejects_demo_account():
    with pytest.raises(ValueError, match="SEED_DEMO_USER"):
        Settings(app_env="production", seed_demo_user=True)


async def test_native_tts_sends_language_without_forwarding_cloud_key(monkeypatch):
    def handle(request):
        body = json.loads(request.content)
        assert body["language"] == "Chinese"
        assert body["voice"] == "serena"
        assert body["temperature"] == 0.9
        assert body["seed"] == 42
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"wave-result")

    monkeypatch.setattr(cloud, "_client", lambda *args: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    settings = Settings(tts_provider="qwen-native", dashscope_api_key="cloud-key", tts_api_key="")
    result = await cloud.SpeechAPI(settings).synthesize("测试中文", "serena", "Chinese")
    assert result.audio == b"wave-result"
