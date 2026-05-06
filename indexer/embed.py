#!/usr/bin/env python3
"""Embedding pipeline.

  python3 -m indexer.embed                # embed every chunk that hasn't been embedded yet
  python3 -m indexer.embed --reset-vec    # drop chunks_vec then re-embed everything

Used at *both* index-build time (this CLI) and query time
(`embed_texts([question])` in `query/ask.py`).

Default provider: OpenAI `text-embedding-3-small` (1536 dim, cosine).
Override via env vars:
  OPENAI_API_KEY              required
  BTMCP_EMBEDDING_MODEL       default: text-embedding-3-small
  BTMCP_EMBEDDING_DIM         default: 1536
  BTMCP_EMBEDDING_BATCH_SIZE  default: 100
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

EMBEDDING_MODEL = os.environ.get("BTMCP_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.environ.get("BTMCP_EMBEDDING_DIM", "1536"))
EMBEDDING_BATCH = int(os.environ.get("BTMCP_EMBEDDING_BATCH_SIZE", "100"))

DEFAULT_DB = Path(__file__).resolve().parent / "index.db"


def _client():
    from openai import OpenAI

    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY required for embeddings")
    try:
        key.encode("ascii")
    except UnicodeEncodeError as e:
        raise RuntimeError(
            f"OPENAI_API_KEY contains non-ASCII char {key[e.start:e.end]!r} at position {e.start}; "
            f"re-copy from a plain-text source"
        ) from None
    return OpenAI(api_key=key)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed via OpenAI. Order-preserving."""
    if not texts:
        return []
    resp = _client().embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [e.embedding for e in resp.data]


def serialize_vector(vec: list[float]) -> bytes:
    """Pack a Python float list into the bytes layout sqlite-vec expects."""
    import sqlite_vec  # type: ignore

    return sqlite_vec.serialize_float32(vec)


def ensure_vec_table(db: sqlite3.Connection) -> None:
    db.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[{EMBEDDING_DIM}] distance_metric=cosine
        )
        """
    )


def _stored_model(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", ("embedding_model",)).fetchone()
    return row[0] if row else None


def embed_all_chunks(db: sqlite3.Connection, *, batch_size: int = EMBEDDING_BATCH) -> dict:
    """Embed every chunk that doesn't yet have a row in chunks_vec.

    If a different embedding model was used previously (recorded in meta),
    raise — callers should --reset-vec to switch models so the index doesn't
    end up with mixed-provenance vectors.
    """
    ensure_vec_table(db)

    prev = _stored_model(db)
    if prev is not None and prev != EMBEDDING_MODEL:
        raise RuntimeError(
            f"existing index was embedded with {prev!r}; current model is {EMBEDDING_MODEL!r}. "
            f"Run `python -m indexer.embed --reset-vec` to re-embed under the new model."
        )

    rows = db.execute(
        """
        SELECT chunks.id, chunks.body
        FROM chunks
        LEFT JOIN chunks_vec ON chunks_vec.chunk_id = chunks.id
        WHERE chunks_vec.chunk_id IS NULL
        ORDER BY chunks.id
        """
    ).fetchall()

    embedded = 0
    skipped = 0
    total = len(rows)
    for i in range(0, total, batch_size):
        batch = rows[i : i + batch_size]
        # Drop rows with empty bodies — OpenAI rejects empty inputs; their
        # absence from chunks_vec just means vector retrieval can't surface them.
        items = [(cid, body) for cid, body in batch if body and body.strip()]
        skipped += len(batch) - len(items)
        if not items:
            continue
        ids = [it[0] for it in items]
        bodies = [it[1] for it in items]
        vectors = embed_texts(bodies)
        params = [(cid, serialize_vector(v)) for cid, v in zip(ids, vectors)]
        db.executemany("INSERT INTO chunks_vec(chunk_id, embedding) VALUES (?, ?)", params)
        db.commit()
        embedded += len(params)
        print(f"  embedded {min(i + batch_size, total)}/{total}", file=sys.stderr)

    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("embedding_model", EMBEDDING_MODEL))
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("embedding_dim", str(EMBEDDING_DIM)))
    db.commit()
    return {"embedded": embedded, "skipped_empty": skipped, "candidate_total": total}


def reset_vec(db: sqlite3.Connection) -> None:
    db.execute("DROP TABLE IF EXISTS chunks_vec")
    db.execute("DELETE FROM meta WHERE key IN ('embedding_model', 'embedding_dim')")
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, type=Path)
    ap.add_argument("--reset-vec", action="store_true", help="drop chunks_vec, then re-embed all chunks")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"db not found: {args.db}\nrun ingest + indexer.build first", file=sys.stderr)
        return 2

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from indexer.db import has_vec, open_db
    from indexer.env import load_env

    load_env()
    db = open_db(args.db)
    if not has_vec(db):
        print("sqlite-vec is not loaded; cannot embed. Install with: pip install sqlite-vec", file=sys.stderr)
        return 3

    if args.reset_vec:
        reset_vec(db)

    result = embed_all_chunks(db)
    result["db"] = str(args.db)
    result["model"] = EMBEDDING_MODEL
    result["dim"] = EMBEDDING_DIM
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
