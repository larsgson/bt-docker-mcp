# Backend / server implementation plan

How we will (or are) handling the backend side: a FastAPI service over
the existing SQLite + sqlite-vec index, deployed on fly.io / Railway,
fronting the API contract documented in
[`client-integration.md`](client-integration.md).

This is the planning + reference doc for whoever implements the backend.
Cross-references the existing CLI codebase: most of the work is wrapping
existing modules in HTTP handlers, not new logic.

## Deployment shape

```
                       Internet
                          │
                          ↓
         ┌─────────────────────────────────┐
         │  Netlify (static frontend)      │
         │  yourapp.netlify.app            │
         │  React/Astro/SvelteKit bundle   │
         └────────────┬────────────────────┘
                      │ HTTPS, JSON
                      ↓
         ┌─────────────────────────────────┐
         │  fly.io  (or Railway)           │
         │  api.yourapp.dev                │
         │                                 │
         │  ┌─────────────────────────┐    │
         │  │  FastAPI app (uvicorn)  │    │
         │  │  /api/* routes          │    │
         │  └────────────┬────────────┘    │
         │               │                 │
         │  ┌────────────▼────────────┐    │
         │  │  /data/index.db         │    │  ← mounted volume
         │  │  (SQLite + sqlite-vec)  │    │     (~10–500 MB)
         │  └─────────────────────────┘    │
         │                                 │
         │  Env: GROQ_API_KEY,             │
         │       OPENAI_API_KEY            │
         └─────────────────────────────────┘
                      │
                      │ during synthesis only
                      ↓
            Groq → OpenAI fallback
            (LLM provider APIs)
```

The split is deliberate:

- **Static frontend on Netlify** — Netlify's strength is serving static
  bundles globally. No SQLite there.
- **FastAPI on fly.io / Railway** — both support persistent volumes for
  the SQLite file, plus environment secrets for LLM API keys, plus zero
  cold-start when machines are kept warm.

Don't try to put the index inside Netlify Functions: 50 MB bundle limit
+ ephemeral filesystem make it impractical. The index file alone exceeds
that at any meaningful scale.

## What's already built

The CLI (`query.ask`) implements the full pipeline. Backend implementation
is mostly **HTTP handlers wrapping existing modules**, not new logic.

| Endpoint | Existing module | New code needed |
|---|---|---|
| `GET /api/health` | — | trivial |
| `GET /api/tree/<tree_name>` | — | tree-specific SQL helper |
| `GET /api/tree/<path>` | — | tree-specific SQL helper |
| `GET /api/chunk/<chunk_id>` | `indexer.citations` | extend resolver to include cross-refs and all_paths |
| `GET /api/search` | `query.analyzer`, `query.retrieve`, `indexer.citations` | thin wrapper |
| `POST /api/ask` | `query.analyzer`, `query.retrieve`, `query.synthesize`, `query.llm`, `indexer.citations` | thin wrapper |
| `POST /api/ask` (SSE) | same as ask + streaming wrapper around `chat_completion` | streaming reformat |

Net new code: maybe **600–800 lines of FastAPI**. The hard parts (retrieval,
synthesis, citation validation) are already done.

## Module layout

```
server/
├── __init__.py
├── app.py                  FastAPI app instance, route registration
├── routes/
│   ├── health.py           /api/health
│   ├── trees.py            /api/tree/*
│   ├── chunks.py           /api/chunk/*
│   ├── search.py           /api/search
│   └── ask.py              /api/ask (incl. SSE)
├── trees/
│   ├── __init__.py         tree registry
│   ├── scripture.py        Scripture tree builder
│   ├── source.py           Source tree builder
│   ├── kind.py             Kind tree builder
│   ├── term.py             Term tree builder
│   ├── methodology.py      Methodology tree builder
│   ├── pericope.py         Pericope view
│   ├── aquifer.py          Aquifer collection view
│   └── language.py         Language tree (trivial today)
├── resolver.py             chunk_id → primary_path + all_paths + cross_refs
├── pagination.py           offset/limit helpers
├── deps.py                 FastAPI Depends() — db connection, auth
├── cors.py                 CORS configuration (env-driven origin allowlist)
└── requirements.txt        fastapi + uvicorn[standard] + (existing)
```

The `server/` directory is parallel to `query/`, `indexer/`, `ingest/`.
It depends on those but they don't depend on it — so the CLI keeps
working unchanged.

## Per-route implementation

### `GET /api/health`

```python
@router.get("/health")
def health(db: Connection = Depends(get_db)):
    schema = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    indexed = db.execute("SELECT value FROM meta WHERE key='indexed_at'").fetchone()
    return {
        "status": "ok",
        "schema_version": schema[0] if schema else None,
        "indexed_at": int(indexed[0]) if indexed else None,
    }
```

### `GET /api/tree/<tree_name>` and `GET /api/tree/<tree_name>/<path...>`

Tree-specific. Each tree module exposes:

```python
class TreeBuilder:
    def root(self, lang: str) -> dict: ...
    def descend(self, lang: str, path: list[str]) -> dict: ...
```

The route handler dispatches to the right builder. Return shape distinguishes
intermediate nodes (`children`) from leaves (`chunks`).

Examples of the underlying SQL:

**Scripture tree — testament listing:**

```sql
-- Books with content, partitioned by testament:
SELECT DISTINCT
  passage_refs.start_bbcccvvv / 1000000 AS book_num
FROM passage_refs
ORDER BY book_num;
```

Then map `book_num` → testament (`<= 39` → ot, else nt) and
→ USFM code via `BOOK_NUMBERS`.

**Scripture tree — verses in chapter:**

```sql
SELECT DISTINCT
  (start_bbcccvvv / 1000) % 1000 AS chapter,
  start_bbcccvvv % 1000 AS verse
FROM passage_refs
WHERE start_bbcccvvv / 1000000 = :book_num
  AND (start_bbcccvvv / 1000) % 1000 = :chapter
ORDER BY verse;
```

**Scripture tree — chunks at a verse:**

```sql
SELECT DISTINCT
  chunks.id, documents.title, documents.metadata
FROM chunks
JOIN passage_refs ON passage_refs.doc_id = chunks.doc_id
JOIN documents    ON documents.id = chunks.doc_id
WHERE passage_refs.start_bbcccvvv <= :bbcccvvv
  AND passage_refs.end_bbcccvvv   >= :bbcccvvv
ORDER BY documents.source_path;
```

**Source tree — providers:**

```sql
SELECT
  CASE
    WHEN EXISTS (SELECT 1 FROM tags t WHERE t.doc_id = documents.id AND t.tag = 'resource:aquifer')
    THEN 'aquifer'
    ELSE 'door43'
  END AS provider,
  COUNT(*) AS doc_count
FROM documents
GROUP BY provider;
```

**Source tree — Door43 resources:**

```sql
SELECT REPLACE(tags.tag, 'resource:', '') AS resource, COUNT(DISTINCT documents.id) AS n
FROM tags JOIN documents ON documents.id = tags.doc_id
WHERE tags.tag IN ('resource:ult','resource:ust','resource:tn','resource:tq','resource:tw','resource:ta')
GROUP BY tags.tag;
```

**Source tree — Aquifer repos:**

```sql
SELECT REPLACE(tags.tag, 'aquifer:', '') AS repo, COUNT(DISTINCT documents.id) AS n
FROM tags JOIN documents ON documents.id = tags.doc_id
WHERE tags.tag LIKE 'aquifer:%'
GROUP BY tags.tag;
```

**Kind tree — top level:**

```sql
SELECT REPLACE(tag, 'kind:', '') AS kind, COUNT(DISTINCT doc_id) AS n
FROM tags WHERE tag LIKE 'kind:%' GROUP BY tag;
```

**Term tree — entity types (merged across Door43 + Aquifer):**

```sql
SELECT REPLACE(tag, 'category:', '') AS type, COUNT(DISTINCT doc_id) AS n
FROM tags WHERE tag LIKE 'category:%'
GROUP BY tag

UNION ALL

SELECT
  SUBSTR(tag, LENGTH('acai:') + 1, INSTR(SUBSTR(tag, LENGTH('acai:') + 1), ':') - 1) AS type,
  COUNT(DISTINCT doc_id) AS n
FROM tags WHERE tag LIKE 'acai:%'
GROUP BY type;
```

**Term tree — entities of one type (Door43):**

```sql
SELECT REPLACE(tag, 'term:', '') AS entity, COUNT(DISTINCT doc_id) AS n
FROM tags
WHERE tag LIKE 'term:%'
  AND doc_id IN (SELECT doc_id FROM tags WHERE tag = 'category:keyterm' OR tag = 'category:kt')
GROUP BY tag;
```

**Term tree — chunks for "Paul" (merged Door43 + Aquifer):**

```sql
SELECT DISTINCT chunks.id, documents.title
FROM chunks
JOIN documents ON documents.id = chunks.doc_id
JOIN tags      ON tags.doc_id = documents.id
WHERE tags.tag IN ('term:paul', 'acai:person:Paul');
```

**Methodology tree — sections:**

```sql
SELECT REPLACE(tag, 'section:', '') AS section, COUNT(DISTINCT doc_id) AS n
FROM tags WHERE tag LIKE 'section:%' GROUP BY tag;
```

**Pericope view — chunks at a range:**

```sql
SELECT DISTINCT chunks.id, documents.title, GROUP_CONCAT(t.tag) AS tag_csv
FROM chunks
JOIN documents ON documents.id = chunks.doc_id
JOIN passage_refs ON passage_refs.doc_id = chunks.doc_id
LEFT JOIN tags AS t ON t.doc_id = chunks.doc_id
WHERE passage_refs.start_bbcccvvv <= :end
  AND passage_refs.end_bbcccvvv   >= :start
GROUP BY chunks.id;
```

### `GET /api/chunk/<chunk_id>`

This is the centerpiece — extends `indexer.citations.resolve` with full
body, all tree paths, and cross-references.

```python
def resolve_chunk(db: Connection, chunk_id: str) -> dict | None:
    row = db.execute("""
        SELECT chunks.id, chunks.doc_id, chunks.body,
               documents.title, documents.source_path, documents.metadata
        FROM chunks JOIN documents ON documents.id = chunks.doc_id
        WHERE chunks.id = ?
    """, (chunk_id,)).fetchone()
    if not row:
        return None

    tags = [r[0] for r in db.execute(
        "SELECT tag FROM tags WHERE doc_id = ? ORDER BY tag", (row[1],)
    )]
    passages = db.execute(
        "SELECT start_bbcccvvv, end_bbcccvvv FROM passage_refs WHERE doc_id = ?",
        (row[1],)
    ).fetchall()

    paths = derive_tree_paths(tags, passages)        # see resolver.py
    cross = derive_cross_refs(db, row[1], tags, passages)

    return {
        "chunk_id": chunk_id,
        "doc_id": row[1],
        "body": row[2],
        "title": row[3],
        "passage": humanize_passage(passages),
        "passage_refs": passages,
        "tags": tags,
        "kind": kind_from_tags(tags),
        "primary_path": paths[0] if paths else None,
        "all_paths": paths,
        "permalink": f"/c/{chunk_id}",
        "cross_refs": cross,
    }
```

`derive_tree_paths` is a small function that returns ordered tree paths;
"primary" is the first (most natural) — typically Scripture if there's a
passage, otherwise Source.

`derive_cross_refs` runs three SQL queries:

```python
def derive_cross_refs(db, doc_id, tags, passages):
    out = {"passage": [], "support_ref": [], "term": []}

    # Other chunks at the same passages
    if passages:
        out["passage"] = chunks_at_passages(db, passages, exclude_doc_id=doc_id)

    # TA modules referenced from this chunk's support_ref tags
    sup_tags = [t for t in tags if t.startswith("support_ref:")]
    if sup_tags:
        out["support_ref"] = ta_modules_for_support_refs(db, sup_tags)

    # TW articles for this chunk's terms (or vice versa)
    term_tags = [t for t in tags if t.startswith("term:") or t.startswith("acai:")]
    if term_tags:
        out["term"] = tw_articles_for_terms(db, term_tags, exclude_doc_id=doc_id)

    return out
```

### `GET /api/search`

Wraps the existing CLI logic. Roughly:

```python
@router.get("/search")
def search(
    q: str,
    lang: str = "en",
    kind: str | None = None,
    book: str | None = None,
    source: Literal["all","door43","aquifer"] = "all",
    top_k: int = 10,
    no_vec: bool = False,
    db: Connection = Depends(get_db),
):
    analysis = analyze(q)
    # Apply faceted filters via additional tag candidates
    if kind:
        analysis.tags.append(f"kind:{kind}")
    if book:
        analysis.tags.append(f"book:{book}")

    query_vec = None
    if not no_vec and has_vec(db):
        query_vec = embed_texts([q])[0]

    hits = retrieve(db, analysis, top_k=top_k, query_vec=query_vec, source_filter=source)
    cards = citations.resolve_many(db, [h.chunk_id for h in hits])
    enriched = [enrich_with_paths(c, h) for c, h in zip(cards, hits)]

    return {
        "query": q,
        "lang": lang,
        "filters": {"kind": kind, "book": book, "source": source},
        "analysis": asdict(analysis),
        "hits": enriched,
        "total": len(enriched),
    }
```

### `POST /api/ask`

Same shape, calls synthesize after retrieve.

```python
@router.post("/ask")
def ask(req: AskRequest, db: Connection = Depends(get_db)):
    analysis = analyze(req.question)
    query_vec = None
    if has_vec(db):
        query_vec = embed_texts([req.question])[0]
    hits = retrieve(db, analysis, query_vec=query_vec,
                    source_filter=req.scope.source if req.scope else "all")
    cards = citations.resolve_many(db, [h.chunk_id for h in hits])
    synth = synthesize(req.question, cards, db=db)

    citations_out = []
    for n, chunk_id in enumerate(synth["citations"], start=1):
        card_extra = enrich_with_paths(next(c for c in cards if c.chunk_id == chunk_id))
        citations_out.append({"n": n, **card_extra})

    return {
        "question": req.question,
        "answer": synth["answer"],
        "citations": citations_out,
        "confidence": synth["confidence"],
        "analysis": asdict(analysis),
    }
```

### `POST /api/ask` (SSE streaming)

The trickier endpoint. The LLM client (`query.llm.chat_completion`)
currently returns a complete string. To stream tokens, we need to update
it to optionally return an iterator.

```python
def chat_completion_stream(...):
    """Same as chat_completion but yields token chunks."""
    resp = client.chat.completions.create(..., stream=True)
    for chunk in resp:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
```

Then the SSE handler:

```python
@router.post("/ask")
async def ask(req: AskRequest, accept: str = Header("application/json"), ...):
    if "text/event-stream" not in accept:
        return ask_sync(req, db)  # JSON path

    async def events():
        yield sse("status", {"phase": "analyzing"})
        analysis = analyze(req.question)
        yield sse("status", {"phase": "retrieving", "intent": analysis.intent})

        query_vec = embed_texts([req.question])[0] if has_vec(db) else None
        hits = retrieve(db, analysis, query_vec=query_vec, ...)
        cards = citations.resolve_many(db, [h.chunk_id for h in hits])

        yield sse("hits", {
            "count": len(cards),
            "preview": [card_preview(c) for c in cards],
        })

        yield sse("status", {"phase": "synthesizing"})
        accumulated = ""
        for token in synthesize_stream(req.question, cards, db=db):
            accumulated += token
            yield sse("token", {"text": token})

        # Validate citations after the full answer
        result = validate_citations(accumulated, cards)
        yield sse("complete", {
            "answer": result["answer"],
            "citations": result["citations"],
            "confidence": result["confidence"],
        })

    return StreamingResponse(events(), media_type="text/event-stream")
```

`synthesize_stream` is a streaming variant of the existing `synthesize`.
The LLM token stream goes straight to the wire; final validation
(citation drop) happens after the full answer is collected.

## Auth strategy

Three options, by trust profile:

1. **Public, rate-limited.** Most read endpoints (`/api/tree`, `/api/chunk`,
   `/api/search`) need no auth — content is public anyway. Apply a per-IP
   rate limit (e.g., `slowapi`) to prevent abuse.

2. **Bearer token gate on `/api/ask`.** LLM calls cost money. Gate `/ask`
   behind a token your frontend embeds. Anyone with the token can use
   the LLM; revoke / rotate as needed.

3. **Origin allowlist.** Combined with CORS — only requests from your
   Netlify domains are accepted. Easy bypass via `curl --origin`, but
   stops casual browser-side abuse.

Recommended for v1: **CORS allowlist + per-IP rate limit**, no token. Add
token if billing pressure shows up.

## CORS

```python
from fastapi.middleware.cors import CORSMiddleware

allowed = [
    "https://yourapp.netlify.app",
    "https://*.netlify.app",        # preview deploys
    "https://yourapp.dev",
    "http://localhost:3000",
    "http://localhost:5173",        # vite default
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,        # no cookies; bearer token via header is fine without credentials
)
```

Allowlist is read from `CORS_ALLOWED_ORIGINS` env var (comma-separated)
in production.

## Caching

Tree data and chunks are highly cacheable; ask responses aren't.

```python
@router.get("/tree/{...:path}")
def get_tree(...):
    response = build_tree_response(...)
    return JSONResponse(response, headers={
        "Cache-Control": "public, max-age=300",   # 5 min
        "ETag": etag_for_index(),                  # invalidate on rebuild
    })

@router.get("/chunk/{chunk_id}")
def get_chunk(...):
    return JSONResponse(response, headers={
        "Cache-Control": "public, max-age=3600",  # 1 hr
        "ETag": chunk_id,                          # chunk_ids are stable
    })

@router.post("/ask")
def ask(...):
    return JSONResponse(response, headers={
        "Cache-Control": "no-store",
    })
```

The `etag_for_index` is `meta.indexed_at` plus `meta.embedding_model` —
changes whenever the corpus changes. Browsers + Netlify edge cache
play well with this.

## Index invalidation on rebuild

`indexer.build` updates `meta.indexed_at`. The server should poll or
watch this:

- **Simple**: `etag_for_index()` reads `meta.indexed_at` per-request. Cheap
  (one indexed lookup). Stale-but-correct.
- **Better**: in-process invalidation hook on file mtime change of
  `index.db`. Works for single-machine fly.io.
- **Best**: ingest pipeline POSTs to `/admin/invalidate` after build to
  flush in-memory caches. Overkill for v1.

For v1: just read `meta.indexed_at` on each request. fly.io's volume mounts
make this fast.

## Deployment

### fly.io recipe

```toml
# fly.toml
app = "bt-docker-mcp-api"
primary_region = "ord"

[build]
  dockerfile = "Dockerfile"

[env]
  PORT = "8080"
  PYTHONUNBUFFERED = "1"

[[mounts]]
  source = "bt_docker_mcp_data"
  destination = "/data"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = true
  auto_start_machines = true
  min_machines_running = 0
```

Volume holds the SQLite file at `/data/index.db`. Set:
- `flyctl volumes create bt_docker_mcp_data --size 1` (1 GB; adjust as corpus grows)
- `flyctl secrets set GROQ_API_KEY=… OPENAI_API_KEY=… CORS_ALLOWED_ORIGINS=…`

Initial data load: ingest + build locally, then `flyctl ssh sftp shell`
or volume snapshot to copy `index.db` into the volume. Or run ingest
inside the container itself (set up a cron in fly.io).

### Railway recipe

```dockerfile
# Dockerfile (existing — needs only updates)
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y ca-certificates sqlite3 && rm -rf /var/lib/apt/lists/*
COPY indexer/requirements.txt ingest/requirements.txt query/requirements.txt server/requirements.txt /app/
RUN pip install --no-cache-dir \
    -r indexer/requirements.txt \
    -r ingest/requirements.txt \
    -r query/requirements.txt \
    -r server/requirements.txt
COPY indexer ingest query server /app/
ENV INDEX_DB_PATH=/data/index.db
EXPOSE 8080
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

Railway env vars: `GROQ_API_KEY`, `OPENAI_API_KEY`, `CORS_ALLOWED_ORIGINS`.
Mount a Railway volume at `/data`. Same load strategy as fly.io.

## Eval framework as contract test

The existing `eval/run.py` invokes `query/analyzer`, `query/retrieve`,
`query/synthesize` directly. After adding the HTTP layer, port the
runner to also support **HTTP mode**:

```bash
python -m eval.run                                    # in-process (current)
python -m eval.run --http http://localhost:8080       # against running server
```

In HTTP mode it calls `POST /api/ask` for each case. Pass rate should
match in-process mode exactly — anything else is a server-side bug.
This makes the API a contract that the eval set anchors. Useful before
deploying.

## Phased implementation

In rough priority order:

1. **`/api/health` + `/api/chunk/<chunk_id>`** (~half-day)
   - Get the FastAPI skeleton up
   - Chunk resolver + tree-path derivation
   - Smoke: hit a chunk_id, see full body + paths
2. **`/api/tree/source` + `/api/tree/scripture`** (~1 day)
   - The two highest-value trees
   - Pagination
3. **`/api/search`** (~half-day)
   - Wraps existing analyzer + retrieve + citations.resolve
   - Faceted filters via tag candidates
4. **`/api/ask` (sync)** (~half-day)
   - Wraps existing synthesize
   - Citations enriched with tree paths
5. **CORS + auth + rate-limiting** (~half-day)
6. **fly.io / Railway deploy** (~half-day)
   - Dockerfile (mostly exists), volume, secrets
7. **Eval HTTP mode** (~half-day)
   - Validates the API matches in-process behavior
8. **`/api/tree/term`** (~half-day)
   - Door43 + Aquifer entity merge
9. **Other trees** as needed (~half-day each)
10. **`/api/ask` SSE streaming** (~1 day)
    - Stream LLM tokens, validate citations after

Total to a deployable minimum: **~3 days of focused work**.

To "feature parity with the CLI": **~5 days**.

## What this design preserves

- **Auditable provenance.** Every API call that returns chunks goes through
  the same retrieval logic the CLI uses. Citations are validated identically.
  See [`architecture.md`](architecture.md) for why this matters.
- **Source-agnostic content shape.** All tree builders query the
  `kind:*` taxonomy where possible, so adding a new source repository
  (Aquifer or otherwise) doesn't require new tree code.
- **Stable permalinks.** `/c/<chunk_id>` and `/d/<doc_id>` work across
  rebuilds. Tree paths can restructure without breaking citations.
- **Separation of read vs synth.** `/api/search` and `/api/tree` are
  cheap, deterministic, cacheable. `/api/ask` is the only LLM-cost
  endpoint, naturally rate-limited or token-gated.

## See also

- [`client-integration.md`](client-integration.md) — frontend consumer perspective on the same API
- [`architecture.md`](architecture.md) — why the system is shaped this way
- [`query-pipeline.md`](query-pipeline.md) — internal logic that backs `/api/search` and `/api/ask`
- [`data-pipeline.md`](data-pipeline.md) — how the SQLite the server reads gets built
