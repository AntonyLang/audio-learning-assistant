"""Text normalisation, hashing and lexical preparation (doc §4.2, §4.3).

Everything that defines a "character offset" lives here: offsets are Unicode code
points into the normalised body text, which is the same unit the web client uses for
highlighting a citation.
"""

from __future__ import annotations

import hashlib
import re

import jieba

_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_NEWLINE_RE = re.compile(r"\r\n?")
_SENTENCE_END = "。！？!?；;\n"
#: ASCII tokens that must survive tokenisation unchanged (API names, versions).
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-/]{1,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")


def normalize_text(raw: str) -> str:
    """Stable whitespace normalisation that never changes code-point identity of content."""
    text = _NEWLINE_RE.sub("\n", raw)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lexemes_for(text: str) -> str:
    """Tokenise for the PostgreSQL ``simple`` tsvector channel.

    Chinese is segmented with jieba; ASCII identifiers and versions are kept as single
    tokens so ``qwen-plus`` or ``v1.2`` remain searchable. The same function is applied
    to queries, which is what makes the keyword channel comparable at all.
    """
    tokens: list[str] = []
    for piece in re.split(r"(\s+)", text):
        if not piece or piece.isspace():
            continue
        if _CJK.search(piece):
            tokens.extend(t.strip() for t in jieba.cut(piece) if t.strip())
        tokens.extend(_ASCII_TOKEN.findall(piece))
        for token in re.findall(r"\d+(?:\.\d+)+", piece):
            tokens.append(token)
    seen: list[str] = []
    for token in tokens:
        token = token.lower().strip("，。、；：（）()[]【】\"'")
        if not token or token in _STOPWORDS:
            continue
        seen.append(token)
    return " ".join(seen)


def query_terms(question: str, limit: int = 6) -> list[str]:
    """Reduce a long question to a few controlled terms.

    ``plainto_tsquery`` joins terms with AND, so feeding every segmented token of a long
    sentence silently drops recall; the document calls this out explicitly.
    """
    tokens = [t for t in lexemes_for(question).split() if len(t) > 1 or _CJK.search(t)]
    ranked: list[str] = []
    for token in tokens:
        if token not in ranked:
            ranked.append(token)
    return ranked[:limit]


_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "就",
        "都",
        "而",
        "及",
        "或",
        "一个",
        "这个",
        "我们",
        "你们",
        "他们",
        "可以",
        "the",
        "a",
        "an",
        "of",
        "to",
        "and",
        "or",
        "is",
        "are",
        "in",
        "on",
        "for",
        "with",
        "this",
        "that",
    }
)


def split_sentences(text: str) -> list[str]:
    """Sentence split that keeps the terminating punctuation (needed for TTS pauses)."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in _SENTENCE_SPLIT.split(line) if p.strip()]
        for part in parts or [line]:
            if len(part) > 220:
                out.extend(_split_long_sentence(part))
            else:
                out.append(part)
    return out


def _split_long_sentence(sentence: str, limit: int = 180) -> list[str]:
    pieces: list[str] = []
    buffer = ""
    for char in sentence:
        buffer += char
        if len(buffer) >= limit and char in "，、：,:":
            pieces.append(buffer)
            buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def estimate_tokens(text: str) -> int:
    """Conservative char->token estimate: CJK ≈ 1 token/char, ASCII ≈ 1 token/4 chars."""
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return cjk + other // 4 + 1


def truncate_at_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    idx = max(cut.rfind(mark) for mark in _SENTENCE_END) if cut else -1
    return cut[: idx + 1] if idx > max_chars // 2 else cut
