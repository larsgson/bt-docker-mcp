"""GET /api/search — keyword + structured retrieval; semantic vec is opt-in (gated)."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from indexer import citations as citations_mod
from indexer.db import has_vec
from query.analyzer import analyze
from query.retrieve import retrieve
from server.auth import verify
from server.deps import get_db
from server.ratelimit import LIMIT_SEARCH, limiter
from server.resolver import chunk_preview_from_card

router = APIRouter()


@router.get("/search")
@limiter.limit(LIMIT_SEARCH)
def search(
    request: Request,
    q: str,
    lang: str = "en",
    kind: str | None = None,
    book: str | None = None,
    source: Literal["all", "door43", "aquifer"] = "all",
    top_k: int = 10,
    semantic: bool = False,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="query (?q=) is required")
    if top_k < 1 or top_k > 50:
        raise HTTPException(status_code=400, detail="top_k must be 1..50")

    analysis = analyze(q)
    # Apply faceted filters via tag candidates (RRF-boost, not hard filter — see retrieve.py)
    if kind:
        analysis.tags.append(f"kind:{kind}")
    if book:
        analysis.tags.append(f"book:{book.upper()}")

    query_vec = None
    if semantic and has_vec(db):
        # Semantic = hits OpenAI embeddings → password-gated.
        if not verify(authorization, x_api_key):
            raise HTTPException(
                status_code=401,
                detail="semantic search requires API password (BTMCP_API_PASSWORD)",
            )
        try:
            from indexer.embed import embed_texts
            query_vec = embed_texts([q])[0]
        except Exception as e:
            # Embedding failures are non-fatal: degrade to FTS+structured.
            print(f"  search: embed failed ({type(e).__name__}: {e}); proceeding without vec", flush=True)

    hits = retrieve(db, analysis, top_k=top_k, query_vec=query_vec, source_filter=source)
    cards = citations_mod.resolve_many(db, [h.chunk_id for h in hits])

    # Pair hits with cards (resolve_many preserves chunk_id order, but be defensive)
    by_id = {c.chunk_id: c for c in cards}
    enriched: list[dict] = []
    for h in hits:
        card = by_id.get(h.chunk_id)
        if card is None:
            continue
        preview = chunk_preview_from_card(card, lang=lang)
        preview["score"] = round(float(h.score), 6)
        preview["retrievers"] = h.retrievers
        enriched.append(preview)

    return {
        "query": q,
        "lang": lang,
        "filters": {"kind": kind, "book": book.upper() if book else None, "source": source},
        "semantic": bool(query_vec is not None),
        "analysis": {
            "fts_query": analysis.fts_query,
            "passages": [list(p) for p in analysis.passages],
            "tags": analysis.tags,
            "intent": analysis.intent,
        },
        "hits": enriched,
        "total": len(enriched),
    }
