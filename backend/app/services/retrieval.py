"""Hybrid retrieval with reciprocal rank fusion (doc §4.2, §4.3).

Two channels over the same table: a PostgreSQL ``simple`` tsvector channel and a
pgvector cosine channel. Each returns at most 12 rows; scores are fused by *rank*, never
by mixing raw magnitudes, and the top 8 are expanded with their neighbouring paragraphs
before the evidence budget is applied.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Source, SourceVersion
from app.schemas.content import RetrievalHit
from app.services.textproc import lexemes_for, query_terms, split_sentences

_SAFE_TERM = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
CHANNEL_LIMIT = 12
RRF_K = 60
FINAL_TOP = 8


@dataclass(slots=True)
class LoadedChunk:
    id: uuid.UUID
    source_version_id: uuid.UUID
    ordinal: int
    text: str
    start_char: int
    end_char: int
    section_path: str
    page_no: int | None
    paragraph_id: str


def rrf_fuse(
    channels: dict[str, list[uuid.UUID]], *, k: int = RRF_K, top: int = FINAL_TOP
) -> list[tuple[uuid.UUID, float, dict[str, int]]]:
    """``score = sum(1 / (k + rank))`` over channels where the item is present."""
    scores: dict[uuid.UUID, float] = {}
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for name, ids in channels.items():
        for position, item in enumerate(ids, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
            ranks.setdefault(item, {})[name] = position
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], str(pair[0])))
    return [(item, score, ranks[item]) for item, score in ordered[:top]]


def build_tsquery(question: str, max_terms: int = 6) -> str:
    terms: list[str] = []
    for term in query_terms(question, limit=max_terms):
        cleaned = _SAFE_TERM.sub("", term)
        if len(cleaned) < 2 and not re.search(r"[\u4e00-\u9fff]", cleaned):
            continue
        if cleaned and cleaned not in terms:
            terms.append(cleaned)
    if not terms:
        return ""
    return " & ".join(terms)


async def keyword_channel(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    question: str,
    limit: int = CHANNEL_LIMIT,
) -> list[uuid.UUID]:
    tsquery = build_tsquery(question)
    if not tsquery:
        return []
    from sqlalchemy import func

    query = func.to_tsquery("simple", tsquery)
    rank = func.ts_rank_cd(Chunk.lexemes_tsv, query).label("rank")
    stmt = (
        select(Chunk.id)
        .where(
            Chunk.owner_id == owner_id,
            Chunk.source_version_id.in_(version_ids),
            Chunk.lexemes_tsv.op("@@")(query),
        )
        .order_by(desc(rank), Chunk.ordinal)
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return list(rows.scalars())


async def semantic_channel(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    embedding: list[float],
    limit: int = CHANNEL_LIMIT,
) -> list[uuid.UUID]:
    distance = Chunk.embedding.cosine_distance(embedding)
    stmt = (
        select(Chunk.id)
        .where(
            Chunk.owner_id == owner_id,
            Chunk.source_version_id.in_(version_ids),
            Chunk.embedding.is_not(None),
        )
        .order_by(distance, Chunk.ordinal)
        .limit(limit)
    )
    rows = await session.execute(stmt)
    return list(rows.scalars())


async def fuse_channels(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    version_ids: list[uuid.UUID],
    question: str,
    embedding: list[float],
    top: int = FINAL_TOP,
) -> list[RetrievalHit]:
    """Both channels + RRF; the single entry point used by the graph."""
    keywords = await keyword_channel(
        session, owner_id=owner_id, version_ids=version_ids, question=question
    )
    semantic = await semantic_channel(
        session, owner_id=owner_id, version_ids=version_ids, embedding=embedding
    )
    fused = rrf_fuse({"keyword": keywords, "semantic": semantic}, top=top)
    ids = [item for item, _score, _ranks in fused]
    versions = await chunk_versions(session, ids)
    return [
        RetrievalHit(
            chunk_id=item,
            source_version_id=versions.get(item, uuid.UUID(int=0)),
            score=score,
            channels=list(ranks),
            rank_keyword=ranks.get("keyword"),
            rank_semantic=ranks.get("semantic"),
        )
        for item, score, ranks in fused
    ]


async def chunk_versions(
    session: AsyncSession, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not chunk_ids:
        return {}
    rows = await session.execute(
        select(Chunk.id, Chunk.source_version_id).where(Chunk.id.in_(chunk_ids))
    )
    return {row[0]: row[1] for row in rows}


async def load_chunks(
    session: AsyncSession, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, LoadedChunk]:
    if not chunk_ids:
        return {}
    rows = await session.execute(select(Chunk).where(Chunk.id.in_(chunk_ids)))
    out: dict[uuid.UUID, LoadedChunk] = {}
    for chunk in rows.scalars():
        out[chunk.id] = LoadedChunk(
            id=chunk.id,
            source_version_id=chunk.source_version_id,
            ordinal=chunk.ordinal,
            text=chunk.text,
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            section_path=chunk.section_path,
            page_no=chunk.page_no,
            paragraph_id=chunk.paragraph_id,
        )
    return out


async def expand_neighbours(
    session: AsyncSession, chunks: dict[uuid.UUID, LoadedChunk]
) -> dict[uuid.UUID, LoadedChunk]:
    """Add the paragraph before/after each hit (the document warns about missing premises)."""
    if not chunks:
        return {}
    wanted: set[tuple[uuid.UUID, int]] = set()
    for chunk in chunks.values():
        for ordinal in (chunk.ordinal - 1, chunk.ordinal + 1):
            if ordinal >= 0:
                wanted.add((chunk.source_version_id, ordinal))
    if not wanted:
        return chunks
    version_ids = {v for v, _o in wanted}
    ordinals = {o for _v, o in wanted}
    rows = await session.execute(
        select(Chunk).where(
            Chunk.source_version_id.in_(version_ids), Chunk.ordinal.in_(ordinals)
        )
    )
    for chunk in rows.scalars():
        if (chunk.source_version_id, chunk.ordinal) in wanted and chunk.id not in chunks:
            chunks[chunk.id] = LoadedChunk(
                id=chunk.id,
                source_version_id=chunk.source_version_id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                start_char=chunk.start_char,
                end_char=chunk.end_char,
                section_path=chunk.section_path,
                page_no=chunk.page_no,
                paragraph_id=chunk.paragraph_id,
            )
    return dict(sorted(chunks.items(), key=lambda kv: (str(kv[1].source_version_id), kv[1].ordinal)))


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sentence in split_sentences(text):
        index = text.find(sentence, cursor)
        if index < 0:
            continue
        spans.append((index, index + len(sentence)))
        cursor = index + len(sentence)
    return spans


def select_quote(chunk: LoadedChunk, question: str, max_chars: int = 260) -> tuple[int, int, str]:
    """Pick the best sentence inside a chunk and return *absolute* character offsets."""
    spans = sentence_spans(chunk.text)
    if not spans:
        end = min(len(chunk.text), max_chars)
        return chunk.start_char, chunk.start_char + end, chunk.text[:end]
    terms = set(lexemes_for(question).split())
    best = spans[0]
    best_score = -1.0
    for start, end in spans:
        if end - start > max_chars:
            continue
        candidate = chunk.text[start:end]
        candidate_terms = set(lexemes_for(candidate).split())
        if not candidate_terms:
            continue
        score = len(terms & candidate_terms) / (len(terms) ** 0.5 or 1)
        if score > best_score:
            best_score = score
            best = (start, end)
    start, end = best
    quote = chunk.text[start:end]
    if len(quote) > max_chars:
        cut = quote.rfind("，", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars - 1
        quote = quote[: cut + 1]
        end = start + len(quote)
    return chunk.start_char + start, chunk.start_char + end, quote


def build_outline(
    body: str, blocks, budget_chars: int
) -> tuple[str, bool]:
    """Bounded outline: headings plus the first sentences of each block.

    Returns ``(outline_text, is_full_document)``. A short document whose whole body fits
    the budget is passed through verbatim with paragraph ids.
    """
    if len(body) <= budget_chars:
        return body, True
    parts: list[str] = []
    used = 0
    for block in blocks:
        head = block.section_path or "正文"
        snippet = block.text[:220]
        line = f"[{block.paragraph_id}] {head}：{snippet}"
        if used + len(line) > budget_chars:
            break
        parts.append(line)
        used += len(line)
    return "\n".join(parts), False


async def load_primary_version(
    session: AsyncSession, *, owner_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[SourceVersion, Source] | None:
    row = await session.execute(
        select(SourceVersion, Source)
        .join(Source, Source.id == SourceVersion.source_id)
        .where(SourceVersion.id == version_id, Source.owner_id == owner_id)
    )
    pair = row.first()
    return (pair[0], pair[1]) if pair else None

def select_quotes(
    chunk: LoadedChunk, question: str, max_chars: int = 260, max_items: int = 2
) -> list[tuple[int, int, str]]:
    """Best sentence plus one neighbouring sentence, each returned with absolute offsets.

    Retrieving a chunk is not the same as citing it: the pipeline needs the sentence that
    answers the question *and* the sentence that carries its premise, which is why the
    document asks for the parent paragraph to be restored after retrieval.
    """
    spans = sentence_spans(chunk.text)
    if not spans:
        end = min(len(chunk.text), max_chars)
        return [(chunk.start_char, chunk.start_char + end, chunk.text[:end])]
    terms = set(lexemes_for(question).split())
    scored: list[tuple[float, int, int]] = []
    for start, end in spans:
        if end - start > max_chars:
            continue
        candidate = chunk.text[start:end]
        candidate_terms = set(lexemes_for(candidate).split())
        if not candidate_terms:
            continue
        score = len(terms & candidate_terms) / (len(terms) ** 0.5 or 1)
        scored.append((score, start, end))
    if not scored:
        start, end = spans[0]
        return [(chunk.start_char + start, chunk.start_char + end, chunk.text[start:end])]
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, int, str]] = []
    for _score, start, end in scored[:max_items]:
        quote = chunk.text[start:end]
        chosen.append((chunk.start_char + start, chunk.start_char + end, quote))
    return chosen