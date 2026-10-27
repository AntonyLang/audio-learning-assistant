"""Cloud adapters for the first pinned provider set (doc §3.1, §13.2).

They are only constructed when ``PROVIDER_MODE=cloud`` and the matching key is present.
The request/response shapes follow the documented DashScope compatible-mode and Tavily
HTTP APIs; the offline mode exists so this repository stays runnable without keys.
"""

from __future__ import annotations

import httpx

from app.adapters.base import (
    AdapterError,
    LLMResponse,
    ModelInfo,
    ProviderTimeout,
    SearchResult,
    TTSResult,
    Usage,
    new_request_id,
)
from app.adapters.prompts import system_prompt
from app.config import Settings
from app.services.textproc import estimate_tokens


def _client(timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


class DashScopeLLM:
    kind = "llm"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.info = ModelInfo(
            provider=settings.llm_provider,
            model=settings.llm_model,
            kind="llm",
            params={"json_mode": True, "temperature": 0.0},
        )

    async def complete_json(
        self, *, task: str, payload: dict, system: str = "", max_output_tokens: int | None = None
    ) -> LLMResponse:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system or system_prompt(task)},
                {
                    "role": "user",
                    "content": f"任务：{task}\n只输出 JSON 对象。\n输入数据：\n"
                    + _json_dumps(payload),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": max_output_tokens or self.settings.llm_output_token_cap,
        }
        if self.settings.llm_thinking == "disabled":
            if self.settings.llm_provider in {"dashscope", "siliconflow"}:
                body["enable_thinking"] = False
            elif self.settings.llm_provider in {"mimo", "volcengine"}:
                body["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            async with _client() as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:  # pragma: no cover - network path
            raise ProviderTimeout("dashscope") from exc
        if response.status_code >= 500:
            raise AdapterError("llm_upstream_5xx", str(response.status_code), retryable=True)
        if response.status_code >= 400:
            raise AdapterError("llm_request_failed", response.text[:400])
        data = response.json()
        if data["choices"][0].get("finish_reason") == "length":
            raise AdapterError("llm_output_truncated", "increase LLM_OUTPUT_TOKEN_CAP")
        content = data["choices"][0]["message"]["content"]
        parsed = _loads_json(content)
        usage = data.get("usage", {})
        returned = data.get("model")
        return LLMResponse(
            data=parsed,
            usage=Usage(tokens_in=usage.get("prompt_tokens", 0), tokens_out=usage.get("completion_tokens", 0)),
            model=ModelInfo(
                provider=self.info.provider,
                model=self.info.model,
                kind=self.info.kind,
                returned_model=returned,
                prompt_version=payload.get("prompt_version", ""),
                params=self.info.params,
            ),
        )


class DashScopeEmbedding:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.info = ModelInfo(
            provider="dashscope",
            model=settings.embedding_model,
            kind="embedding",
            params={"dim": settings.embedding_dim, "normalised": False},
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.settings.embedding_base_url.rstrip('/')}/embeddings"
        body = {
            "model": self.settings.embedding_model,
            "input": texts,
            "dimensions": self.settings.embedding_dim,
            "encoding_format": "float",
        }
        headers = {"Authorization": f"Bearer {self.settings.embedding_api_key}"}
        out: list[list[float]] = []
        # DashScope caps a single request at 10 inputs for v4; batch conservatively.
        for start in range(0, len(texts), 10):
            batch = dict(body, input=texts[start : start + 10])
            try:
                async with _client() as client:
                    response = await client.post(url, json=batch, headers=headers)
            except httpx.TimeoutException as exc:  # pragma: no cover
                raise ProviderTimeout("dashscope-embedding") from exc
            if response.status_code >= 400:
                raise AdapterError("embedding_request_failed", response.text[:400])
            payload = response.json()
            out.extend([row["embedding"] for row in sorted(payload["data"], key=lambda row: row["index"])])
        for vector in out:
            if len(vector) != self.settings.embedding_dim:
                raise AdapterError(
                    "embedding_dim_mismatch",
                    f"expected {self.settings.embedding_dim}, got {len(vector)}",
                )
        return out


class DashScopeTTS:
    """Non-realtime synthesis: one short segment per call, URL downloaded immediately."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.info = ModelInfo(
            provider="dashscope",
            model=settings.tts_model,
            kind="tts",
            params={"voice": settings.tts_voice, "language": settings.tts_language},
        )

    async def synthesize(self, text: str, voice: str, language: str) -> TTSResult:
        if len(text) > 600:
            raise AdapterError("tts_input_too_long", f"{len(text)} chars")
        url = f"{self.settings.tts_base_url.rstrip('/')}/services/aigc/multimodal-generation/generation"
        body = {
            "model": self.settings.tts_model,
            "input": {"text": text, "voice": voice, "language_type": language},
        }
        headers = {"Authorization": f"Bearer {self.settings.tts_api_key}"}
        try:
            async with _client(90.0) as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:  # pragma: no cover
            raise ProviderTimeout("dashscope-tts") from exc
        if response.status_code >= 400:
            raise AdapterError("tts_request_failed", response.text[:400])
        payload = response.json()
        audio = payload.get("output", {}).get("audio", {})
        audio_url = audio.get("url")
        request_id = payload.get("request_id", "")
        if not audio_url:
            raise AdapterError("tts_bad_response", str(payload)[:400])
        # The URL is recorded by the caller before the download is attempted, which is
        # what lets a retry resume a finished synthesis instead of paying for it twice.
        return TTSResult(
            provider_request_id=request_id,
            audio=None,
            audio_url=audio_url,
            expires_at=None,
            usage=Usage(characters=len(text)),
            model=self.info,
            content_type="audio/mpeg",
        )

    async def download(self, result: TTSResult) -> bytes:
        if not result.audio_url:
            raise AdapterError("tts_no_url", "synthesis response carried no audio url")
        return await self.download_url(result.audio_url)

    async def download_url(self, url: str) -> bytes:
        try:
            async with _client(60.0) as client:
                audio_response = await client.get(url)
        except httpx.TimeoutException as exc:  # pragma: no cover
            raise ProviderTimeout("dashscope-tts-download") from exc
        if audio_response.status_code != 200:
            raise AdapterError("tts_download_failed", str(audio_response.status_code), retryable=True)
        return audio_response.content


class SpeechAPI:
    """WAV speech endpoint used by the private CPU TTS service and compatible APIs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.info = ModelInfo(provider="speech-api", model=settings.tts_model, kind="tts",
                              params={"voice": settings.tts_voice})

    async def synthesize(self, text: str, voice: str, language: str) -> TTSResult:
        headers = {"Authorization": f"Bearer {self.settings.tts_api_key}"} if self.settings.tts_api_key else {}
        body = {"model": self.settings.tts_model, "input": text, "voice": voice, "response_format": "wav"}
        if self.settings.tts_provider == "qwen-native":
            body.update(language=language, temperature=0.9, top_k=50, top_p=1.0, rep_penalty=1.05, seed=42)
        try:
            async with _client(self.settings.tts_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.tts_base_url.rstrip('/')}/audio/speech",
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout("speech-api") from exc
        if response.status_code >= 400:
            raise AdapterError("tts_request_failed", f"HTTP {response.status_code}",
                               retryable=response.status_code == 429 or response.status_code >= 500)
        return TTSResult(provider_request_id=new_request_id("speech"), audio=response.content,
                         expires_at=None, usage=Usage(characters=len(text)), model=self.info,
                         content_type="audio/wav")

    async def download(self, result: TTSResult) -> bytes:
        return result.audio

    async def download_url(self, url: str) -> bytes:
        raise AdapterError("speech_has_no_download_url")


class TavilySearch:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.info = ModelInfo(provider="tavily", model="basic", kind="search")

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchResult]:
        if not self.settings.tavily_api_key:
            raise AdapterError("search_not_configured", "仅依据用户提交的材料生成")
        body: dict = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "max_results": min(max_results, self.settings.max_tavily_results),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with _client(30.0) as client:
                response = await client.post("https://api.tavily.com/search", json=body)
        except httpx.TimeoutException as exc:  # pragma: no cover
            raise ProviderTimeout("tavily") from exc
        if response.status_code >= 500:
            raise AdapterError("search_upstream_5xx", str(response.status_code), retryable=True)
        if response.status_code >= 400:
            raise AdapterError("search_request_failed", response.text[:300])
        payload = response.json()
        return [
            SearchResult(
                url=row.get("url", ""),
                title=row.get("title", ""),
                snippet=(row.get("content") or "")[:400],
                score=float(row.get("score") or 0.0),
            )
            for row in payload.get("results", [])
        ]


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _loads_json(content: str) -> dict:
    import json

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end <= start:
            raise AdapterError("llm_invalid_json", content[:200]) from None
        parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise AdapterError("llm_not_object", type(parsed).__name__)
    return parsed


def tokens_for(text: str) -> int:
    return estimate_tokens(text)
