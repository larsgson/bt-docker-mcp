# Backend / server implementation plan

How we will (or are) handling the backend side: a FastAPI service over
the existing SQLite + sqlite-vec index, deployed on Railway,
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
         │  Railway                        │
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
- **FastAPI on Railway** — Railway supports persistent volumes for
  the SQLite file, environment secrets for LLM API keys, and Dockerfile
  builds with healthchecks out of the box.

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

The rule: any code path that consumes a server-side AI provider key
(OpenAI for embeddings, Groq / OpenAI for synthesis) is gated behind a
shared secret. Read-only deterministic endpoints stay public.

| Surface | Hits AI provider key | Gate |
|---|---|---|
| `GET /api/health` / `/api/chunk/*` / `/api/tree/*` | no | open |
| `GET /api/search` (default — FTS only) | no | open |
| `GET /api/search?semantic=true` | yes (OpenAI embeddings) | password |
| `POST /api/ask` | yes (LLM synthesis) | password |
| MCP `tools/call` name=`get_chunk` / `passage_lookup` / `entity_lookup` / `tree_listing` | no | open |
| MCP `tools/call` name=`search` (default) | no | open |
| MCP `tools/call` name=`search` w/ `use_semantic: true` | yes | password |
| MCP `tools/call` name=`ask` (when exposed) | yes | password |

Configure the secret with `BTMCP_API_PASSWORD`. Set it on Railway as a
service env var; if the variable is unset/empty the gate is disabled
(useful only for local dev).

Clients pass it on every gated request via either header:

```
Authorization: Bearer <password>
X-API-Key: <password>
```

Stdio MCP transport (`python -m server.mcp.stdio`) is local-only and
skips the gate by design. Origin allowlist + CORS is still enforced
separately (see below) for browser-side calls.

Implementation: `server/auth.py` defines `verify()`, the FastAPI
dependency `require_password`, and `mcp_tool_call_uses_ai()` which the
MCP dispatcher consults before calling tool handlers.

## Rate limiting

Every public endpoint has a per-IP rate limit (slowapi, in-process
storage). Auth is evaluated first; an unauthorized client gets 401
without consuming a rate-limit slot.

Defaults — overridable via env vars at process startup:

| Env var | Default | Applies to |
|---|---|---|
| `BTMCP_RATE_LIMIT_ASK` | `10/minute` | `POST /api/ask` |
| `BTMCP_RATE_LIMIT_SEARCH` | `60/minute` | `GET /api/search` |
| `BTMCP_RATE_LIMIT_READ` | `120/minute` | `/api/health`, `/api/chunk/*`, `/api/tree/*` |
| `BTMCP_RATE_LIMIT_MCP` | `60/minute` | `POST /mcp` (the JSON-RPC endpoint as a whole) |

Format is slowapi's `<count>/<period>` (period: `second`, `minute`,
`hour`, `day`). Excess requests get HTTP `429 Too Many Requests`.

Per-IP attribution honors `X-Forwarded-For` so each client gets its own
bucket behind Railway's load balancer; without that header `request.client.host`
is used.

Caveat: MCP `tools/call` is rate-limited at the endpoint level, not per
tool name. An authorized client can spend 60 calls/min total across
`ask`, `search`, etc. If you need stricter per-tool limits (e.g. 10/min
for `ask` via MCP too), add the check in `server/mcp/server.py:_handle_one`
after the auth gate. For v1 the auth gate plus endpoint-level cap is
the intended trade.

Storage is in-process — single-instance Railway deployment is the
assumed shape. Horizontal scaling would need a shared backend (Redis);
see slowapi's `storage_uri` option.

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

## Deployment (Railway)

### Files in this repo

- `Dockerfile` — single-stage Python 3.12 image; binds `0.0.0.0:${PORT}`,
  declares `VOLUME ["/data"]`, defaults `INDEX_DB_PATH=/data/index.db`.
- `railway.toml` — pins build (Dockerfile) and deploy (healthcheck on
  `/api/health`, restart policy). Railway auto-detects the Dockerfile,
  but pinning is more reproducible.

### One-time setup

1. **Create a Railway project from the repo.** New project → Deploy from
   GitHub → pick `bt-docker-mcp`. Railway picks up `railway.toml` and
   builds the Dockerfile.

2. **Add a persistent volume.** Service → Volumes → Add Volume:
   - mount path: `/data`
   - size: 1 GB to start (corpus is ~10 MB today; bump as it grows)

3. **Set service environment variables.** Required:
   - `GROQ_API_KEY` — primary synthesis LLM
   - `OPENAI_API_KEY` — fallback LLM + embeddings
   - `BTMCP_API_PASSWORD` — shared secret for LLM/embedding endpoints
     (see [Auth strategy](#auth-strategy))
   - `CORS_ALLOWED_ORIGINS` — comma-separated allowlist for the browser
     frontend (e.g. `https://yourapp.netlify.app,https://*.netlify.app`)

   Optional:
   - `BTMCP_EXPOSE_ASK=1` — list `ask` in MCP `tools/list` (it's still
     password-gated regardless)
   - `BTMCP_GROQ_MODEL`, `BTMCP_OPENAI_MODEL`, `BTMCP_EMBEDDING_MODEL` —
     model overrides; defaults are sensible

   Railway already injects `PORT`; the Dockerfile honors it.

4. **Bootstrap the index** (one-time — see next section).

### Bootstrap (initial index)

The volume starts empty. The HTTP server boots fine but `/api/health`
reports `status: "uninitialized"` until the index exists. Two ways to
populate it:

**Option A — build locally, upload to volume.** Fastest. Run on your
laptop, then push the file:
```bash
.venv/bin/python -m ingest.cli --source door43 --source aquifer --book TIT --book RUT
.venv/bin/python -m indexer.build --source ingest/_staging --reset
.venv/bin/python -m indexer.embed
# Upload indexer/index.db → /data/index.db on the Railway volume
# (Railway dashboard → Volumes → Upload, or `railway volume` CLI)
```

**Option B — build inside the container.** Slower, but no manual upload
and the same code path the cron will use later:
```bash
railway shell             # or: railway run -s <service>
python -m ingest.cli --source door43 --source aquifer --book TIT --book RUT
python -m indexer.build --source ingest/_staging --reset
python -m indexer.embed
```
This runs ingest+build+embed against the live volume. `OPENAI_API_KEY`
must be set on the service for the embedding step. Subsequent re-runs
are incremental — `indexer.build` is idempotent and `indexer.embed`
only embeds chunks without vectors.

After either option `/api/health` flips to `status: "ok"`.

### Freshness (cron — built, not yet enabled)

`python -m indexer.refresh` runs the same three steps the bootstrap does
(ingest → build → embed), each one incremental:

- ingest re-pulls upstream into `ingest/_staging` (overwrites)
- `indexer.build` is idempotent (content-derived doc ids; DELETE+INSERT)
- `indexer.embed` only embeds chunks that lack a vector

So daily runs cost near-zero unless upstream actually changed something.

Configure the corpus scope via env vars on the cron service (same
defaults as the current local corpus):

| Env var | Default |
|---|---|
| `BTMCP_REFRESH_SOURCES` | `door43 aquifer` (space-separated) |
| `BTMCP_REFRESH_BOOKS` | `TIT RUT` (space-separated USFM codes) |
| `BTMCP_REFRESH_LANG` | `en` |

To enable on Railway when ready (no code change required):

1. Same project → **New Service** → "Empty Service" (or duplicate the API
   service so it inherits the Dockerfile).
2. Reuse the same `Dockerfile` — point at the repo.
3. **Service Settings → Cron Schedule**: set a cron expression
   (e.g. `0 6 * * *` for 06:00 UTC daily).
4. **Custom Start Command**: `python -m indexer.refresh`
5. **Volumes**: mount the *same* volume as the API service at `/data`.
6. **Variables**: set `OPENAI_API_KEY` (required for embed) and any
   `BTMCP_REFRESH_*` overrides.

Until the cron service exists, the API service runs against whatever
index was last bootstrapped — no automatic refresh happens. The HTTP
service picks up file changes automatically once the cron writes a new
`index.db` (per-request SQLite connections).

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
