"""Free-form query analyzer (regex-based).

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

  word_study       "What does the Greek word AGAPE mean?", "Strong's G3962" —
                   routes to lexicon_search + morphology_search
  morphology       "morphology of John 1:1", "what tense is X" —
                   routes to morphology_search
  genealogy        "father of David", "wife of Boaz" — routes to entity_search
                   with graph traversal
  topic            "What does the Bible say about covenant?" — routes to
                   topic_search (Nave's)
  xref             "cross-references for Romans 5:1" — routes to xref_search
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

    # Stage-3 intent extensions. Populated only when the analyzer detects
    # the corresponding intent shape; consumed by the matching retriever.
    word_study_terms: list[str] = field(default_factory=list)      # transliterations or English glosses
    entity_query: dict | None = None                                # {'name': str, 'relation': str | None}
    topic_query: str | None = None                                  # canonical topic name
    xref_source: int | None = None                                  # source bbcccvvv for cross-reference followup


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


# ---------- stage-3 intent patterns ----------

# Word-study: Greek/Hebrew lexicon lookups by transliteration or Strong's.
# Each pattern requires EXPLICIT Greek/Hebrew context or Strong's marker —
# we deliberately avoid matching plain "what does X mean" since that's
# entity_lookup territory (see _ENTITY_LOOKUP_PATTERNS).
_WORD_STUDY_RE = re.compile(
    r"\b(?:"
    # "Greek/Hebrew word X" — explicit lang
    r"(?:greek|hebrew)\s+word\s+([A-Za-z]{2,})"
    # "(meaning of) X in Greek/Hebrew" — explicit lang
    r"|(?:meaning\s+of\s+)?([A-Za-z]{2,})\s+in\s+(?:greek|hebrew)"
    # Strong's number anywhere
    r"|strong'?s?\s+(?:number\s+)?([GH]\d{1,5})"
    # "look up Strong's G####"
    r"|look\s+up\s+(?:strong'?s?\s+)?(?:number\s+)?([GH]\d{1,5})"
    # "lexicon entry/definition for X" / "BDB/LSJ definition of X"
    r"|lexicon\s+(?:entry|definition)\s+(?:for|of)\s+(?:the\s+)?(?:greek\s+|hebrew\s+)?(?:word\s+)?([A-Za-z]{2,}|[GH]\d+)"
    r"|(?:BDB|LSJ|Abbott[\s-]?Smith|TBESH|TBESG)\s+(?:definition|entry)\s+(?:of|for)\s+([HG]\d+|[A-Za-z]{2,})"
    r")\b",
    re.IGNORECASE,
)
# Note: deliberately NOT matching plain "what does X mean" — that pattern
# is too permissive (with IGNORECASE, `[A-Z]{3,}` matches lowercase too,
# triggering on "what does godliness mean?" which is entity_lookup. The
# legitimate "what does AGAPE mean?" form is covered by pattern 1
# ("Greek word AGAPE" — when the user adds the language prefix).

# Morphology: parse questions about specific verses or words.
_MORPHOLOGY_RE = re.compile(
    r"\b(?:"
    r"morphology\s+of"
    r"|what\s+(?:tense|case|gender|mood|voice)\s+is"
    r"|parse\s+(?:the\s+word|this\s+verse)"
    r"|word.by.word\s+(?:parse|breakdown|analysis)"
    r")\b",
    re.IGNORECASE,
)

# Genealogy: relation traversal. Captures relation word + entity name.
# Note: "ancestor"/"descendant" require multi-hop — left out for now.
_GENEALOGY_RE = re.compile(
    r"\b(father|mother|parents?|son|daughter|child(?:ren)?|"
    r"wife|husband|spouse|partner|"
    r"brother|sister|sibling)"
    r"\s+of\s+(?:the\s+)?(['\"]?)([A-Z][a-zA-Z'-]+)\2",
    re.IGNORECASE,
)
# "Whose <relation> is X?" or "Who is X's <relation>?"
_GENEALOGY_POSSESSIVE_RE = re.compile(
    r"\b([A-Z][a-zA-Z'-]+?)'s\s+(father|mother|son|daughter|"
    r"wife|husband|spouse|partner|brother|sister|child(?:ren)?|parents?)\b",
    re.IGNORECASE,
)

# Topic: Nave's-style "Bible says about X" / "verses about X".
_TOPIC_RE = re.compile(
    r"\b(?:"
    r"(?:what|which)\s+(?:does\s+|do\s+|did\s+)?(?:the\s+)?bible\s+say\s+about\s+(.+?)(?:\?|$)"
    r"|(?:bible\s+)?(?:verses|scriptures|passages)\s+(?:about|on|concerning|regarding)\s+(.+?)(?:\?|$)"
    r"|(?:show\s+me\s+)?(?:bible\s+)?verses\s+(?:about|on)\s+(.+?)(?:\?|$)"
    r"|what\s+(?:scripture|scriptures|verses)\s+(?:are\s+)?about\s+(.+?)(?:\?|$)"
    r")",
    re.IGNORECASE,
)

# Cross-references: explicit ask for related verses.
_XREF_RE = re.compile(
    r"\b(?:"
    r"cross.?references?\s+(?:for|to)\s+(.+?)(?:\?|$)"
    r"|parallel\s+passages?\s+(?:for|to)\s+(.+?)(?:\?|$)"
    r"|related\s+(?:verses?|passages?)\s+(?:to|with|for)\s+(.+?)(?:\?|$)"
    r"|what\s+other\s+(?:verses?|passages?)\s+(?:relate\s+to|are\s+(?:like|similar))\s+(.+?)(?:\?|$)"
    r"|see\s+also\s+(?:for\s+)?(.+?)(?:\?|$)"
    r")",
    re.IGNORECASE,
)

# Map English relation words → entity-graph relation strings (per Theographic
# ingest). Suffix `-rev` means "find someone with this edge TO the target"
# (e.g., "father of David" means find Jesse, who has father-of → David).
_RELATION_MAP = {
    "father": "father-of-rev",
    "mother": "mother-of-rev",
    "parents": "father-of-rev",
    "parent": "father-of-rev",
    "son": "father-of",
    "daughter": "father-of",
    "children": "father-of",
    "child": "father-of",
    "wife": "partner-of",
    "husband": "partner-of",
    "spouse": "partner-of",
    "partner": "partner-of",
    "brother": "sibling-of",
    "sister": "sibling-of",
    "sibling": "sibling-of",
}


def _normalize_strongs(s: str) -> str:
    m = re.match(r"^([GH])(\d+)([a-zA-Z]?)$", s.strip(), re.IGNORECASE)
    if not m:
        return s
    return f"{m.group(1).upper()}{int(m.group(2)):04d}{m.group(3).lower()}"


def _classify_word_study(raw: str) -> tuple[list[str], list[str]]:
    """Return (transliterations, strongs_tags) — empty if not a word-study query."""
    m = _WORD_STUDY_RE.search(raw)
    if not m:
        return [], []
    transliterations: list[str] = []
    strongs: list[str] = []
    for g in m.groups():
        if not g:
            continue
        if re.match(r"^[GH]\d+[a-zA-Z]?$", g, re.IGNORECASE):
            strongs.append(f"strongs:{_normalize_strongs(g)}")
        elif len(g) >= 2 and g.lower() not in _STOPWORDS:
            # 2-char minimum — covers "AB" (Hebrew, "father") and similar short
            # transliterations. The regex itself only emits these when explicit
            # Greek/Hebrew context surrounds the capture, so the FP risk is low.
            transliterations.append(g.lower())
    return transliterations, sorted(set(strongs))


def _classify_genealogy(raw: str) -> dict | None:
    m = _GENEALOGY_RE.search(raw)
    if m:
        relation_word = m.group(1).lower()
        name = m.group(3)
        rel = _RELATION_MAP.get(relation_word)
        if rel and name:
            return {"name": name, "relation": rel}
    m = _GENEALOGY_POSSESSIVE_RE.search(raw)
    if m:
        name = m.group(1)
        relation_word = m.group(2).lower()
        rel = _RELATION_MAP.get(relation_word)
        if rel and name:
            return {"name": name, "relation": rel}
    return None


_TOPIC_STOPWORDS = {"the", "this", "these", "any", "all", "some"}


def _classify_topic(raw: str) -> str | None:
    m = _TOPIC_RE.search(raw)
    if not m:
        return None
    for g in m.groups():
        if not g:
            continue
        topic = g.strip().rstrip("?.!,;").strip()
        # Strip leading articles
        topic = re.sub(r"^(?:the|a|an)\s+", "", topic, flags=re.IGNORECASE).strip()
        if not topic or topic.lower() in _TOPIC_STOPWORDS:
            continue
        # Reject overly long captures (likely greedy-match noise)
        if len(topic.split()) > 4:
            continue
        return topic
    return None


def _classify_xref(raw: str, parsed_passages: list[tuple[int, int]]) -> int | None:
    """Return the source bbcccvvv if this looks like a cross-reference query.
    Falls back to the first parsed passage when the regex matches but doesn't
    capture a re-parseable reference (already extracted at the top of analyze)."""
    m = _XREF_RE.search(raw)
    if not m:
        return None
    # Try to re-parse the captured tail
    for g in m.groups():
        if not g:
            continue
        refs = parse_references(g.strip())
        if refs:
            return refs[0][0]
    # Fall back to first passage extracted at top of analyze()
    if parsed_passages:
        return parsed_passages[0][0]
    return None


def _classify_intent(
    raw: str,
    passages: list[tuple[int, int]],
    term_candidates: list[str],
    *,
    word_study: bool,
    morphology: bool,
    entity_query: dict | None,
    topic_query: str | None,
    xref_source: int | None,
) -> str:
    # Highest precedence: structured stage-3 intents (most specific shapes).
    if xref_source is not None:
        return "xref"
    if entity_query is not None:
        return "genealogy"
    if word_study:
        return "word_study"
    if morphology:
        return "morphology"
    if topic_query:
        return "topic"
    # Existing v2 intents.
    if term_candidates:
        return "entity_lookup"
    if _METHODOLOGY_RE.search(raw):
        return "methodology"
    if passages:
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

    # Stage-3 intent detection. Each returns structured info; intent
    # classification picks the most specific match.
    word_study_terms, word_study_strongs = _classify_word_study(raw)
    morphology_marker = bool(_MORPHOLOGY_RE.search(raw))
    entity_query = _classify_genealogy(raw)
    topic_query = _classify_topic(raw)
    xref_source = _classify_xref(raw, passages)

    # Promote word-study Strong's tags into the analysis tag set so
    # tag_search and lexicon_search both see them.
    tags.extend(word_study_strongs)

    intent = _classify_intent(
        raw, passages, term_candidates,
        word_study=bool(word_study_terms or word_study_strongs),
        morphology=morphology_marker,
        entity_query=entity_query,
        topic_query=topic_query,
        xref_source=xref_source,
    )

    return QueryAnalysis(
        raw=raw, fts_query=fts_query, passages=passages, tags=tags, intent=intent,
        word_study_terms=word_study_terms,
        entity_query=entity_query,
        topic_query=topic_query,
        xref_source=xref_source,
    )
