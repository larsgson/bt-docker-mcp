"""Hybrid retrieval — FTS5 + passage + tag + (v2) vector ANN, fused via RRF.

Vector retrieval is OPTIONAL: if sqlite-vec isn't loaded or no `query_vec`
is supplied, the v1 retrievers still run on their own.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .analyzer import QueryAnalysis


@dataclass
class Hit:
    chunk_id: str
    score: float
    retrievers: list[str]


# ---------- candidate filtering ----------
# Only passages act as a HARD filter on the candidate set. Tags are treated
# as ranking BOOSTS (via tag_search → RRF) — that way an analyzer mis-guess
# can never exclude relevant content; it just doesn't help.

def _docs_overlapping_passages(db: sqlite3.Connection, passages: list[tuple[int, int]]) -> set[str] | None:
    """Set of doc_ids whose passage ranges overlap any of `passages`. None = no filter."""
    if not passages:
        return None
    where = " OR ".join("(start_bbcccvvv <= ? AND end_bbcccvvv >= ?)" for _ in passages)
    params: list[int] = []
    for s, e in passages:
        params.extend([e, s])  # query end >= ref.start AND query start <= ref.end
    rows = db.execute(f"SELECT DISTINCT doc_id FROM passage_refs WHERE {where}", params).fetchall()
    return {r[0] for r in rows}


def _docs_by_source(db: sqlite3.Connection, source: str | None) -> set[str] | None:
    """Restrict candidate docs to one source. None / 'all' = no filter.

    'door43'  = chunks NOT carrying `resource:aquifer`
    'aquifer' = chunks carrying `resource:aquifer`
    """
    if not source or source == "all":
        return None
    if source == "aquifer":
        rows = db.execute(
            "SELECT DISTINCT doc_id FROM tags WHERE tag = 'resource:aquifer'"
        ).fetchall()
        return {r[0] for r in rows}
    if source == "door43":
        rows = db.execute(
            "SELECT id FROM documents "
            "WHERE id NOT IN (SELECT doc_id FROM tags WHERE tag = 'resource:aquifer')"
        ).fetchall()
        return {r[0] for r in rows}
    raise ValueError(f"unknown source filter: {source!r} (expected 'door43', 'aquifer', or 'all')")


def _intersect_filters(*filters: set[str] | None) -> set[str] | None:
    """Intersect multiple optional doc-id filters. None means 'no constraint'."""
    out: set[str] | None = None
    for f in filters:
        if f is None:
            continue
        out = f if out is None else (out & f)
    return out


# ---------- retrievers ----------

def fts_search(db: sqlite3.Connection, query: str, *,
               doc_filter: set[str] | None = None, limit: int = 50) -> list[Hit]:
    """FTS5 match on chunks_fts, optionally constrained to a doc_id whitelist."""
    if not query.strip():
        return []
    sql = (
        "SELECT chunks.id, rank "
        "FROM chunks_fts "
        "JOIN chunks ON chunks_fts.rowid = chunks.rowid "
        "WHERE chunks_fts MATCH ?"
    )
    params: list = [query]
    if doc_filter is not None:
        if not doc_filter:
            return []
        placeholders = ",".join("?" * len(doc_filter))
        sql += f" AND chunks.doc_id IN ({placeholders})"
        params.extend(doc_filter)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 syntax errors (e.g. user query contains reserved chars) — degrade gracefully.
        print(f"fts_search: skipping due to {e!r}", flush=True)
        return []
    # FTS5 rank: lower is better; negate so larger = better for downstream UX.
    return [Hit(chunk_id=r[0], score=-float(r[1]), retrievers=["fts"]) for r in rows]


def passage_search(db: sqlite3.Connection, passages: list[tuple[int, int]], *,
                   limit: int = 50) -> list[Hit]:
    """One Hit per overlapping doc — chunk_index=0 is the canonical chunk."""
    if not passages:
        return []
    where = " OR ".join("(passage_refs.start_bbcccvvv <= ? AND passage_refs.end_bbcccvvv >= ?)" for _ in passages)
    params: list[int] = []
    for s, e in passages:
        params.extend([e, s])
    sql = (
        "SELECT DISTINCT chunks.id "
        "FROM passage_refs "
        "JOIN chunks ON chunks.doc_id = passage_refs.doc_id AND chunks.chunk_index = 0 "
        f"WHERE {where} "
        "ORDER BY passage_refs.start_bbcccvvv "
        "LIMIT ?"
    )
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    n = len(rows)
    # Synthetic descending score so RRF treats earlier hits as higher rank.
    return [Hit(chunk_id=r[0], score=1.0 - i / max(1, n), retrievers=["passage"]) for i, r in enumerate(rows)]


def scripture_search(
    db: sqlite3.Connection,
    passages: list[tuple[int, int]],
    query_vec: list[float] | None,
    *,
    fts_query: str = "",
    limit: int = 25,
) -> list[Hit]:
    """Two-pass `kind:scripture` retrieval within the passage filter.

    Pass 1 — vec-rank scripture chunks (when query_vec available).
    Pass 2 — FTS5 rank scripture chunks against `fts_query` (when present).

    Both passes feed RRF, so a verse that matches FTS keywords ("must be
    blameless") OR is semantically close to the question ranks well even
    when the embedding alone misses it. Without this dual signal, vec
    ranking with text-embedding-3-small often prefers greeting/closing
    verses over the actual answer-bearing verses on thematic questions.

    Why this exists: when a passage filter is active, the actual verse text
    is often the highest-value content — but commentary uses the user's
    vocabulary directly while verses use biblical vocabulary, so naive
    full-corpus retrieval lets commentary push scripture below top-K. This
    retriever contributes an INDEPENDENT scripture-only ranking that RRF
    then folds in alongside the general retrievers.
    """
    if not passages:
        return []
    where_passage = " OR ".join(
        "(passage_refs.start_bbcccvvv <= ? AND passage_refs.end_bbcccvvv >= ?)"
        for _ in passages
    )
    params: list = []
    for s, e in passages:
        params.extend([e, s])
    rows = db.execute(
        f"""
        SELECT DISTINCT passage_refs.doc_id
        FROM passage_refs
        JOIN tags ON tags.doc_id = passage_refs.doc_id AND tags.tag = 'kind:scripture'
        WHERE {where_passage}
        """,
        params,
    ).fetchall()
    scripture_doc_ids = {r[0] for r in rows}
    if not scripture_doc_ids:
        return []

    out: list[Hit] = []
    # Pass 1 — vec-ranked scripture (broader limit; RRF handles dedup).
    if query_vec:
        for h in vector_search(db, query_vec, doc_filter=scripture_doc_ids, limit=limit):
            out.append(Hit(chunk_id=h.chunk_id, score=h.score, retrievers=["scripture"]))
    # Pass 2 — FTS5-ranked scripture. Catches "must" / "blameless" / etc.
    # when vec ranking under-weights them.
    if fts_query.strip():
        for h in fts_search(db, fts_query, doc_filter=scripture_doc_ids, limit=limit):
            out.append(Hit(chunk_id=h.chunk_id, score=h.score, retrievers=["scripture"]))
    return out


def title_search(
    db: sqlite3.Connection,
    query: str,
    *,
    doc_filter: set[str] | None = None,
    limit: int = 20,
) -> list[Hit]:
    """FTS5 over document titles — pinpoint hits for entity / term lookups.

    Why: chunk-body FTS saturates with noise on entity questions (every
    narrative passage with the entity's name competes). Title FTS is
    discriminative: TW articles, book intros, and named verses get titles
    like "TW — Boaz" / "Aquifer — Titus 1:1" that pin the entity hit.
    """
    if not query.strip():
        return []
    sql = (
        "SELECT documents.id "
        "FROM documents_fts "
        "JOIN documents ON documents_fts.rowid = documents.rowid "
        "WHERE documents_fts MATCH ?"
    )
    params: list = [query]
    if doc_filter is not None:
        if not doc_filter:
            return []
        placeholders = ",".join("?" * len(doc_filter))
        sql += f" AND documents.id IN ({placeholders})"
        params.extend(doc_filter)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        doc_rows = db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        # FTS5 syntax errors on user input — degrade gracefully.
        print(f"title_search: skipping due to {e!r}", flush=True)
        return []
    if not doc_rows:
        return []

    # Map matching doc_ids → their canonical (chunk_index=0) chunks.
    doc_ids = [r[0] for r in doc_rows]
    placeholders = ",".join("?" * len(doc_ids))
    chunk_rows = db.execute(
        f"SELECT id, doc_id FROM chunks WHERE doc_id IN ({placeholders}) AND chunk_index = 0",
        doc_ids,
    ).fetchall()
    chunk_by_doc = {r[1]: r[0] for r in chunk_rows}
    n = len(doc_ids)
    hits: list[Hit] = []
    for i, did in enumerate(doc_ids):
        chunk_id = chunk_by_doc.get(did)
        if chunk_id:
            hits.append(Hit(chunk_id=chunk_id, score=1.0 - i / max(1, n), retrievers=["title"]))
    return hits


def vector_search(
    db: sqlite3.Connection,
    query_vec: list[float] | None,
    *,
    doc_filter: set[str] | None = None,
    limit: int = 50,
    overfetch: int = 4,
) -> list[Hit]:
    """KNN over chunks_vec using sqlite-vec. Returns [] if vec unavailable."""
    if not query_vec:
        return []
    try:
        # Existence check — fails fast if chunks_vec isn't there or sqlite-vec isn't loaded.
        db.execute("SELECT count(*) FROM chunks_vec LIMIT 1")
    except sqlite3.OperationalError:
        return []

    from indexer.embed import serialize_vector  # lazy: avoids importing on v1-only paths

    qvec = serialize_vector(query_vec)
    # sqlite-vec rejects LIMIT alongside `k = ?` on its virtual tables.
    # Use `k` to bound the ANN scan; clamp client-side after fetch.
    k = max(limit * overfetch, limit)
    try:
        if doc_filter is None:
            rows = db.execute(
                "SELECT chunk_id, distance FROM chunks_vec "
                "WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (qvec, limit),
            ).fetchall()
        else:
            if not doc_filter:
                return []
            placeholders = ",".join("?" * len(doc_filter))
            # Outer SELECT can use LIMIT freely — only the chunks_vec MATCH is restricted.
            rows = db.execute(
                f"""
                WITH knn AS (
                    SELECT chunk_id, distance
                    FROM chunks_vec
                    WHERE embedding MATCH ? AND k = ?
                )
                SELECT knn.chunk_id, knn.distance
                FROM knn
                JOIN chunks ON chunks.id = knn.chunk_id
                WHERE chunks.doc_id IN ({placeholders})
                ORDER BY knn.distance
                LIMIT ?
                """,
                [qvec, k, *doc_filter, limit],
            ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"vector_search: skipping due to {e!r}", flush=True)
        return []

    # cosine distance — lower is closer; negate so larger == more relevant.
    return [Hit(chunk_id=r[0], score=-float(r[1]), retrievers=["vec"]) for r in rows]


def tag_search(db: sqlite3.Connection, tags: list[str], *, limit: int = 50) -> list[Hit]:
    if not tags:
        return []
    placeholders = ",".join("?" * len(tags))
    sql = (
        "SELECT DISTINCT chunks.id "
        "FROM tags "
        "JOIN chunks ON chunks.doc_id = tags.doc_id AND chunks.chunk_index = 0 "
        f"WHERE tags.tag IN ({placeholders}) "
        "LIMIT ?"
    )
    params = list(tags) + [limit]
    rows = db.execute(sql, params).fetchall()
    n = len(rows)
    return [Hit(chunk_id=r[0], score=1.0 - i / max(1, n), retrievers=["tag"]) for i, r in enumerate(rows)]


# ---------- fusion ----------

def rrf(
    hit_lists: list[list[Hit]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[Hit]:
    """Reciprocal Rank Fusion across ranked retriever outputs, with optional per-list weights."""
    if weights is None:
        weights = [1.0] * len(hit_lists)
    if len(weights) != len(hit_lists):
        raise ValueError(f"weights length {len(weights)} != hit_lists length {len(hit_lists)}")
    scores: dict[str, float] = {}
    retrievers: dict[str, set[str]] = {}
    for hits, weight in zip(hit_lists, weights):
        if weight == 0:
            continue
        for rank, h in enumerate(hits, start=1):
            scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + weight / (k + rank)
            retrievers.setdefault(h.chunk_id, set()).update(h.retrievers)
    fused = [
        Hit(chunk_id=cid, score=score, retrievers=sorted(retrievers[cid]))
        for cid, score in scores.items()
    ]
    fused.sort(key=lambda h: h.score, reverse=True)
    return fused


# ---------- top-level ----------

# Per-intent RRF weights. Order: [fts, title, passage, scripture, tag, vec].
#
# Title-search is gold for entity_lookup (Who/What is X?) and useful for
# methodology (matching module names like "Metaphor"). For thematic and
# passage-shaped queries, title hits over-weight TW term articles and push
# narrative notes / per-verse content out of top-K, which kills queries
# where the answer is in a TN body, not a TW title. Down-weight title there.
_INTENT_WEIGHTS: dict[str, list[float]] = {
    "thematic":         [1.0, 0.5, 1.0, 1.0, 1.0, 1.0],
    "entity_lookup":    [1.0, 2.5, 0.8, 0.8, 1.5, 1.0],
    "passage_specific": [1.0, 0.6, 1.2, 1.5, 1.0, 1.0],
    "passage_book":     [1.0, 0.6, 1.1, 1.4, 1.0, 1.0],
    "methodology":      [1.0, 1.5, 1.0, 0.8, 1.0, 1.2],
}


def retrieve(
    db: sqlite3.Connection,
    analysis: QueryAnalysis,
    *,
    top_k: int = 10,
    query_vec: list[float] | None = None,
    source_filter: str | None = None,
) -> list[Hit]:
    """Run all configured retrievers, fuse via intent-weighted RRF, return top_k chunks.

    `query_vec`     enables the vector retriever; if None, vector is skipped.
    `source_filter` 'door43' / 'aquifer' / 'all'. Restricts candidate docs.

    Note on passage filtering: a NARROW passage (specific verse(s), range
    < 999 verses) acts as a hard `doc_filter` to constrain FTS/vec. A BROAD
    passage (whole-book range from "according to Titus" / "the gospel of John")
    is treated as a soft hint — it still drives `scripture_search` and
    `passage_search`, but does NOT exclude content from FTS/vec. That way an
    inferred book scope helps without crowding out cross-book term articles
    or narrative notes that legitimately bear on the question.
    """
    narrow = analysis.passages and any((e - s) < 999 for s, e in analysis.passages)
    passages_filter = _docs_overlapping_passages(db, analysis.passages) if narrow else None
    source = _docs_by_source(db, source_filter)
    doc_filter = _intersect_filters(passages_filter, source)

    fts_hits   = fts_search(db, analysis.fts_query, doc_filter=doc_filter)
    title_hits = title_search(db, analysis.fts_query, doc_filter=source)  # ignore passage filter
    pas_hits   = passage_search(db, analysis.passages)
    scrip_hits = scripture_search(db, analysis.passages, query_vec, fts_query=analysis.fts_query)
    tag_hits   = tag_search(db, analysis.tags)
    vec_hits   = vector_search(db, query_vec, doc_filter=doc_filter) if query_vec else []

    weights = _INTENT_WEIGHTS.get(analysis.intent, _INTENT_WEIGHTS["thematic"])
    fused = rrf(
        [fts_hits, title_hits, pas_hits, scrip_hits, tag_hits, vec_hits],
        weights=weights,
    )
    return fused[:top_k]
