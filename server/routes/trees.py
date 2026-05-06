"""GET /api/tree/<tree_name>[/...] — dispatch to tree builders."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from server.deps import get_db
from server.trees import BUILDERS

router = APIRouter()


@router.get("/tree/{tree_name}")
def tree_root(tree_name: str, lang: str = "en", db: sqlite3.Connection = Depends(get_db)) -> dict:
    builder = BUILDERS.get(tree_name)
    if builder is None:
        raise HTTPException(status_code=404, detail=f"unknown tree: {tree_name}")
    try:
        return builder.root(db, lang=lang)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tree/{tree_name}/{path:path}")
def tree_descend(
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
