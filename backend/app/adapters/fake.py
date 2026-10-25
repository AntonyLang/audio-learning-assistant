"""Deterministic local adapters.

They are not stubs that return canned prose: the embedding is a real hashing vectoriser,
the TTS writes real PCM audio whose length is proportional to the spoken text, and the
LLM derives every draft claim from an actual source span. That keeps the *contracts* —
citation relocation, duration measurement, budget accounting — under test without API
keys, which is what makes the repository reproducible.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import wave
from datetime import UTC, datetime, timedelta

from app.adapters.base import (
    AdapterError,
    LLMResponse,
    ModelInfo,
    SearchResult,
    TTSResult,
    Usage,
)
from app.services.textproc import estimate_tokens, lexemes_for

_FEATURE = re.compile(r"[\u4e00-\u9fff]|[a-z0-9]+")


def _features(text: str) -> list[str]:
    tokens = _FEATURE.findall(text.lower())
    bigrams = [tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)]
    return tokens + bigrams


def hashing_embedding(text: str, dim: int = 1024) -> list[float]:
    """Feature-hashing embedding: deterministic, normalised, no network."""
    vector = [0.0] * dim
    for feature in _features(text):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class FakeEmbedding:
    name = "fake-embedding"
    info = ModelInfo(
        provider="local",
        model="hashing-1024",
        kind="embedding",
        params={"dim": 1024, "normalised": True},
    )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


class FakeSearch:
    """Searches the offline web fixtures in ``evals/fixtures/web``."""

    def __init__(self, corpus: dict[str, tuple[str, str]] | None = None) -> None:
        self.corpus = corpus or {}
        self.info = ModelInfo(provider="local", model="fixture-search", kind="search")

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchResult]:
        terms = set(lexemes_for(query).split())
        scored: list[tuple[float, str, str, str]] = []
        for slug, (title, body) in self.corpus.items():
            body_terms = set(lexemes_for(body).split())
            if not terms or not body_terms:
                continue
            overlap = len(terms & body_terms) / math.sqrt(len(terms) * len(body_terms))
            if overlap > 0:
                snippet = body.strip().split("\n")[0][:160]
                scored.append((overlap, f"local://{slug}", title, snippet))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            SearchResult(url=url, title=title, snippet=snippet, score=score)
            for score, url, title, snippet in scored[:max_results]
        ]


class FakeTTS:
    """Writes 24 kHz mono PCM whose duration is ``chars / chars_per_second``.

    The tone carries a per-segment pitch and a sentence-level envelope so concatenated
    audio is not a constant beep; duration is the part that matters for the gate.
    """

    chars_per_second = 4.2

    def __init__(self, sample_rate: int = 24_000) -> None:
        self.sample_rate = sample_rate
        self.info = ModelInfo(
            provider="local",
            model="fake-tts-24k",
            kind="tts",
            params={"sample_rate": sample_rate, "chars_per_second": self.chars_per_second},
        )

    def _seconds(self, text: str) -> float:
        return max(0.35, len(text) / self.chars_per_second)

    async def synthesize(self, text: str, voice: str, language: str) -> TTSResult:
        seconds = self._seconds(text)
        total = int(seconds * self.sample_rate)
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
        base_hz = 140 + (digest[0] % 60)
        frames = bytearray()
        for i in range(total):
            t = i / self.sample_rate
            # slow vibrato keeps the signal non-constant while staying speech-like in band
            vibrato = 1.0 + 0.02 * math.sin(2 * math.pi * 0.7 * t)
            sample = (
                0.22 * math.sin(2 * math.pi * base_hz * vibrato * t)
                + 0.10 * math.sin(2 * math.pi * (base_hz * 2.02) * t)
                + 0.05 * math.sin(2 * math.pi * (base_hz * 3.03) * t)
            )
            # syllable-rate amplitude envelope
            envelope = 0.65 + 0.35 * math.sin(2 * math.pi * 3.1 * t) ** 2
            frames += struct.pack("<h", int(max(-1.0, min(1.0, sample * envelope)) * 32767))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            handle.writeframes(bytes(frames))
        return TTSResult(
            provider_request_id=f"fake_tts_{digest.hex()[:12]}",
            audio=buffer.getvalue(),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
            usage=Usage(characters=len(text)),
            model=self.info,
            audio_url=None,
        )

    async def download(self, result: TTSResult) -> bytes:
        if result.audio is None:
            raise AdapterError("fake_tts_no_audio", result.provider_request_id)
        return result.audio

    async def download_url(self, url: str) -> bytes:
        raise AdapterError("fake_tts_no_url", url)


class FakeLLM:
    """Deterministic, span-faithful stand-in for the structured-output LLM.

    ``task`` selects a small rule set. Each result is still validated by the same
    Pydantic models the cloud adapter's output goes through, so schema drift is caught
    offline as well.
    """

    name = "fake-llm"

    def __init__(self) -> None:
        self.info = ModelInfo(
            provider="local",
            model="fake-qwen-plus",
            kind="llm",
            params={"json_mode": True, "temperature": 0.0},
        )

    async def complete_json(
        self, *, task: str, payload: dict, system: str = "", max_output_tokens: int | None = None
    ) -> LLMResponse:
        builder = {
            "focus": self._focus,
            "gap_plan": self._gap_plan,
            "draft": self._draft,
            "verify": self._verify,
        }.get(task)
        if builder is None:
            raise ValueError(f"unknown fake LLM task {task}")
        data = builder(payload)
        prompt_chars = len(str(payload))
        return LLMResponse(
            data=data,
            usage=Usage(
                tokens_in=estimate_tokens(str(payload)),
                tokens_out=estimate_tokens(str(data)),
            ),
            model=ModelInfo(
                provider=self.info.provider,
                model=self.info.model,
                kind=self.info.kind,
                prompt_version=payload.get("prompt_version", ""),
                params={**self.info.params, "prompt_chars": prompt_chars},
            ),
        )

    # -- tasks -------------------------------------------------------------
    def _focus(self, payload: dict) -> dict:
        question = payload.get("question") or ""
        points = [p for p in (payload.get("critical_candidates") or []) if p.get("text")]
        matched = [p for p in points if p.get("matched")]
        chosen = (matched or points)[:2]
        missing = [
            term
            for term in (payload.get("question_terms") or [])
            if term not in set(payload.get("document_terms") or [])
        ][:1]
        critical = [{"point": p["text"], "why": p.get("reason", ""), "matched": True} for p in chosen]
        # A concept the question depends on but the document never explains is a real gap;
        # treating it as a critical point is what makes the gap planner non-trivial.
        critical += [
            {
                "point": f"原文没有解释「{term}」的具体机制，需要补充",
                "why": "问题依赖的概念在原文中缺失",
                "matched": False,
            }
            for term in missing
        ]
        return {
            "core_question": question or "这份材料最值得记住的一点是什么？",
            "critical_points": critical[:3],
            "document_mode": payload.get("document_mode", "full"),
            "rationale": "offline baseline: matched document points plus one missing concept",
        }

    def _gap_plan(self, payload: dict) -> dict:
        uncovered = payload.get("uncovered_points") or []
        core = payload.get("core_question", "")
        gaps = []
        for point in uncovered[:1]:
            # A query keeps the episode's own question and adds the missing point, which
            # is closer to what a person would type than the bare paragraph would be.
            gaps.append(
                {
                    "gap_id": "g1",
                    "question": f"{core} {point[:80]}".strip(),
                    "why_needed": "核心要点缺少原文直接支持",
                    "domain_hints": [],
                }
            )
        return {"gaps": gaps, "rationale": "offline baseline: only the first uncovered point"}

    def _draft(self, payload: dict) -> dict:
        """Assemble a script from evidence quotes, keeping every span exact."""
        core_question = payload.get("core_question", "")
        evidence = payload.get("evidence") or []
        target_chars = int(payload.get("target_chars", 1000))
        points = [p for p in payload.get("critical_points", []) if p]
        claims: list[dict] = []
        segments: list[dict] = []

        def add_claim(text: str, kind: str, refs: list[dict], critical: bool) -> str:
            claim_id = f"c{len(claims) + 1:02d}"
            claims.append(
                {
                    "claim_id": claim_id,
                    "text": text,
                    "kind": kind,
                    "critical": critical,
                    "evidence": refs,
                    "review": "NOT_FOUND",
                }
            )
            return claim_id

        used = 0
        opening = f"这一期只回答一个问题：{core_question}。"
        first_quote = evidence[0]["quote"] if evidence else ""
        answer = f"先说结论：{first_quote[:80]}" if first_quote else "先说结论：材料里没有足够依据。"
        segments.append(
            {
                "segment_id": "s1",
                "heading": "本期问题",
                "spoken_text": opening + answer,
                "claim_ids": [],
            }
        )
        used += len(opening) + len(answer)

        used_refs: set[str] = set()
        for index, point in enumerate(points, start=1):
            if used >= target_chars:
                break
            chosen = self._best_evidence(point, evidence, used_refs)
            if chosen is None:
                continue
            used_refs.add(chosen["ref"]["chunk_id"])
            quote = chosen["quote"]
            lead = f"第{index}点，{point.rstrip('。？?')}。原文是这样说的："
            claim_id = add_claim(quote, "external_fact", [chosen["ref"]], critical=True)
            text = lead + quote
            segments.append(
                {
                    "segment_id": f"s{len(segments) + 1}",
                    "heading": point[:24],
                    "spoken_text": text,
                    "claim_ids": [claim_id],
                }
            )
            used += len(text)

        # one example segment, explicitly not a sourced fact
        spare = [item for item in evidence if item["ref"]["chunk_id"] not in used_refs]
        if spare and used < target_chars:
            item = spare[0]
            text = f"放到实际场景里看：{item['quote'][:120]}。这不是新的结论，只是为了把上面的条件说清楚。"
            claim_id = add_claim(text, "example", [item["ref"]], critical=False)
            segments.append(
                {"segment_id": f"s{len(segments) + 1}", "heading": "一个例子", "spoken_text": text,
                 "claim_ids": [claim_id]}
            )
            used += len(text)

        boundary = (
            "最后说清楚边界：这一期只覆盖上面引用到的片段，"
            "原文没有讨论的情况，这里不做推断。"
        )
        segments.append(
            {
                "segment_id": f"s{len(segments) + 1}",
                "heading": "适用边界",
                "spoken_text": boundary,
                "claim_ids": [],
            }
        )
        return {
            "core_question": core_question,
            "one_line_answer": answer,
            "segments": segments,
            "claims": claims,
        }

    def _best_evidence(self, point: str, evidence: list[dict], used_refs: set[str]) -> dict | None:
        point_terms = set(lexemes_for(point).split())
        best: tuple[float, dict] | None = None
        for item in evidence:
            if item["ref"]["chunk_id"] in used_refs:
                continue
            terms = set(lexemes_for(item["quote"]).split())
            if not terms:
                continue
            overlap = len(point_terms & terms) / math.sqrt(len(point_terms) * len(terms) or 1)
            if overlap <= 0.05:
                continue
            if best is None or overlap > best[0]:
                best = (overlap, item)
        return best[1] if best is not None else None

    def _verify(self, payload: dict) -> dict:
        verdicts = []
        for claim in payload.get("claims", []):
            quote = (claim.get("quote") or "").strip()
            text = claim.get("text", "")
            if not quote:
                verdicts.append(
                    {"claim_id": claim["claim_id"], "verdict": "NOT_FOUND", "reason": "无可引用片段"}
                )
                continue
            if quote[:40] and quote[:40] in text:
                verdicts.append(
                    {"claim_id": claim["claim_id"], "verdict": "SUPPORTED", "reason": "引文在正文中逐字出现"}
                )
            elif len(set(quote) & set(text)) / max(len(set(quote)), 1) > 0.7:
                verdicts.append(
                    {"claim_id": claim["claim_id"], "verdict": "PARTIAL", "reason": "措辞改写，需人工确认"}
                )
            else:
                verdicts.append(
                    {"claim_id": claim["claim_id"], "verdict": "PARTIAL", "reason": "覆盖不足"}
                )
        return {"verdicts": verdicts}
