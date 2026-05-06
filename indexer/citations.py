"""Resolve a chunk_id back to a user-facing CitationCard.

Cards are what the UI shows. Every claim in an answer carries one card so
the user can see exactly which source backs it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from . import references

DEFAULT_DB = Path(__file__).resolve().parent / "index.db"
EXCERPT_LEN = 240


@dataclass
class CitationCard:
    chunk_id: str
    document_title: str
    passage: str | None        # human-readable, e.g. "Titus 1:1"
    tags: list[str]
    source: str                # source_path of the document
    excerpt: str               # body trimmed to EXCERPT_LEN chars
    metadata: dict             # the document's full metadata dict

    def asdict(self) -> dict:
        return asdict(self)


def _trim(text: str, n: int = EXCERPT_LEN) -> str:
    text = " ".join(text.split())  # collapse whitespace
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _passage_for(db: sqlite3.Connection, doc_id: str) -> str | None:
    rows = db.execute(
        "SELECT MIN(start_bbcccvvv), MAX(end_bbcccvvv) FROM passage_refs WHERE doc_id = ?",
        (doc_id,),
    ).fetchone()
    if not rows or rows[0] is None:
        return None
    return references.human(rows[0], rows[1])


def _tags_for(db: sqlite3.Connection, doc_id: str) -> list[str]:
    return [r[0] for r in db.execute("SELECT tag FROM tags WHERE doc_id = ? ORDER BY tag", (doc_id,))]


def resolve(db: sqlite3.Connection, chunk_id: str) -> CitationCard | None:
    row = db.execute(
        """
        SELECT chunks.body,
               documents.id, documents.title, documents.source_path, documents.metadata
        FROM chunks
        JOIN documents ON documents.id = chunks.doc_id
        WHERE chunks.id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    body, doc_id, title, source_path, meta_json = row
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except json.JSONDecodeError:
        meta = {}
    return CitationCard(
        chunk_id=chunk_id,
        document_title=title,
        passage=_passage_for(db, doc_id),
        tags=_tags_for(db, doc_id),
        source=source_path,
        excerpt=_trim(body),
        metadata=meta,
    )


def resolve_many(db: sqlite3.Connection, chunk_ids: list[str]) -> list[CitationCard]:
    cards: list[CitationCard] = []
    for cid in chunk_ids:
        card = resolve(db, cid)
        if card is not None:
            cards.append(card)
    return cards
