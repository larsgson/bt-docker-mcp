"""GET /api/health — sanity check + corpus metadata."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from indexer.db import has_vec
from server.deps import get_db

router = APIRouter()


@router.get("/health")
def health(db: sqlite3.Connection = Depends(get_db)) -> dict:
    schema_row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    indexed_row = db.execute("SELECT value FROM meta WHERE key = 'indexed_at'").fetchone()
    embed_row = db.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
    docs = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    try:
        vec_rows = db.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    except sqlite3.OperationalError:
        vec_rows = 0
    return {
        "status": "ok",
        "schema_version": schema_row[0] if schema_row else None,
        "indexed_at": int(indexed_row[0]) if indexed_row and indexed_row[0].isdigit() else None,
        "embedding_model": embed_row[0] if embed_row else None,
        "vec_loaded": has_vec(db),
        "counts": {"documents": docs, "chunks": chunks, "vectors": vec_rows},
    }
