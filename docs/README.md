# bt-docker-mcp — Architectural Documentation

This folder is the architectural map of bt-docker-mcp. The top-level
[../README.md](../README.md) is the project's elevator pitch and quick-start.
The docs here are the deeper dive — what's actually happening behind every
`python -m query.ask "…"` invocation, why the system is shaped the way it is,
and where to look when something needs to change.

## Reading order

If you're new, read in this order. Each builds on the one before.

| | What it covers | When to read |
|---|---|---|
| **1. [architecture.md](architecture.md)** | Two-layer mental model, end-to-end query flow, ingest flow, module map | First. The map. |
| **2. [data-pipeline.md](data-pipeline.md)** | Layer 1 — sources, staging, schema, build, embed | When you change what gets indexed or how |
| **3. [query-pipeline.md](query-pipeline.md)** | Layer 2 — analyzer, six retrievers, RRF, intent weights, synthesis, citations | When you change how questions become answers |
| **4. [eval.md](eval.md)** | Eval set format, runner, metrics, output | When tuning retrieval or synthesis quality |
| **5. [decisions.md](decisions.md)** | Design decisions chronicled with the *why* | When you need to understand why something is the way it is — or when reverting feels tempting |
| **6. [client-integration.md](client-integration.md)** | API contract + tree URL scheme + integration guide | When building a frontend (Netlify-hosted React/Astro/SvelteKit) against the backend API |
| **7. [server.md](server.md)** | Backend implementation plan (FastAPI, deployment, route handlers) | When implementing or modifying the HTTP layer that fronts the index |

## At a glance

```
                user question
                     │
                     ↓
   ┌──────────────────────────────────────────┐
   │  Layer 2 — Query pipeline                │     query/
   │  analyzer → retrieve → synthesize        │     query/
   └────────────────┬─────────────────────────┘
                    │ reads
   ┌────────────────▼─────────────────────────┐
   │  Layer 1 — Index (one SQLite file)       │     indexer/
   │  documents · chunks · FTS5 · vectors     │     indexer/
   │  passage_refs · tags · meta              │     indexer/
   └────────────────▲─────────────────────────┘
                    │ writes (one-time per source)
   ┌────────────────┴─────────────────────────┐
   │  Ingest                                  │     ingest/
   │  Door43 · Aquifer  → staging/  → build   │     ingest/
   └──────────────────────────────────────────┘
                    ▲
                    │ pulls from
       Door43 (git.door43.org)   BibleAquifer (github.com)
```

The right-hand column is the actual repo path that owns each piece. Layer 1 is
data infrastructure; Layer 2 is the conversation surface.

## See also

- [`../README.md`](../README.md) — project overview, quick start, deployment
- [`../eval/set/v1.yaml`](../eval/set/v1.yaml) — the curated eval set
- [`../indexer/schema.sql`](../indexer/schema.sql) — the schema, source of truth
