"""GET /api/chunk/<chunk_id> — full body + tree paths + cross-refs."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from server.deps import get_db
from server.resolver import resolve_chunk

router = APIRouter()


@router.get("/chunk/{chunk_id}")
def get_chunk(chunk_id: str, lang: str = "en", db: sqlite3.Connection = Depends(get_db)) -> dict:
    result = resolve_chunk(db, chunk_id, lang=lang)
    if result is None:
        raise HTTPException(status_code=404, detail=f"chunk_id not found: {chunk_id}")
    return result
