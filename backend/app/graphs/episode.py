"""The content graph (doc §5).

LangGraph owns *only* the parts of the pipeline that need a semantic decision: what the
episode is about, which gaps deserve a search, what evidence to keep, how to draft, and
whether the draft survives verification. Audio synthesis, measurement and publishing are
plain service functions — they are not decisions.

Every node returns structured conclusions plus evidence ids; no node stores or exposes
model reasoning. A structurally valid JSON answer is still rejected when a citation does
not resolve, a version is not readable by the owner, or a budget is exhausted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.adapters import Adapters
from app.adapters.base import AdapterError, Usage
from app.adapters.fetch import UrlFetcher
from app.config import Settings
from app.db import session_scope
from app.models import (
    ArtifactKind,
    EpisodeRevision,
    Phase,
    ProviderCall,
    RevisionStatus,
)
from app.schemas.content import (
    Claim,
    ClaimVerdict,
    EvidenceItem,
    EvidenceOrigin,
    EvidencePack,
    FocusResult,
    GapPlan,
    Relation,
    ScriptDoc,
    ScriptSegment,
)
from app.services import artifacts, budget, calls, runs
from app.services.chunking import build_blocks
from app.services.ingest import IngestError, ingest_external_page
from app.services.retrieval import (
    expand_neighbours,
    fuse_channels,
    load_chunks,
    load_primary_version,
    select_quotes,
)
from app.services.textproc import estimate_tokens, lexemes_for, split_sentences
from app.services.validation import (
    merge_review,
    validate_evidence_pack,
    validate_script_structure,
)

FOCUS_PROMPT_VERSION = "focus-v2"
GAP_PROMPT_VERSION = "gap-v2"
DRAFT_PROMPT_VERSION = "draft-v2"
VERIFY_PROMPT_VERSION = "verify-v2"


class EpisodeState(TypedDict, total=False):
    """Mirrors the document's state contract, plus two internal retrieval fields."""

    run_id: str
    attempt_id: str
    episode_id: str
    primary_version_id: str
    preference_snapshot: dict
    core_question: str
    critical_points: list[dict]
    gaps: list[dict]
    evidence_artifact_id: str | None
    script_artifact_id: str | None
    queries_used: int
    pages_fetched: int
    fact_revisions: int
    tokens_used: int
    deadline_at: str
    unresolved: list[dict]
    stop_reason: str | None
    # internal
    document_mode: str
    external_version_ids: list[str]
    review: dict
    route: str


@dataclass(slots=True)
class EpisodeRuntime:
    settings: Settings
    adapters: Adapters
    owner_id: uuid.UUID
    run_id: uuid.UUID
    attempt_id: uuid.UUID
    episode_id: uuid.UUID
    revision_id: uuid.UUID
    primary_version_id: uuid.UUID
    question: str
    preference_snapshot: dict = field(default_factory=dict)
    target_seconds: int = 240
    event_hook: Any = None

    # -- helpers ----------------------------------------------------------
    async def phase(self, phase: Phase, detail: dict | None = None) -> None:
        async with session_scope() as session:
            await runs.set_phase(
                session, run_id=self.run_id, attempt_id=self.attempt_id, phase=phase, detail=detail
            )

    async def event(self, event_type: str, payload: dict) -> None:
        from app.services.events import append_event

        async with session_scope() as session:
            await append_event(session, self.run_id, event_type, payload)

    async def _reserve(self, estimate: int) -> int:
        return await budget.reserve_tokens(self.run_id, estimate)

    async def _settle(self, reserved: int, usage: Usage) -> None:
        await budget.settle_tokens(self.run_id, reserved, usage)

    async def persist_evidence(self, pack: EvidencePack) -> uuid.UUID:
        key = artifacts.fingerprint(
            {
                "primary": str(self.primary_version_id),
                "question": self.question,
                "items": [
                    [str(i.source_version_id), str(i.chunk_id), i.span_start, i.span_end]
                    for i in pack.items
                ],
                "evidence_v": 1,
            }
        )
        async with session_scope() as session:
            await runs.require_authority(
                session, run_id=self.run_id, attempt_id=self.attempt_id
            )
            artifact, created = await artifacts.upsert(
                session,
                owner_id=self.owner_id,
                kind=ArtifactKind.EVIDENCE,
                input_fingerprint=key,
                payload=pack.model_dump(mode="json"),
            )
            await artifacts.attach_to_revision(session, self.revision_id, ArtifactKind.EVIDENCE, artifact.id)
            if created:
                from app.services.events import append_event

                await append_event(
                    session,
                    self.run_id,
                    "evidence.pack",
                    {"artifact_id": str(artifact.id), "items": len(pack.items)},
                )
        return artifact.id

    async def persist_script(self, script: ScriptDoc, fact_revision: int) -> uuid.UUID:
        key = artifacts.fingerprint(
            {
                "episode": str(self.episode_id),
                "revision": self.revision_id.hex,
                "fact_revision": fact_revision,
                "prompt": DRAFT_PROMPT_VERSION,
                "spoken": script.spoken_text,
                "claims": [[c.claim_id, c.text, c.review.value] for c in script.claims],
            }
        )
        async with session_scope() as session:
            await runs.require_authority(
                session, run_id=self.run_id, attempt_id=self.attempt_id
            )
            artifact, created = await artifacts.upsert(
                session,
                owner_id=self.owner_id,
                kind=ArtifactKind.SCRIPT,
                input_fingerprint=key,
                payload=script.model_dump(mode="json"),
            )
            revision = await session.get(EpisodeRevision, self.revision_id, with_for_update=True)
            revision.script_id = artifact.id
            revision.status = RevisionStatus.SCRIPT_READY
            if created:
                from app.services.events import append_event

                await append_event(
                    session,
                    self.run_id,
                    "script.ready",
                    {"artifact_id": str(artifact.id), "chars": script.char_count},
                )
        return artifact.id

    async def load_pack(self, artifact_id: uuid.UUID) -> EvidencePack:
        async with session_scope() as session:
            artifact = await artifacts.get(session, artifact_id)
            return EvidencePack.model_validate(artifact.payload)

    async def load_script(self, artifact_id: uuid.UUID) -> ScriptDoc:
        async with session_scope() as session:
            artifact = await artifacts.get(session, artifact_id)
            return ScriptDoc.model_validate(artifact.payload)

    async def update_review(self, verdicts: list[ClaimVerdict], blocking: list[str]) -> None:
        async with session_scope() as session:
            revision = await session.get(EpisodeRevision, self.revision_id, with_for_update=True)
            snapshot = dict(revision.config_snapshot or {})
            snapshot["review"] = {
                "verdicts": [v.model_dump(mode="json") for v in verdicts],
                "blocking": blocking,
            }
            revision.config_snapshot = snapshot


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def build_graph(runtime: EpisodeRuntime):
    settings = runtime.settings
    adapters = runtime.adapters

    async def focus(state: EpisodeState) -> dict:
        await runtime.phase(Phase.RESEARCH, {"step": "focus"})
        async with session_scope() as session:
            pair = await load_primary_version(
                session, owner_id=runtime.owner_id, version_id=runtime.primary_version_id
            )
            if pair is None:
                raise IngestError("primary_version_not_readable", str(runtime.primary_version_id))
            version, _source = pair
            body = version.body_text
        blocks = build_blocks(body)
        outline, is_full = _outline(body, blocks, settings.document_budget_chars)
        candidates = _candidates(blocks, runtime.question)
        payload = {
            "question": runtime.question,
            "outline": outline[:6000],
            "critical_candidates": candidates,
            "question_terms": [t for t in lexemes_for(runtime.question).split() if len(t) >= 2][:8],
            "document_terms": sorted(
                {term for block in blocks for term in lexemes_for(block.text).split()}
            )[:400],
            "document_mode": "full" if is_full else "outline",
            "prompt_version": FOCUS_PROMPT_VERSION,
        }
        reserved = await runtime._reserve(estimate_tokens(str(payload)))
        response = await adapters.llm.complete_json(task="focus", payload=payload)
        await runtime._settle(reserved, response.usage)
        result = FocusResult.model_validate(response.data)
        return {
            "core_question": result.core_question,
            "critical_points": [p.model_dump() for p in result.critical_points],
            "document_mode": result.document_mode,
            "tokens_used": response.usage.tokens_in + response.usage.tokens_out,
        }

    async def gap_plan(state: EpisodeState) -> dict:
        await runtime.phase(Phase.RESEARCH, {"step": "gap_plan"})
        uncovered = await _uncovered_points(runtime, state.get("critical_points", []))
        payload = {
            "core_question": state.get("core_question", ""),
            "critical_points": [p["point"] for p in state.get("critical_points", [])],
            "uncovered_points": uncovered,
            "prompt_version": GAP_PROMPT_VERSION,
        }
        reserved = await runtime._reserve(estimate_tokens(str(payload)))
        response = await adapters.llm.complete_json(task="gap_plan", payload=payload)
        await runtime._settle(reserved, response.usage)
        plan = GapPlan.model_validate(response.data)
        unresolved = list(state.get("unresolved", []))
        gaps = [g.model_dump() for g in plan.gaps]
        if gaps and settings.provider_mode == "cloud" and not settings.tavily_api_key:
            unresolved.extend({"kind": "gap", "reason": "search_not_configured", "question": gap["question"]} for gap in gaps)
            gaps = []
        if gaps and not await budget.can_consume(runtime.run_id, budget.KEY_SEARCHES):
            unresolved.append(
                {"kind": "gap", "reason": "search_budget_exhausted", "question": gaps[0]["question"]}
            )
            gaps = []
        return {"gaps": gaps, "unresolved": unresolved}

    async def search_fetch(state: EpisodeState) -> dict:
        await runtime.phase(Phase.RESEARCH, {"step": "search_fetch"})
        unresolved = list(state.get("unresolved", []))
        external: list[str] = [str(x) for x in state.get("external_version_ids", [])]
        pages_fetched = int(state.get("pages_fetched", 0))
        queries_used = int(state.get("queries_used", 0))
        fetcher = UrlFetcher(settings, fixtures_dir=settings.offline_fixtures_dir)
        known_urls = await _known_version_urls(runtime)

        for gap in state.get("gaps", []):
            if not await budget.can_consume(runtime.run_id, budget.KEY_SEARCHES):
                unresolved.append({"kind": "gap", "reason": "budget", "question": gap["question"]})
                break
            await budget.consume(runtime.run_id, budget.KEY_SEARCHES)
            queries_used += 1
            async with session_scope() as session:
                call = await calls.begin_call(
                    session,
                    run_id=runtime.run_id,
                    attempt_id=runtime.attempt_id,
                    stage="search",
                    provider=getattr(adapters.search, "name", "search"),
                    input_fingerprint=artifacts.fingerprint(gap["question"]),
                )
            try:
                results = await adapters.search.search(
                    gap["question"], max_results=settings.search_max_results
                )
            except AdapterError as exc:
                async with session_scope() as session:
                    await calls.fail_call(session, await session.get(ProviderCall, call.id), code=exc.code)
                unresolved.append(
                    {"kind": "gap", "reason": exc.code, "question": gap["question"]}
                )
                continue
            async with session_scope() as session:
                await calls.succeed_call(
                    session,
                    await session.get(ProviderCall, call.id),
                    request_id=f"search_{queries_used}",
                    usage={"results": len(results)},
                )
            if not results:
                unresolved.append({"kind": "gap", "reason": "no_results", "question": gap["question"]})

            for result in results:
                if pages_fetched >= settings.max_pages_per_episode:
                    unresolved.append(
                        {"kind": "gap", "reason": "page_budget", "question": gap["question"]}
                    )
                    break
                canonical = result.url
                if canonical in known_urls:
                    if canonical not in external:
                        external.append(known_urls[canonical])
                    continue
                if not await budget.can_consume(runtime.run_id, budget.KEY_PAGES):
                    break
                await budget.consume(runtime.run_id, budget.KEY_PAGES)
                pages_fetched += 1
                try:
                    page = await fetcher.fetch(result.url)
                except AdapterError as exc:
                    unresolved.append(
                        {"kind": "page", "reason": exc.code, "url": result.url}
                    )
                    continue
                async with session_scope() as session:
                    try:
                        version = await ingest_external_page(
                            session,
                            settings=settings,
                            adapters=adapters,
                            owner_id=runtime.owner_id,
                            page=page,
                        )
                        external.append(str(version.id))
                        known_urls[canonical] = str(version.id)
                    except (IngestError, AdapterError) as exc:
                        code = getattr(exc, "code", "external_ingest_failed")
                        unresolved.append({"kind": "page", "reason": code, "url": result.url})
        return {
            "external_version_ids": external,
            "pages_fetched": pages_fetched,
            "queries_used": queries_used,
            "unresolved": unresolved,
        }

    async def evidence(state: EpisodeState) -> dict:
        await runtime.phase(Phase.RESEARCH, {"step": "evidence"})
        version_ids = [runtime.primary_version_id] + [
            uuid.UUID(v) for v in state.get("external_version_ids", [])
        ]
        question = state.get("core_question") or runtime.question or "核心结论"
        embedding = (await adapters.embedding.embed([question]))[0]
        async with session_scope() as session:
            hits = await fuse_channels(
                session,
                owner_id=runtime.owner_id,
                version_ids=version_ids,
                question=question,
                embedding=embedding,
            )
            loaded = await load_chunks(session, [h.chunk_id for h in hits])
            loaded = await expand_neighbours(session, loaded)

        ordered = [loaded[h.chunk_id] for h in hits if h.chunk_id in loaded]
        extras = [c for cid, c in loaded.items() if cid not in {h.chunk_id for h in hits}]
        items: list[EvidenceItem] = []
        used_chars = 0
        for chunk in ordered + extras:
            origin = (
                EvidenceOrigin.PRIMARY
                if chunk.source_version_id == runtime.primary_version_id
                else EvidenceOrigin.EXTERNAL
            )
            for start, end, quote in select_quotes(chunk, state.get("core_question", "")):
                if not quote.strip():
                    continue
                items.append(
                    EvidenceItem(
                        source_version_id=chunk.source_version_id,
                        chunk_id=chunk.id,
                        span_start=start,
                        span_end=end,
                        quote=quote,
                        relation=Relation.SUPPORTS,
                        origin=origin,
                        retrieved_for=state.get("core_question", ""),
                    )
                )
                used_chars += len(quote)
                if used_chars >= settings.evidence_budget_chars:
                    break
            if used_chars >= settings.evidence_budget_chars:
                break

        pack = EvidencePack(
            pack_id=f"pack_{uuid.uuid4().hex[:12]}",
            primary_version_id=runtime.primary_version_id,
            core_question=state.get("core_question", ""),
            items=items,
            gaps=state.get("gaps", []),
            unresolved=state.get("unresolved", []),
            queries_used=int(state.get("queries_used", 0)),
            pages_fetched=int(state.get("pages_fetched", 0)),
        )
        async with session_scope() as session:
            problems = await validate_evidence_pack(
                session, owner_id=runtime.owner_id, pack=pack
            )
        if problems:
            bad = {p.index for p in problems if p.index >= 0}
            items = [item for index, item in enumerate(items) if index not in bad]
            pack = pack.model_copy(update={"items": items})
            await runtime.event(
                "evidence.rejected", {"problems": [p.as_dict() for p in problems]}
            )
        if not pack.items:
            return {
                "evidence_artifact_id": None,
                "unresolved": [
                    *state.get("unresolved", []),
                    {"kind": "evidence", "reason": "no_relocatable_evidence"},
                ],
                "stop_reason": "evidence_blocked",
            }
        artifact_id = await runtime.persist_evidence(pack)
        return {"evidence_artifact_id": str(artifact_id)}

    async def draft(state: EpisodeState) -> dict:
        await runtime.phase(Phase.SCRIPT, {"step": "draft"})
        pack = await runtime.load_pack(uuid.UUID(state["evidence_artifact_id"]))
        target_chars = int(runtime.target_seconds * settings.spoken_chars_per_second)
        payload = {
            "core_question": state.get("core_question", ""),
            "critical_points": [p["point"] for p in state.get("critical_points", [])],
            "preference": state.get("preference_snapshot", {}),
            "target_chars": target_chars,
            "target_seconds": runtime.target_seconds,
            "evidence": [
                {
                    "quote": item.quote,
                    "origin": item.origin.value,
                    "ref": {
                        "source_version_id": str(item.source_version_id),
                        "chunk_id": str(item.chunk_id),
                        "span_start": item.span_start,
                        "span_end": item.span_end,
                        "quote": item.quote,
                        "relation": item.relation.value,
                    },
                }
                for item in pack.items
            ],
            "prompt_version": DRAFT_PROMPT_VERSION,
            "fact_revision": int(state.get("fact_revisions", 0)),
        }
        if state.get("fact_revisions") and state.get("script_artifact_id"):
            previous = await runtime.load_script(uuid.UUID(state["script_artifact_id"]))
            payload["previous_script"] = previous.model_dump(mode="json")
            payload["review_feedback"] = state.get("review", {})
        reserved = await runtime._reserve(estimate_tokens(str(payload)))
        response = await adapters.llm.complete_json(task="draft", payload=payload)
        await runtime._settle(reserved, response.usage)
        script = _assemble_script(
            response.data, runtime=runtime, pack=pack, prompt_version=DRAFT_PROMPT_VERSION
        )
        problems = validate_script_structure(script, pack)
        if problems:
            await runtime.event("script.rejected", {"problems": [p.as_dict() for p in problems]})
            return {
                "stop_reason": "review_required",
                "route": "blocked",
                "unresolved": [
                    *state.get("unresolved", []),
                    {"kind": "script", "reason": "structural", "problems": [p.as_dict() for p in problems]},
                ],
            }
        artifact_id = await runtime.persist_script(script, int(state.get("fact_revisions", 0)))
        return {
            "script_artifact_id": str(artifact_id),
            "tokens_used": int(state.get("tokens_used", 0))
            + response.usage.tokens_in
            + response.usage.tokens_out,
        }

    async def verify(state: EpisodeState) -> dict:
        await runtime.phase(Phase.SCRIPT, {"step": "verify"})
        if not state.get("script_artifact_id") or not state.get("evidence_artifact_id"):
            return {"stop_reason": "review_required"}
        pack = await runtime.load_pack(uuid.UUID(state["evidence_artifact_id"]))
        script = await runtime.load_script(uuid.UUID(state["script_artifact_id"]))
        structural = validate_script_structure(script, pack)
        payload = {
            "core_question": state.get("core_question", ""),
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "text": claim.text,
                    "kind": claim.kind.value,
                    "critical": claim.critical,
                    "quote": claim.evidence[0].quote if claim.evidence else "",
                }
                for claim in script.claims
            ],
            "evidence": [item.quote for item in pack.items][:12],
            "prompt_version": VERIFY_PROMPT_VERSION,
        }
        if settings.verify_mode == "structural":
            # Evaluation baseline: citation relocation only, no semantic check.
            verdicts = [
                ClaimVerdict(
                    claim_id=claim.claim_id,
                    verdict="SUPPORTED" if claim.evidence else "NOT_FOUND",
                    reason="structural_only",
                )
                for claim in script.claims
            ]
        else:
            reserved = await runtime._reserve(estimate_tokens(str(payload)))
            response = await adapters.llm.complete_json(task="verify", payload=payload)
            await runtime._settle(reserved, response.usage)
            verdicts = [
                ClaimVerdict.model_validate(row) for row in response.data.get("verdicts", [])
            ]
        updated, critical_ok, blocking = merge_review(
            script, [v.model_dump(mode="json") for v in verdicts]
        )
        if structural:
            blocking = sorted({*blocking, *[p.detail for p in structural]})
            critical_ok = False
        await runtime.update_review(verdicts, blocking)
        await runtime.event(
            "script.verified",
            {
                "critical_ok": critical_ok,
                "blocking": blocking,
                "verdicts": len(verdicts),
                "structural": [p.as_dict() for p in structural],
            },
        )
        fact_revisions = int(state.get("fact_revisions", 0))
        if critical_ok:
            reviewed_id = await runtime.persist_script(updated, fact_revisions)
            return {"script_artifact_id": str(reviewed_id), "review": {"verdicts": len(verdicts), "blocking": []}, "route": "done"}
        if fact_revisions < settings.max_fact_revisions and structural == []:
            return {
                "review": {"blocking": blocking, "verdicts": [v.model_dump(mode="json") for v in verdicts]},
                "route": "revise",
                "unresolved": [
                    *state.get("unresolved", []),
                    {"kind": "claim", "reason": "blocking", "claims": blocking},
                ],
            }
        await runtime.update_review(verdicts, blocking)
        return {"review": {"blocking": blocking}, "route": "blocked", "stop_reason": "review_required"}

    async def revise(state: EpisodeState) -> dict:
        await runtime.phase(Phase.SCRIPT, {"step": "revise"})
        try:
            await budget.consume(runtime.run_id, budget.KEY_FACT_REVISIONS)
        except budget.BudgetExceeded:
            return {
                "stop_reason": "review_required",
                "route": "blocked",
                "fact_revisions": settings.max_fact_revisions,
            }
        fact_revisions = int(state.get("fact_revisions", 0)) + 1
        result = await draft({**state, "fact_revisions": fact_revisions, "route": "revise"})
        result["fact_revisions"] = fact_revisions
        return result

    def after_gap(state: EpisodeState) -> str:
        return "search" if state.get("gaps") else "evidence"

    def after_evidence(state: EpisodeState) -> str:
        return "blocked" if state.get("stop_reason") else "draft"

    def after_verify(state: EpisodeState) -> str:
        if state.get("stop_reason") == "review_required":
            return "blocked"
        return "revise" if state.get("route") == "revise" else "done"

    graph = StateGraph(EpisodeState)
    graph.add_node("focus", focus)
    graph.add_node("gap_plan", gap_plan)
    graph.add_node("search_fetch", search_fetch)
    graph.add_node("evidence", evidence)
    graph.add_node("draft", draft)
    graph.add_node("verify", verify)
    graph.add_node("revise", revise)

    graph.add_edge(START, "focus")
    graph.add_edge("focus", "gap_plan")
    graph.add_conditional_edges(
        "gap_plan", after_gap, {"search": "search_fetch", "evidence": "evidence"}
    )
    graph.add_edge("search_fetch", "evidence")
    graph.add_conditional_edges(
        "evidence", after_evidence, {"draft": "draft", "blocked": END}
    )
    graph.add_edge("draft", "verify")
    graph.add_conditional_edges(
        "verify", after_verify, {"revise": "revise", "done": END, "blocked": END}
    )
    graph.add_edge("revise", "verify")
    return graph.compile()


class ScriptInvalid(RuntimeError):
    def __init__(self, problems) -> None:
        super().__init__("; ".join(p.code for p in problems))
        self.problems = problems


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _outline(body: str, blocks, budget_chars: int) -> tuple[str, bool]:
    if len(body) <= budget_chars:
        return body, True
    parts: list[str] = []
    used = 0
    for block in blocks:
        snippet = block.text[:220]
        line = f"[{block.paragraph_id}] {block.section_path or '正文'}：{snippet}"
        if used + len(line) > budget_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts), False


def _candidates(blocks, question: str, limit: int = 6) -> list[dict]:
    terms = set(lexemes_for(question).split())
    scored: list[tuple[float, dict]] = []
    for block in blocks:
        sentence = split_sentences(block.text)
        if not sentence:
            continue
        text = sentence[0][:160]
        block_terms = set(lexemes_for(text).split())
        score = len(terms & block_terms) if terms else 0.0
        score += 0.2 if block.section_path else 0.0
        scored.append(
            (
                score,
                {
                    "text": text,
                    "reason": block.section_path or block.paragraph_id,
                    "paragraph_id": block.paragraph_id,
                    "matched": score > 0,
                },
            )
        )
    scored.sort(key=lambda pair: (-pair[0],))
    chosen = scored[:limit]
    if len(chosen) < 3:
        chosen = scored[:limit]
    return [item for _score, item in chosen]


async def _uncovered_points(runtime: EpisodeRuntime, points: list[dict]) -> list[str]:
    """Decide whether the primary source actually answers each critical point.

    A point counts as covered only when some chunk shares at least two query terms and at
    least one of them is *distinctive* for this document. Without the distinctive-term
    rule, generic words like "任务" or "执行" make every question look answered and the
    gap planner never fires.
    """
    from sqlalchemy import select

    from app.models import Chunk

    async with session_scope() as session:
        rows = await session.execute(
            select(Chunk.text).where(
                Chunk.owner_id == runtime.owner_id,
                Chunk.source_version_id == runtime.primary_version_id,
            )
        )
        chunk_terms = [set(lexemes_for(text).split()) for text in rows.scalars()]
    total = max(len(chunk_terms), 1)
    frequencies: dict[str, int] = {}
    for terms in chunk_terms:
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1

    def distinctive(term: str) -> bool:
        return frequencies.get(term, 0) / total <= 0.34 and len(term) >= 2

    uncovered: list[str] = []
    for entry in points:
        point = entry.get("point", "") if isinstance(entry, dict) else str(entry)
        if isinstance(entry, dict) and entry.get("matched") is False:
            uncovered.append(point)
            continue
        terms = set(lexemes_for(point).split())
        if not terms:
            continue
        covered = False
        for terms_in_chunk in chunk_terms:
            shared = terms & terms_in_chunk
            if len(shared) >= 2 and any(distinctive(term) for term in shared):
                covered = True
                break
        if not covered:
            uncovered.append(point)
    return uncovered


async def _known_version_urls(runtime: EpisodeRuntime) -> dict[str, str]:
    from sqlalchemy import select

    from app.models import Source, SourceVersion

    async with session_scope() as session:
        rows = await session.execute(
            select(Source.canonical_url, SourceVersion.id)
            .join(SourceVersion, SourceVersion.source_id == Source.id)
            .where(Source.owner_id == runtime.owner_id, Source.canonical_url.is_not(None))
            .order_by(SourceVersion.fetched_at.desc())
        )
        out: dict[str, str] = {}
        for url, version_id in rows:
            out.setdefault(url, str(version_id))
        return out


def _assemble_script(
    data: dict, *, runtime: EpisodeRuntime, pack: EvidencePack, prompt_version: str
) -> ScriptDoc:
    segments = [ScriptSegment.model_validate(row) for row in data.get("segments", [])]
    claims = [Claim.model_validate(row) for row in data.get("claims", [])]
    spoken = "\n".join(segment.spoken_text for segment in segments).strip()
    if not spoken:
        raise ScriptInvalid([])
    return ScriptDoc(
        episode_id=runtime.episode_id,
        revision_id=runtime.revision_id,
        core_question=data.get("core_question") or "",
        one_line_answer=data.get("one_line_answer") or "",
        segments=segments,
        claims=claims,
        spoken_text=spoken,
        char_count=len(spoken),
        revision_no=int(data.get("revision_no", 1)),
        prompt_version=prompt_version,
        model=getattr(runtime.adapters.llm, "name", "llm"),
    )


async def run_content_graph(runtime: EpisodeRuntime) -> EpisodeState:
    graph = build_graph(runtime)
    initial: EpisodeState = {
        "run_id": str(runtime.run_id),
        "attempt_id": str(runtime.attempt_id),
        "episode_id": str(runtime.episode_id),
        "primary_version_id": str(runtime.primary_version_id),
        "preference_snapshot": runtime.preference_snapshot,
        "core_question": runtime.question,
        "critical_points": [],
        "gaps": [],
        "evidence_artifact_id": None,
        "script_artifact_id": None,
        "queries_used": 0,
        "pages_fetched": 0,
        "fact_revisions": 0,
        "tokens_used": 0,
        "unresolved": [],
        "stop_reason": None,
        "document_mode": "full",
        "external_version_ids": [],
        "review": {},
        "route": "",
    }
    final = await graph.ainvoke(initial, {"recursion_limit": 40})
    if final.get("stop_reason") == "review_required":
        async with session_scope() as session:
            revision = await session.get(EpisodeRevision, runtime.revision_id, with_for_update=True)
            revision.status = RevisionStatus.BLOCKED
    return final


__all__ = ["EpisodeRuntime", "EpisodeState", "build_graph", "run_content_graph"]
