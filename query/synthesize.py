"""LLM-driven answer synthesis with strict citation constraint.

The prompt forces the model to cite only chunk_ids that appear in its
SOURCES list. Downstream validation drops any citation not in that set,
so a hallucinated source_id can't survive into the rendered answer.

Note on chunk body vs. excerpt: CitationCard.excerpt is the 240-char
display preview shown alongside citations in the UI. The LLM gets the
FULL chunk body (capped at MAX_BODY_CHARS) — otherwise it can't see
content past the excerpt boundary. Translator notes commonly bury the
operative word ("blameless", "loyalty oath", etc.) past 240 chars; TW
and TA articles often only show generic definitional openers in 240
chars while the substantive theological content lives below.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import TYPE_CHECKING

from .llm import chat_completion

if TYPE_CHECKING:
    from indexer.citations import CitationCard

MAX_BODY_CHARS = 1500


SYSTEM_PROMPT = """You answer questions about Bible translation resources using ONLY the SOURCES provided.

The SOURCES are excerpts from a translation-helps corpus. Common types you may see:
  - ULT/UST: scripture text (Literal Text / Simplified Text)
  - TN: translation notes — explanations, alternate translations, guidance
  - TQ: translation questions WITH their responses — these directly answer
        comprehension questions about the passage
  - TW: term definitions (people, places, key biblical concepts)
  - TA: translation methodology articles
  - TWL: passage→term link metadata

ALL of these count as valid answers when they speak to the user's question. In
particular, TQ rows ARE answers — when the user asks "what questions are there
for X" or asks a comprehension question, surface the TQ rows that match.

Hard rules:
1. Use ONLY the SOURCES — do not bring in outside knowledge or speculate.
2. Every claim MUST cite at least one chunk_id inline using square brackets, e.g. [56001001:0001].
3. If the SOURCES truly do not contain anything that speaks to the question
   (not even a tangentially related TN, TW, TQ, or TA), set "answer" to:
   "I don't see an answer to that in the indexed sources."
   and "citations" to the chunk_ids of the closest related sources.
4. Be concise. No preamble, no meta-commentary, no opinions.

Reply with a single JSON object only:
{
  "answer":     "<text with inline [chunk_id] citations>",
  "citations":  ["<chunk_id>", ...],
  "confidence": "low" | "medium" | "high"
}
"""

USER_TEMPLATE = """SOURCES:

{sources}

QUESTION: {question}

Reply only with the JSON object specified."""


def _format_sources(cards: list["CitationCard"], bodies: dict[str, str]) -> str:
    """Render SOURCES block with FULL chunk bodies (capped) — not the 240-char display excerpt."""
    blocks = []
    for c in cards:
        passage = c.passage or "no passage"
        body = bodies.get(c.chunk_id) or c.excerpt
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "…"
        blocks.append(f"[{c.chunk_id}] {c.document_title} — {passage}\n  {body}")
    return "\n\n".join(blocks)


def _fetch_bodies(db: sqlite3.Connection | None, chunk_ids: list[str]) -> dict[str, str]:
    """Look up full chunk bodies. Returns {} if no db (caller falls back to excerpts)."""
    if db is None or not chunk_ids:
        return {}
    placeholders = ",".join("?" * len(chunk_ids))
    rows = db.execute(
        f"SELECT id, body FROM chunks WHERE id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _extract_inline_citations(answer: str) -> list[str]:
    return re.findall(r"\[([A-Za-z0-9:_\-]+)\]", answer)


def synthesize(
    question: str,
    cards: list["CitationCard"],
    db: sqlite3.Connection | None = None,
) -> dict:
    """Return {answer, citations, confidence, raw}.

    `db`, when supplied, lets the synthesizer pull full chunk bodies for the
    LLM prompt (instead of the truncated 240-char display excerpts).
    `citations` is the validated union of inline-cited and explicitly-listed
    chunk_ids that actually exist in `cards`. Hallucinated ids are dropped.
    """
    if not cards:
        # Use the same phrase the LLM is instructed to emit in its refusal exit,
        # so the eval's refusal regex catches both paths uniformly.
        return {
            "answer": "I don't see an answer to that in the indexed sources.",
            "citations": [],
            "confidence": "low",
            "raw": None,
        }

    valid_ids = {c.chunk_id for c in cards}
    bodies = _fetch_bodies(db, [c.chunk_id for c in cards])
    user = USER_TEMPLATE.format(sources=_format_sources(cards, bodies), question=question)

    raw = chat_completion(system=SYSTEM_PROMPT, user=user, response_format="json")

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {
            "answer": (raw or "(empty)").strip(),
            "citations": [],
            "confidence": "low",
            "raw": raw,
        }

    answer = parsed.get("answer", "") or ""
    listed = parsed.get("citations") or []
    inline = _extract_inline_citations(answer)
    cited = []
    seen = set()
    for cid in [*inline, *listed]:
        if cid in valid_ids and cid not in seen:
            cited.append(cid)
            seen.add(cid)

    # Strip inline [chunk_id] markers that aren't in valid_ids.
    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in valid_ids else ""

    answer = re.sub(r"\[([A-Za-z0-9:_\-]+)\]", _replace, answer)
    answer = re.sub(r"\s{2,}", " ", answer).strip()

    return {
        "answer": answer,
        "citations": cited,
        "confidence": parsed.get("confidence", "medium"),
        "raw": raw,
    }
