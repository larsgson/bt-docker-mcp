"""MCP tool definitions and handlers.

Each tool is a thin wrapper over the underlying retrieval / resolver code.
Handlers take (arguments: dict, db: sqlite3.Connection) and return the
JSON-serializable result dict that goes into the MCP `content` text body.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Callable

from indexer import citations as citations_mod
from indexer.db import has_vec
from indexer.references import parse_references
from query.analyzer import analyze
from query.retrieve import retrieve
from server.resolver import chunk_preview_from_card, resolve_chunk
from server.trees import BUILDERS

ToolHandler = Callable[[dict, sqlite3.Connection], dict]


# ---------- registry ----------

_REGISTRY: list[dict] = []
_HANDLERS: dict[str, ToolHandler] = {}


def register_tool(*, name: str, description: str, input_schema: dict):
    def decorate(fn: ToolHandler) -> ToolHandler:
        _REGISTRY.append({"name": name, "description": description, "inputSchema": input_schema})
        _HANDLERS[name] = fn
        return fn
    return decorate


def list_tools() -> list[dict]:
    if os.environ.get("BTMCP_EXPOSE_ASK") == "1":
        return list(_REGISTRY)
    return [t for t in _REGISTRY if t["name"] != "ask"]


def call_tool(name: str, arguments: dict, db: sqlite3.Connection) -> dict:
    if name == "ask" and os.environ.get("BTMCP_EXPOSE_ASK") != "1":
        raise ValueError("tool 'ask' is disabled (set BTMCP_EXPOSE_ASK=1)")
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name}")
    return handler(arguments or {}, db)


# ---------- tools ----------

@register_tool(
    name="search",
    description=(
        "Search the indexed Bible-translation corpus. Returns ranked chunks "
        "with metadata; does NOT generate an answer — caller (you) should read "
        "the chunks and synthesize.\n\n"
        "By default uses FTS5 keyword matching, passage-range matching, title "
        "matching, and tag filters via reciprocal rank fusion — no model calls, "
        "no API keys required, deterministic. Pass `use_semantic: true` to "
        "additionally enable vector ANN (requires OPENAI_API_KEY on the server "
        "and adds ~150ms per call); useful for paraphrased queries where keyword "
        "match misses the right chunks. NOTE: `use_semantic: true` is gated by "
        "the server-side API password (BTMCP_API_PASSWORD) — pass `Authorization: "
        "Bearer <password>` or `X-API-Key: <password>` on the MCP HTTP request."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-form question or keyword search."},
            "lang": {"type": "string", "default": "en"},
            "kind": {
                "type": "string",
                "enum": ["scripture", "translator-note", "question", "term", "methodology",
                         "study-note", "book-intro", "map", "image"],
            },
            "book": {"type": "string", "description": "USFM book code (e.g. 'TIT')."},
            "source": {"type": "string", "enum": ["all", "door43", "aquifer"], "default": "all"},
            "top_k": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            "use_semantic": {
                "type": "boolean",
                "default": False,
                "description": "Opt-in: also rank by semantic vector similarity. Requires OPENAI_API_KEY on the server.",
            },
        },
        "required": ["query"],
    },
)
def _search(args: dict, db: sqlite3.Connection) -> dict:
    q = args.get("query", "").strip()
    if not q:
        raise ValueError("'query' is required and non-empty")
    lang = args.get("lang", "en")
    top_k = int(args.get("top_k", 10))

    analysis = analyze(q)
    if args.get("kind"):
        analysis.tags.append(f"kind:{args['kind']}")
    if args.get("book"):
        analysis.tags.append(f"book:{str(args['book']).upper()}")

    # MCP default: NO model calls. Pure FTS5 + structured retrieval.
    # Caller can opt in to semantic vec via use_semantic=true (costs an
    # OPENAI_API_KEY-backed embedding call per query).
    query_vec = None
    if args.get("use_semantic") and has_vec(db):
        try:
            from indexer.embed import embed_texts
            query_vec = embed_texts([q])[0]
        except Exception:
            pass

    hits = retrieve(db, analysis, top_k=top_k, query_vec=query_vec,
                    source_filter=args.get("source", "all"))
    cards = citations_mod.resolve_many(db, [h.chunk_id for h in hits])
    by_id = {c.chunk_id: c for c in cards}

    out_hits = []
    for h in hits:
        card = by_id.get(h.chunk_id)
        if card is None:
            continue
        preview = chunk_preview_from_card(card, lang=lang)
        preview["score"] = round(float(h.score), 6)
        preview["retrievers"] = h.retrievers
        out_hits.append(preview)

    return {
        "query": q,
        "lang": lang,
        "analysis": {
            "fts_query": analysis.fts_query,
            "passages": [list(p) for p in analysis.passages],
            "tags": analysis.tags,
            "intent": analysis.intent,
        },
        "hits": out_hits,
    }


@register_tool(
    name="get_chunk",
    description=(
        "Fetch the full body of a specific chunk by chunk_id. Returns body text, "
        "tree paths the chunk lives in, and cross-references."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string"},
            "lang": {"type": "string", "default": "en"},
        },
        "required": ["chunk_id"],
    },
)
def _get_chunk(args: dict, db: sqlite3.Connection) -> dict:
    chunk_id = args.get("chunk_id", "").strip()
    if not chunk_id:
        raise ValueError("'chunk_id' is required")
    result = resolve_chunk(db, chunk_id, lang=args.get("lang", "en"))
    if result is None:
        raise ValueError(f"chunk_id not found: {chunk_id}")
    return result


@register_tool(
    name="passage_lookup",
    description=(
        "Get every chunk overlapping a Bible passage range. Returns chunks "
        "from all sources (ULT, UST, TN, TQ, linked TW articles, Aquifer "
        "study notes, etc.)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reference": {
                "type": "string",
                "description": "Bible reference, e.g. 'Titus 1:1', 'Romans 3:24-25', 'Ruth chapter 1'.",
            },
            "lang": {"type": "string", "default": "en"},
        },
        "required": ["reference"],
    },
)
def _passage_lookup(args: dict, db: sqlite3.Connection) -> dict:
    ref = args.get("reference", "").strip()
    if not ref:
        raise ValueError("'reference' is required")
    passages = parse_references(ref)
    if not passages:
        raise ValueError(f"could not parse Bible reference: {ref!r}")

    where = " OR ".join(
        "(passage_refs.start_bbcccvvv <= ? AND passage_refs.end_bbcccvvv >= ?)"
        for _ in passages
    )
    params: list = []
    for s, e in passages:
        params.extend([e, s])
    rows = db.execute(
        f"""
        SELECT DISTINCT chunks.id
        FROM chunks
        JOIN passage_refs ON passage_refs.doc_id = chunks.doc_id
        WHERE {where}
        ORDER BY passage_refs.start_bbcccvvv
        """,
        params,
    ).fetchall()
    cards = citations_mod.resolve_many(db, [r[0] for r in rows])
    return {
        "reference": ref,
        "passages": [list(p) for p in passages],
        "chunks": [chunk_preview_from_card(c, lang=args.get("lang", "en")) for c in cards],
    }


@register_tool(
    name="entity_lookup",
    description=(
        "Find chunks about a person, place, or biblical concept. Merges Door43 "
        "Translation Words and Aquifer ACAI entity tags so a single name "
        "returns hits from both taxonomies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Entity name (e.g. 'Boaz', 'justification')."},
            "type": {
                "type": "string",
                "enum": ["any", "person", "place", "keyterm", "deity", "event"],
                "default": "any",
            },
            "lang": {"type": "string", "default": "en"},
        },
        "required": ["entity"],
    },
)
def _entity_lookup(args: dict, db: sqlite3.Connection) -> dict:
    entity = args.get("entity", "").strip()
    if not entity:
        raise ValueError("'entity' is required")
    type_ = (args.get("type") or "any").lower()
    lang = args.get("lang", "en")

    candidates: set[str] = set()
    # Door43 TW: term:<lowercase>
    candidates.add(f"term:{entity.lower()}")
    # Aquifer ACAI: acai:<type>:<entity>
    if type_ == "any":
        for t in ("person", "place", "keyterm", "deity", "event"):
            candidates.add(f"acai:{t}:{entity}")
            candidates.add(f"acai:{t}:{entity.lower()}")
    else:
        candidates.add(f"acai:{type_}:{entity}")
        candidates.add(f"acai:{type_}:{entity.lower()}")

    placeholders = ",".join("?" * len(candidates))
    rows = db.execute(
        f"""
        SELECT DISTINCT chunks.id
        FROM chunks
        JOIN documents ON documents.id = chunks.doc_id
        JOIN tags ON tags.doc_id = documents.id
        WHERE tags.tag IN ({placeholders})
        LIMIT 100
        """,
        list(candidates),
    ).fetchall()
    cards = citations_mod.resolve_many(db, [r[0] for r in rows])
    return {
        "entity": entity,
        "type": type_,
        "lang": lang,
        "matched_tags_searched": sorted(candidates),
        "chunks": [chunk_preview_from_card(c, lang=lang) for c in cards],
    }


@register_tool(
    name="tree_listing",
    description=(
        "Walk one of the perspective trees over the corpus. Returns the children "
        "of the requested node (intermediate) or the chunks at this leaf "
        "(terminal). Use to navigate the corpus structurally — by Bible "
        "book/chapter/verse, by source, by content kind, by entity, etc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tree": {
                "type": "string",
                "enum": ["scripture", "source", "kind", "term", "methodology", "pericope", "aquifer"],
            },
            "path": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "lang": {"type": "string", "default": "en"},
        },
        "required": ["tree"],
    },
)
def _tree_listing(args: dict, db: sqlite3.Connection) -> dict:
    tree = args.get("tree", "")
    builder = BUILDERS.get(tree)
    if builder is None:
        raise ValueError(f"unknown tree: {tree!r}")
    path = args.get("path") or []
    if not isinstance(path, list):
        raise ValueError("'path' must be a list of strings")
    lang = args.get("lang", "en")
    if not path:
        return builder.root(db, lang=lang)
    return builder.descend(db, [str(p) for p in path], lang=lang)


@register_tool(
    name="ask",
    description=(
        "Full retrieval-augmented generation: question -> cited answer. "
        "Off by default in MCP (the consuming agent is itself an LLM); enable "
        "with BTMCP_EXPOSE_ASK=1 on the server. Prefer 'search' + "
        "'get_chunk' for agentic workflows so you synthesize from raw sources."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "lang": {"type": "string", "default": "en"},
            "source": {"type": "string", "enum": ["all", "door43", "aquifer"], "default": "all"},
        },
        "required": ["question"],
    },
)
def _ask(args: dict, db: sqlite3.Connection) -> dict:
    question = args.get("question", "").strip()
    if not question:
        raise ValueError("'question' is required")

    analysis = analyze(question)
    query_vec = None
    if has_vec(db):
        try:
            from indexer.embed import embed_texts
            query_vec = embed_texts([question])[0]
        except Exception:
            pass

    hits = retrieve(db, analysis, query_vec=query_vec, source_filter=args.get("source", "all"))
    cards = citations_mod.resolve_many(db, [h.chunk_id for h in hits])

    from query.synthesize import synthesize  # lazy: openai SDK
    synth = synthesize(question, cards, db=db)

    by_id = {c.chunk_id: c for c in cards}
    citations_out = []
    for n, cid in enumerate(synth["citations"], start=1):
        card = by_id.get(cid)
        if card is None:
            continue
        preview = chunk_preview_from_card(card, lang=args.get("lang", "en"))
        preview["n"] = n
        citations_out.append(preview)

    return {
        "question": question,
        "answer": synth["answer"],
        "citations": citations_out,
        "confidence": synth["confidence"],
    }
