"""GET /api/tree/<tree_name>[/...] — dispatch to tree builders."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from server.deps import get_db
from server.ratelimit import LIMIT_READ, limiter
from server.trees import BUILDERS

router = APIRouter()


@router.get("/tree/{tree_name}")
@limiter.limit(LIMIT_READ)
def tree_root(request: Request, tree_name: str, lang: str = "en", db: sqlite3.Connection = Depends(get_db)) -> dict:
    builder = BUILDERS.get(tree_name)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"unknown tree: {tree_name}")
    try:
        return builder.root(db, lang=lang)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tree/{tree_name}/{path:path}")
@limiter.limit(LIMIT_READ)
def tree_descend(
    request: Request,
    tree_name: str,
    path: str,
    lang: str = "en",
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    builder = BUILDERS.get(tree_name)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"unknown tree: {tree_name}")
    parts = [p for p in path.split("/") if p]
    try:
        return builder.descend(db, parts, lang=lang)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
