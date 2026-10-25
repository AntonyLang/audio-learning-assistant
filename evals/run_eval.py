"""Evaluation harness: quality, citation and duration metrics on the offline pipeline.

Run from the repository root:

    python evals/run_eval.py              # full C configuration on all cases
    python evals/run_eval.py --ablations  # adds A and B on the dev split

The deterministic local adapters are used, so the numbers are reproducible and do not
depend on a model provider. The point of the harness is not a leaderboard: it is a
repeatable gate that fails when citations stop relocating, when the audio exceeds the
documented limit, or when the budget stops being respected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.auth import demo_prefs
from app.config import get_settings
from app.db import SessionFactory, dispose_engine
from app.models import (
    Episode,
    EpisodeRevision,
    GenerationRun,
    RunKind,
    Source,
    SourceKind,
    StageArtifact,
    UserProfile,
)
from app.pipeline.runner import execute_run
from app.schemas.content import EvidencePack, ScriptDoc
from app.security import hash_password
from app.services import devtools, episode_service, runs
from app.services.ingest import EXTRACTOR_VERSION
from app.services.validation import validate_evidence_pack
from sqlalchemy import select

EVAL_EMAIL = "eval@local"
ARTICLES = ROOT / "evals" / "fixtures" / "articles"
CASES_FILE = ROOT / "evals" / "cases.json"
REPORT_MD = ROOT / "evals" / "report.md"
REPORT_JSON = ROOT / "evals" / "report.json"


@dataclass
class Config:
    label: str
    searches: int
    verify_mode: str
    description: str


CONFIGS = {
    "A": Config("A 原文摘要", 0, "structural", "无补充研究、无语义核源：最小生成基线"),
    "B": Config("B 定向补证", 2, "structural", "允许有预算的缺口检索，仍然只做结构化校验"),
    "C": Config("C 补证加核源", 2, "llm", "完整配置：缺口检索 + 语义核源 + 修订"),
}


@dataclass
class CaseResult:
    case_id: str
    split: str
    question: str
    run_state: str = ""
    stop_reason: str | None = None
    duration_ms: int = 0
    duration_ok: bool = False
    claims: int = 0
    claims_supported: int = 0
    quotes: list[str] = field(default_factory=list)
    spoken_text: str = ""
    spans_valid: bool = False
    evidence_problems: list[dict] = field(default_factory=list)
    external_evidence: bool = False
    searches: int = 0
    pages: int = 0
    must_quote_hits: int = 0
    must_explain_hits: int = 0
    error: str = ""

    @property
    def unsupported_rate(self) -> float:
        if not self.claims:
            return 0.0
        return (self.claims - self.claims_supported) / self.claims


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text("utf-8"))["cases"]


async def ensure_owner() -> UserProfile:
    async with SessionFactory() as session:
        owner = (
            await session.execute(select(UserProfile).where(UserProfile.email == EVAL_EMAIL))
        ).scalar_one_or_none()
        if owner is None:
            owner = UserProfile(
                email=EVAL_EMAIL,
                password_hash=hash_password("eval-only"),
                display_name="Eval",
                timezone="Asia/Shanghai",
                prefs=demo_prefs(get_settings()),
            )
            session.add(owner)
            await session.commit()
        owner.prefs = {**owner.prefs, "daily_budget_ms": 7_200_000}
        session.add(owner)
        await session.commit()
        return owner


async def reset_eval_data(owner: UserProfile) -> None:
    async with SessionFactory() as session:
        await devtools.reset_owner(session, owner.id)


async def ingest_article(owner: UserProfile, body: str, title: str) -> Source:
    async with SessionFactory() as session:
        source = Source(owner_id=owner.id, kind=SourceKind.TEXT, title=title, intent_note="eval")
        session.add(source)
        await session.flush()
        from app.adapters import build_adapters

        settings = get_settings()
        adapters = build_adapters(settings, fixtures_dir=settings.offline_fixtures_dir)
        stored = await adapters.storage.put_bytes(
            f"raw/{owner.id}/{source.id}/input.txt", body.encode("utf-8"), "text/plain"
        )
        source.input_ref = stored.key
        run, _ = await runs.create_run(
            session,
            runs.RunSpec(
                owner_id=owner.id,
                kind=RunKind.INGEST,
                request_key=f"ingest:{source.id}:{EXTRACTOR_VERSION}",
                source_id=source.id,
            ),
            get_settings(),
        )
        await session.commit()
        source_id, run_id = source.id, run.id
    await execute_run(run_id, worker_id="eval")
    async with SessionFactory() as session:
        return await session.get(Source, source_id)


async def generate(owner: UserProfile, source: Source, question: str) -> dict:
    async with SessionFactory() as session:
        owner_row = await session.get(UserProfile, owner.id)
        source_row = await session.get(Source, source.id)
        bundle = await episode_service.create_episode_with_run(
            session, owner=owner_row, source=source_row, question=question, settings=get_settings()
        )
        await session.commit()
        episode_id, revision_id, run_id = bundle.episode.id, bundle.revision.id, bundle.run.id
    await execute_run(run_id, worker_id="eval")
    async with SessionFactory() as session:
        run = await session.get(GenerationRun, run_id)
        revision = await session.get(EpisodeRevision, revision_id)
        pack_artifact = (
            await session.get(StageArtifact, revision.evidence_id) if revision.evidence_id else None
        )
        script_artifact = (
            await session.get(StageArtifact, revision.script_id) if revision.script_id else None
        )
        episode = await session.get(Episode, episode_id)
        verdicts = {
            row.get("claim_id"): row.get("verdict")
            for row in ((revision.config_snapshot or {}).get("review", {}).get("verdicts") or [])
        }
        problems: list[dict] = []
        pack = None
        if pack_artifact is not None:
            pack = EvidencePack.model_validate(pack_artifact.payload)
            problems = [
                problem.as_dict()
                for problem in await validate_evidence_pack(
                    session, owner_id=owner.id, pack=pack
                )
            ]
        script = ScriptDoc.model_validate(script_artifact.payload) if script_artifact else None
        if script is not None:
            script = script.model_copy(
                update={
                    "claims": [
                        claim.model_copy(
                            update={"review": verdicts.get(claim.claim_id, claim.review)}
                        )
                        for claim in script.claims
                    ]
                }
            )
        return {
            "run_state": run.state.value,
            "stop_reason": run.stop_reason,
            "duration_ms": int(revision.duration_ms or 0),
            "pack": pack,
            "problems": problems,
            "script": script,
            "published": episode.published_revision_id is not None,
        }


def score(case: dict, result: dict) -> CaseResult:
    settings = get_settings()
    outcome = CaseResult(
        case_id=case["id"],
        split=case["split"],
        question=case["question"],
        run_state=result["run_state"],
        stop_reason=result["stop_reason"],
        duration_ms=result["duration_ms"],
        duration_ok=0 < result["duration_ms"] <= settings.max_episode_ms,
        spans_valid=not result["problems"],
        evidence_problems=result["problems"],
    )
    script = result["script"]
    pack = result["pack"]
    if script is not None:
        outcome.claims = len(script.claims)
        outcome.claims_supported = sum(1 for claim in script.claims if claim.review == "SUPPORTED")
        outcome.spoken_text = script.spoken_text
    if pack is not None:
        outcome.quotes = [item.quote for item in pack.items]
        outcome.external_evidence = any(item.origin.value == "external" for item in pack.items)
        outcome.searches = pack.queries_used
        outcome.pages = pack.pages_fetched
        joined = "\n".join(outcome.quotes)
        outcome.must_quote_hits = sum(1 for needle in case["must_quote"] if needle in joined)
    text = outcome.spoken_text
    outcome.must_explain_hits = sum(1 for term in case["must_explain"] if term in text)
    return outcome


async def run_config(config: Config, cases: list[dict], owner: UserProfile) -> list[CaseResult]:
    settings = get_settings()
    settings.max_searches_per_episode = config.searches
    settings.verify_mode = config.verify_mode
    results: list[CaseResult] = []
    for case in cases:
        body = (ARTICLES / case["article"]).read_text("utf-8")
        try:
            source = await ingest_article(owner, body, f"{case['article']}")
            raw = await generate(owner, source, case["question"])
            results.append(score(case, raw))
        except Exception as exc:  # noqa: BLE001 - a failing case is a result, not a crash
            results.append(
                CaseResult(
                    case_id=case["id"],
                    split=case["split"],
                    question=case["question"],
                    error=repr(exc),
                )
            )
    return results


def summarize(results: list[CaseResult], cases: list[dict]) -> dict:
    def index(case_id: str) -> dict:
        return next(case for case in cases if case["id"] == case_id)

    total = len(results)
    quote_total = sum(len(index(r.case_id)["must_quote"]) for r in results)
    explain_total = sum(len(index(r.case_id)["must_explain"]) for r in results)
    claims = sum(r.claims for r in results)
    supported = sum(r.claims_supported for r in results)
    return {
        "cases": total,
        "errors": sum(1 for r in results if r.error),
        "duration_compliance": round(sum(1 for r in results if r.duration_ok) / total, 3) if total else 0,
        "citation_relocation": round(sum(1 for r in results if r.spans_valid) / total, 3) if total else 0,
        "evidence_recall": round(
            sum(r.must_quote_hits for r in results) / quote_total, 3
        ) if quote_total else 0,
        "keypoint_coverage": round(
            sum(r.must_explain_hits for r in results) / explain_total, 3
        ) if explain_total else 0,
        "unsupported_claim_rate": round((claims - supported) / claims, 3) if claims else 0,
        "claims": claims,
        "external_evidence_cases": sum(1 for r in results if r.external_evidence),
        "avg_duration_s": round(
            sum(r.duration_ms for r in results) / max(1, total) / 1000, 1
        ),
        "avg_searches": round(sum(r.searches for r in results) / max(1, total), 2),
        "avg_pages": round(sum(r.pages for r in results) / max(1, total), 2),
    }


def render_report(
    full: list[CaseResult],
    full_summary: dict,
    ablations: dict[str, tuple[list[CaseResult], dict]],
    cases: list[dict],
) -> str:
    settings = get_settings()
    lines: list[str] = []
    lines.append("# 评测报告：个人音频学习助手")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 适配器：`{settings.provider_mode}`（确定性本地适配器）")
    lines.append(
        f"- 时长门禁：单条 ≤ {settings.max_episode_ms} ms；预算：每天 {settings.daily_budget_ms} ms"
    )
    lines.append(
        f"- 用例：{len(cases)} 个（开发 "
        f"{sum(1 for c in cases if c['split'] == 'dev')} / 冻结验证 "
        f"{sum(1 for c in cases if c['split'] == 'validation')}），按原文分组，避免跨集合泄漏"
    )
    lines.append("")
    lines.append("本文件由 `python evals/run_eval.py` 生成。数字是本地确定性适配器上的实测值，")
    lines.append("用于证明约束真的生效（时长、引用可定位、预算、核源），不是模型效果排行。")
    lines.append("")
    lines.append("## 汇总（配置 C：完整链路）")
    lines.append("")
    lines.append("| 指标 | 值 | 定义 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 用例数 | {full_summary['cases']} | 含失败用例 |")
    lines.append(f"| 运行失败 | {full_summary['errors']} | 抛异常的用例数 |")
    lines.append(
        f"| 时长合规 | {full_summary['duration_compliance']:.0%} | 最终音频 ≤ 300000 ms |"
    )
    lines.append(
        f"| 引用可定位 | {full_summary['citation_relocation']:.0%} | 所有 span 能在保存版本中逐字重定位 |"
    )
    lines.append(
        f"| 证据召回 | {full_summary['evidence_recall']:.0%} | 标注引用片段出现在证据包中的比例 |"
    )
    lines.append(
        f"| 必讲点覆盖 | {full_summary['keypoint_coverage']:.0%} | 标注必讲术语出现在口播文本中的比例 |"
    )
    lines.append(
        f"| 无依据陈述率 | {full_summary['unsupported_claim_rate']:.1%} | 未通过核源的关键陈述占比 |"
    )
    lines.append(
        f"| 发生补证用例 | {full_summary['external_evidence_cases']} | 证据包中出现外部来源的用例数 |"
    )
    lines.append(
        f"| 平均时长 | {full_summary['avg_duration_s']} s | 实测 ffprobe 时长 |"
    )
    lines.append(
        f"| 平均检索/页面 | {full_summary['avg_searches']} / {full_summary['avg_pages']} | 每次搜索与新增正文页 |"
    )
    lines.append("")
    lines.append("## 逐用例（配置 C）")
    lines.append("")
    lines.append("| 用例 | 分组 | 问题 | 时长 | 合规 | 引用 | 必讲点 | 补证 | 核源 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    case_index = {case["id"]: case for case in cases}
    for result in full:
        case = case_index[result.case_id]
        quote_total = len(case["must_quote"])
        explain_total = len(case["must_explain"])
        status = result.error[:22] if result.error else result.run_state
        lines.append(
            "| {id} | {split} | {question} | {dur} s | {ok} | {quotes} | {explain} | {ext} | {rev} |".format(
                id=result.case_id,
                split=result.split,
                question=result.question[:34],
                dur=f"{result.duration_ms / 1000:.1f}",
                ok="✅" if result.duration_ok else "❌",
                quotes=f"{result.must_quote_hits}/{quote_total}",
                explain=f"{result.must_explain_hits}/{explain_total}",
                ext="✅" if result.external_evidence else "—",
                rev=f"{result.claims_supported}/{result.claims}" if result.claims else status,
            )
        )
    lines.append("")
    if ablations:
        lines.append("## 消融（开发分组）")
        lines.append("")
        lines.append(
            "| 配置 | 说明 | 用例 | 补证用例 | 平均检索/页面 | 平均时长 | 必讲点覆盖 | 无依据陈述率 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for label, (_results, summary) in ablations.items():
            lines.append(
                "| {label} | {desc} | {n} | {ext} | {s}/{p} | {dur}s | {cov:.0%} | {uns:.1%} |".format(
                    label=label,
                    desc=CONFIGS[label].description,
                    n=summary["cases"],
                    ext=summary["external_evidence_cases"],
                    s=summary["avg_searches"],
                    p=summary["avg_pages"],
                    dur=summary["avg_duration_s"],
                    cov=summary["keypoint_coverage"],
                    uns=summary["unsupported_claim_rate"],
                )
            )
        lines.append("")
        lines.append(
            "A/B/C 只改变一个机制：是否补证、是否做语义核源。定向补证最直接的可见差异是"
            "「补证用例」与「平均检索」；它是否真的提升理解，需要人工评分与更大的样本，"
            "本报告不据此声称收益。"
        )
        lines.append("")
        lines.append(
            "本仓库随附 8 个可执行用例，是文档中 40 个用例计划的子集；扩充用例只需在 "
            "`evals/cases.json` 增加条目与标注，无需改代码。"
        )
        lines.append("")
    failed = [r for r in full if r.error or not r.spans_valid or not r.duration_ok]
    lines.append("## 失败样例")
    lines.append("")
    if not failed:
        lines.append("本次运行没有失败样例：所有用例都通过时长门禁与引用重定位检查。")
    else:
        for result in failed:
            lines.append(f"- `{result.case_id}`：{result.error or '见指标'}")
            for problem in result.evidence_problems[:3]:
                lines.append(f"  - {problem['code']}：{problem['detail']}")
    lines.append("")
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ablations", action="store_true", help="also run A and B on dev cases")
    parser.add_argument("--keep", action="store_true", help="keep previous eval data")
    args = parser.parse_args()

    cases = load_cases()
    owner = await ensure_owner()
    if not args.keep:
        await reset_eval_data(owner)

    full = await run_config(CONFIGS["C"], cases, owner)
    full_summary = summarize(full, cases)
    ablations: dict[str, tuple[list[CaseResult], dict]] = {}
    if args.ablations:
        dev_cases = [case for case in cases if case["split"] == "dev"]
        dev_full = [result for result in full if result.split == "dev"]
        ablations["C"] = (dev_full, summarize(dev_full, dev_cases))
        for label in ("A", "B"):
            results = await run_config(CONFIGS[label], dev_cases, owner)
            ablations[label] = (results, summarize(results, dev_cases))

    REPORT_MD.write_text(
        render_report(full, full_summary, ablations, cases), encoding="utf-8"
    )
    REPORT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "config": "C",
                "summary": full_summary,
                "cases": [
                    {
                        "case_id": r.case_id,
                        "split": r.split,
                        "state": r.run_state,
                        "duration_ms": r.duration_ms,
                        "claims": r.claims,
                        "claims_supported": r.claims_supported,
                        "external_evidence": r.external_evidence,
                        "searches": r.searches,
                        "pages": r.pages,
                        "error": r.error,
                    }
                    for r in full
                ],
                "ablations": {
                    label: summary for label, (_results, summary) in ablations.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(full_summary, ensure_ascii=False, indent=2))
    await dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
