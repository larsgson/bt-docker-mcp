"""POST /api/ask — full RAG: free-form question → cited answer."""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from indexer import citations as citations_mod
from indexer.db import has_vec
from query.analyzer import analyze
from query.retrieve import retrieve
from server.auth import require_password
from server.deps import get_db
from server.ratelimit import LIMIT_ASK, limiter
from server.resolver import chunk_preview_from_card

router = APIRouter()


class AskScope(BaseModel):
    source: Literal["all", "door43", "aquifer"] = "all"
    book: str | None = None


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    lang: str = "en"
    scope: AskScope | None = None
    top_k: int = 10


@router.post("/ask", dependencies=[Depends(require_password)])
@limiter.limit(LIMIT_ASK)
def ask(request: Request, req: AskRequest, db: sqlite3.Connection = Depends(get_db)) -> dict:
    if req.top_k < 1 or req.top_k > 50:
        raise HTTPException(status_code=400, detail="top_k must be 1..50")

    analysis = analyze(req.question)
    if req.scope and req.scope.book:
        analysis.tags.append(f"book:{req.scope.book.upper()}")

    query_vec = None
    if has_vec(db):
        try:
            from indexer.embed import embed_texts
            query_vec = embed_texts([req.question], input_type="query")[0]
        except Exception as e:
            print(f"  ask: embed failed ({type(e).__name__}: {e}); proceeding without vec", flush=True)

    source_filter = req.scope.source if req.scope else "all"
    hits = retrieve(db, analysis, top_k=req.top_k, query_vec=query_vec, source_filter=source_filter)
    cards = citations_mod.resolve_many(db, [h.chunk_id for h in hits])

    from query.synthesize import synthesize  # lazy: pulls openai SDK
    synth = synthesize(req.question, cards, db=db, analysis=analysis)

    by_id = {c.chunk_id: c for c in cards}
    citations_out: list[dict] = []
    for n, cid in enumerate(synth["citations"], start=1):
        card = by_id.get(cid)
        if card is None:
            continue
        preview = chunk_preview_from_card(card, lang=req.lang)
        preview["n"] = n
        citations_out.append(preview)

    return {
        "question": req.question,
        "answer": synth["answer"],
        "citations": citations_out,
        "confidence": synth["confidence"],
        "lang": req.lang,
        "analysis": {
            "fts_query": analysis.fts_query,
            "passages": [list(p) for p in analysis.passages],
            "tags": analysis.tags,
            "intent": analysis.intent,
        },
    }
