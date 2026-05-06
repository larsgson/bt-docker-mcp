"""Free-form query analyzer (v1 — regex-based).

Pulls structure out of the user's question so the retriever can apply
filters (passage range, tags) alongside the lexical search and choose
which retrievers/weights to favor. Anything the analyzer can't classify
falls through to FTS5 as keywords.

Intent values:
  entity_lookup    short "Who/What is X?" — title/term retrieval matters most
  passage_specific query has a verse-level reference — scripture matters
  passage_book     query mentions a book in book-context ("according to X",
                   "in the book of X") — whole-book passage filter helps
  methodology      "how do I translate", "what is figs-X?" — TA modules matter
  thematic         everything else — balanced fusion (default)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from indexer.references import (
    BOOK_ALIASES,
    BOOK_NUMBERS,
    _REF_RE,
    _normalize_alias,
    encode,
    parse_references,
)


@dataclass
class QueryAnalysis:
    raw: str
    fts_query: str                  # FTS5 MATCH expression
    passages: list[tuple[int, int]] = field(default_factory=list)  # BBCCCVVV pairs
    tags: list[str] = field(default_factory=list)                  # exact-match tag candidates
    intent: str = "thematic"        # see module docstring


# Question/auxiliary words and other tokens that don't carry retrieval signal.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "not", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "of",
    "in", "on", "at", "to", "for", "with", "from", "by", "as", "this", "that",
    "these", "those", "it", "its", "his", "her", "their", "our", "your", "my",
    "we", "us", "you", "they", "them", "he", "she", "what", "which", "who",
    "whom", "whose", "where", "when", "why", "how", "any", "all", "some",
    "no", "yes", "if", "then", "than", "so", "such", "about", "between",
    "into", "out", "off", "down", "over", "under", "above", "below", "more",
    "most", "less", "least", "very", "just", "only", "also", "too", "say",
    "said", "says", "saying", "tell", "telling", "explain", "explaining",
    "show", "give", "mean", "means", "meaning", "i", "me", "mine",
}


def _fts_keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z]{3,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


# Short, recognizable entity-lookup patterns. Each captures the entity term in
# group(2). We deliberately match only short, clearly-shaped questions — so a
# longer query like "Why did Ruth choose to follow Naomi?" doesn't false-trigger.
_ENTITY_LOOKUP_PATTERNS = [
    # "Who was Boaz?" / "Who is Paul?"
    re.compile(r"^who (?:is|are|was|were)\s+(?:the\s+)?(['\"]?)([A-Za-z][\w-]+)\1\??\s*$", re.IGNORECASE),
    # "What is grace?" / "What was the temple?"
    re.compile(r"^what (?:is|are|was|were)\s+(?:the\s+|a\s+|an\s+)?(['\"]?)([a-z][\w-]+)\1\??\s*$", re.IGNORECASE),
    # "What does redemption mean?"
    re.compile(r"^what does\s+(?:the term\s+)?(['\"]?)([a-z][\w-]+)\1\s+mean\??\s*$", re.IGNORECASE),
    # "Tell me about Naomi"
    re.compile(r"^tell me about\s+(?:the\s+)?(['\"]?)([A-Za-z][\w-]+)\1\??\s*$", re.IGNORECASE),
    # "Define justification"
    re.compile(r"^define\s+(['\"]?)([A-Za-z][\w-]+)\1\??\s*$", re.IGNORECASE),
]


def _extract_term_candidates(text: str) -> list[str]:
    """Pull out TW-term candidates from short entity-lookup questions.

    Conservative: only fires on simple single-entity queries (≤10 words) with
    a clearly-shaped form. Returns lowercased candidates the retriever will
    surface as `term:<x>` tag hits via the tag retriever.
    """
    cleaned = text.strip()
    if len(cleaned.split()) > 10:
        return []
    out: set[str] = set()
    for pattern in _ENTITY_LOOKUP_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            cand = m.group(2).lower()
            if len(cand) >= 3 and cand not in _STOPWORDS:
                out.add(cand)
    return sorted(out)


# Phrases that put a book name into clear book-reference context. Used to
# extract a whole-book passage filter ONLY when the surrounding language
# unambiguously says "this is the book". Avoids the catastrophic false-positive
# of bare-token book aliases ("is" → ISA, "am" → AMO).
_BOOK_CONTEXT_RE = re.compile(
    r"\b(?:in|of|from|throughout|"
    r"according\s+to|the\s+book\s+of|the\s+letter(?:s)?\s+(?:to|from)|"
    r"the\s+gospel\s+of)"
    r"\s+((?:[123]\s*)?[A-Za-z]+)\b",
    re.IGNORECASE,
)


def _book_context_passages(text: str) -> list[tuple[int, int]]:
    """Whole-book passage ranges from book-context phrases. Empty if none."""
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for m in _BOOK_CONTEXT_RE.finditer(text):
        book_raw = m.group(1)
        code = BOOK_ALIASES.get(_normalize_alias(book_raw))
        if not code:
            continue
        n = BOOK_NUMBERS[code]
        if n in seen:
            continue
        seen.add(n)
        try:
            start = encode(code, 1, 1)
            end = n * 1_000_000 + 999_000 + 999
        except ValueError:
            continue
        out.append((start, end))
    return out


# Rough surface markers for a methodology question.
_METHODOLOGY_RE = re.compile(
    r"\b(?:translate|translating|translation|translated|"
    r"figs-[a-z]+|"
    r"how\s+(?:do|should|can|to)\s+(?:i|we|one|it|you))\b",
    re.IGNORECASE,
)


def _classify_intent(
    raw: str,
    passages: list[tuple[int, int]],
    term_candidates: list[str],
) -> str:
    if term_candidates:
        return "entity_lookup"
    if _METHODOLOGY_RE.search(raw):
        return "methodology"
    if passages:
        # specific = has at least one verse-level pair (range under a chapter)
        if any((e - s) < 999 for s, e in passages):
            return "passage_specific"
        return "passage_book"
    return "thematic"


def analyze(question: str) -> QueryAnalysis:
    raw = question.strip()
    passages = parse_references(raw)

    # If the question mentions a book in clear book-context phrasing AND
    # didn't already extract a specific reference for that book, add the
    # whole-book range. Helps queries like "according to Titus" / "in Ruth".
    if not passages:
        passages = _book_context_passages(raw)

    # Strip extracted refs from the FTS-bound text so book names don't pollute it.
    cleaned = _REF_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    keywords = _fts_keywords(cleaned)
    fts_query = " OR ".join(keywords) if keywords else ""

    term_candidates = _extract_term_candidates(raw)
    tags = [f"term:{t}" for t in term_candidates]
    intent = _classify_intent(raw, passages, term_candidates)

    return QueryAnalysis(
        raw=raw, fts_query=fts_query, passages=passages, tags=tags, intent=intent,
    )
