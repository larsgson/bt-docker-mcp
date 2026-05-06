# MCP server

The bt-docker-mcp system exposes its content via the **Model Context Protocol** in
addition to the REST API. Same FastAPI process serves both surfaces; same
SQLite + retrieval logic underneath. LLM agents (Claude desktop, Cursor,
Continue, any MCP-aware client) can consume the corpus directly through
deterministic tools, alongside the Netlify frontend's REST consumption.

This doc is the reference for the MCP surface — tool catalog, transports,
auth, and how to add new tools.

## What's exposed

Six tools, mapped to the underlying retrieval pipeline:

| Tool | Purpose | Wraps |
|---|---|---|
| `search` | keyword + structured search → ranked chunks (semantic opt-in) | `query.analyzer` + `query.retrieve` + `indexer.citations` |
| `get_chunk` | full body + tree paths + cross-refs for one chunk_id | `server.resolver` |
| `passage_lookup` | all chunks overlapping a passage range | `query.retrieve.passage_search` |
| `entity_lookup` | merged Door43 TW + Aquifer ACAI search by entity name | `tags` table query |
| `tree_listing` | walk one of the eight perspective trees | tree builders |
| `ask` *(optional, off by default)* | full RAG with LLM synthesis | `query.synthesize` |

### MCP convention: no LLM calls in the default path

By design, **default tool invocations make zero model calls** — no
generative LLM, no embedding model, no API keys required. This matches
the conventions of translation-helps-mcp, aquifer-mcp, and every
data-shape MCP we've studied. The reasoning:

- The MCP **consumer** is itself an LLM agent (Claude, Cursor, etc.).
  Adding LLM work inside the server means double-LLM round-trips and
  the consuming LLM never sees the raw deterministic data.
- MCP servers are expected to behave as pure data primitives. If a
  user runs the server without an `OPENAI_API_KEY`, every default tool
  call should still work.
- Latency and cost stay predictable per tool call.

Two opt-ins move outside that default:

| Opt-in | How | Cost |
|---|---|---|
| `search` semantic re-ranking via vector ANN | pass `use_semantic: true` in the call arguments | one OpenAI embedding call (~150ms, ~$0.00002) |
| `ask` tool exposed at all | set `BTMCP_EXPOSE_ASK=1` on the server | per-call: ~1–5s + LLM token cost |

`search` with `use_semantic: false` (the default) uses FTS5 keyword,
title matching, passage-range matching, and tag filters via RRF — all
local, deterministic, sub-100ms.

## Transports

### HTTP (Streamable HTTP) — production deployment

Mounted at `/mcp` on the same FastAPI app that serves the REST API. JSON-RPC 2.0 over POST. Same auth and CORS as the REST endpoints.

```
POST https://api.yourapp.dev/mcp
Content-Type: application/json
Authorization: Bearer <optional-token>

{ "jsonrpc": "2.0", "id": 1, "method": "tools/list" }
```

This is the production surface — what aquifer-mcp deploys at
`https://aquifer.klappy.dev/mcp` and translation-helps-mcp at
`https://tc-helps.mcp.servant.bible/mcp`. Same shape, our tools.

### stdio — local Claude desktop integration

```bash
python -m server.mcp.stdio
```

Reads JSON-RPC from stdin, writes responses to stdout, line-delimited.
Used by Claude desktop's `claude_desktop_config.json` to launch the
server as a subprocess for local-only workflows. No network exposure.

## Tool catalog

Schemas use JSON Schema. Tool responses are MCP `content[]` arrays
(text + optional structured) plus a `_meta` field with downstream
budget hints.

### `search`

Run the deterministic retrieval pipeline. Returns ranked chunks.
**No LLM calls in the default path** — pure FTS5 + structured fusion.
Pass `use_semantic: true` to additionally engage vector ANN (one
embedding call per query, requires OPENAI_API_KEY on the server).

```json
{
  "name": "search",
  "description": "Search the indexed Bible-translation corpus. Returns ranked chunks; does NOT generate an answer. By default uses FTS5 keyword matching, passage-range matching, title matching, and tag filters via reciprocal rank fusion — no model calls, no API keys required. Pass `use_semantic: true` to additionally enable vector ANN.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Free-form question or keyword search. Natural language works ('what does Titus 1:1 say about being a servant'); explicit Bible references ('Romans 3:24') trigger passage filters automatically."
      },
      "lang": { "type": "string", "default": "en" },
      "kind": {
        "type": "string",
        "enum": ["scripture", "translator-note", "question", "term", "methodology", "study-note", "book-intro", "map", "image"],
        "description": "Restrict to one content shape."
      },
      "book": {
        "type": "string",
        "description": "USFM book code (e.g. 'TIT', 'RUT')."
      },
      "source": {
        "type": "string",
        "enum": ["all", "door43", "aquifer"],
        "default": "all"
      },
      "top_k": { "type": "integer", "default": 10, "minimum": 1, "maximum": 50 },
      "use_semantic": {
        "type": "boolean",
        "default": false,
        "description": "Opt-in: enable vector ANN ranking alongside FTS5/structured. Adds ~150ms per call and requires OPENAI_API_KEY on the server."
      }
    },
    "required": ["query"]
  }
}
```

Result content (text item is JSON-formatted for clients without structured-content support):

```json
{
  "query": "...",
  "analysis": { "intent": "...", "passages": [], "tags": [] },
  "hits": [
    {
      "chunk_id": "abc:0000",
      "score": 0.85,
      "title": "ULT — Titus 1:1",
      "passage": "Titus 1:1",
      "kind": "scripture",
      "primary_path": "/en/scripture/nt/TIT/1/1",
      "excerpt": "Paul, a servant of God…"
    }
  ]
}
```

### `get_chunk`

Resolve a chunk_id to full body, all tree paths, and cross-references.

```json
{
  "name": "get_chunk",
  "description": "Fetch the full body of a specific chunk by chunk_id. Returns body text, tree paths the chunk lives in, and cross-references (other chunks at the same passage, referenced TA modules, linked TW articles). Use this to read the full source of a citation.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "chunk_id": { "type": "string", "description": "Stable identifier returned by search/passage_lookup/etc." },
      "lang": { "type": "string", "default": "en" }
    },
    "required": ["chunk_id"]
  }
}
```

Result:

```json
{
  "chunk_id": "abc:0000",
  "doc_id": "abc",
  "title": "ULT — Titus 1:1",
  "body": "Paul, a servant of God, and an apostle of Jesus Christ...",
  "passage": "Titus 1:1",
  "tags": ["kind:scripture", "book:TIT", ...],
  "kind": "scripture",
  "primary_path": "/en/scripture/nt/TIT/1/1",
  "all_paths": [...],
  "cross_refs": {
    "passage": [...],
    "support_ref": [...],
    "term": [...]
  }
}
```

### `passage_lookup`

```json
{
  "name": "passage_lookup",
  "description": "Get every chunk overlapping a Bible passage range. Returns chunks from all sources (ULT, UST, TN, TQ, TW articles linked to this passage, Aquifer study notes, etc.). Use this to read everything the corpus has about a specific passage.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "reference": {
        "type": "string",
        "description": "Bible reference, e.g. 'Titus 1:1', 'Romans 3:24-25', 'Ruth chapter 1'. Whole-book and whole-chapter ranges are supported."
      },
      "lang": { "type": "string", "default": "en" }
    },
    "required": ["reference"]
  }
}
```

Result is the same shape as `search.hits`, ordered by passage start.

### `entity_lookup`

Cross-source entity browser: merges Door43 TW articles and Aquifer ACAI
entity tags under one query.

```json
{
  "name": "entity_lookup",
  "description": "Find chunks about a person, place, or biblical concept. Merges results from Door43 Translation Words (TW articles) and Aquifer ACAI entity tags — both sources are searched together. Returns the canonical TW article (when one exists) plus all chunks tagged with this entity in either taxonomy.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "entity": {
        "type": "string",
        "description": "Entity name. Lower-case and hyphenated for compound terms. Examples: 'Boaz', 'justification', 'Paul', 'Jerusalem'."
      },
      "type": {
        "type": "string",
        "enum": ["any", "person", "place", "keyterm", "deity", "event"],
        "default": "any"
      },
      "lang": { "type": "string", "default": "en" }
    },
    "required": ["entity"]
  }
}
```

### `tree_listing`

Generic tree walker. Pass a tree name and optional path; receive children
(intermediate nodes) or chunks (leaves).

```json
{
  "name": "tree_listing",
  "description": "Walk one of the eight perspective trees over the corpus. Returns the children of the requested node (for branching nodes) or chunks at this leaf (for terminal nodes). Use this to navigate the corpus structurally — by Bible book/chapter/verse, by source provider, by content kind, by entity, etc.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "tree": {
        "type": "string",
        "enum": ["scripture", "source", "kind", "term", "methodology", "pericope", "aquifer"],
        "description": "Which perspective. 'scripture' = canonical Bible (testament/book/chapter/verse); 'source' = provider/resource; 'kind' = content shape; 'term' = entity; 'methodology' = TA modules."
      },
      "path": {
        "type": "array",
        "items": { "type": "string" },
        "description": "Path segments to drill into. Empty for root. Examples: ['nt'], ['nt','TIT'], ['nt','TIT','1','6']."
      },
      "lang": { "type": "string", "default": "en" }
    },
    "required": ["tree"]
  }
}
```

### `ask` *(optional)*

Off by default. Set `BTMCP_EXPOSE_ASK=1` on the server to enable.

```json
{
  "name": "ask",
  "description": "Full retrieval-augmented generation: question → cited answer. Internally runs search + LLM synthesis. Note: if you (the agent) are already an LLM, prefer `search` + `get_chunk` so you can synthesize from raw sources yourself. Use `ask` only when you want a quick second-opinion synthesis or are reusing the bt-docker-mcp answer verbatim.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "question": { "type": "string" },
      "lang": { "type": "string", "default": "en" },
      "source": {
        "type": "string",
        "enum": ["all", "door43", "aquifer"],
        "default": "all"
      }
    },
    "required": ["question"]
  }
}
```

## Auth

MCP requests honor the same auth as the REST API:

- **Public read tools** (`search`, `get_chunk`, `passage_lookup`,
  `entity_lookup`, `tree_listing`): no token required by default.
  Per-IP rate limiting applies.
- **`ask`**: when enabled, gated behind `Authorization: Bearer <token>`.
  Token configured via `BTMCP_BEARER_TOKEN` env var.

Anonymous probing of `tools/list` is allowed — clients need to discover
what's available before they can choose to authenticate.

## Client setup examples

### Claude desktop (`claude_desktop_config.json`)

For **stdio** mode, run the server as a local subprocess:

```json
{
  "mcpServers": {
    "bt-docker-mcp": {
      "command": "/path/to/bt-docker-mcp/.venv/bin/python",
      "args": ["-m", "server.mcp.stdio"],
      "env": {
        "INDEX_DB_PATH": "/path/to/bt-docker-mcp/indexer/index.db",
        "OPENAI_API_KEY": "...",
        "GROQ_API_KEY": "..."
      }
    }
  }
}
```

For **HTTP** mode (remote deployment):

```json
{
  "mcpServers": {
    "bt-docker-mcp": {
      "url": "https://api.yourapp.dev/mcp"
    }
  }
}
```

### Cursor

Same shape — configure under `Cursor Settings → Features → MCP`.

### Continue (VS Code)

Add to `.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "bt-docker-mcp",
      "transport": "http",
      "url": "https://api.yourapp.dev/mcp"
    }
  ]
}
```

## How an LLM agent uses these tools

Typical flow inside Claude desktop or any MCP-aware agent:

1. User asks the agent a Bible-translation question.
2. Agent's LLM decides to use `search` with the question text.
3. Server returns ranked chunks with `chunk_id`s.
4. Agent inspects the previews; if it needs full bodies for the most
   relevant chunks, calls `get_chunk` per chunk_id.
5. Agent's LLM synthesizes an answer from the full bodies, citing
   chunk_ids inline.
6. User sees the agent's answer with citations that map to bt-docker-mcp's
   tree paths and permalinks.

The deterministic-provenance property is preserved: every citation the
agent emits points at content that *deterministically reached the agent
via our retrieval logic*. The agent's own LLM is the synthesis layer
(replacing our `synthesize.py` for this consumption mode), but the
chunks it sees are not LLM-chosen — they came from `search` /
`passage_lookup` / `tree_listing` / `entity_lookup` deterministic paths.

## Adding a new MCP tool

1. Define the tool schema and handler in `server/mcp/tools.py`:

   ```python
   @register_tool(
       name="my_new_tool",
       description="...",
       input_schema={
           "type": "object",
           "properties": {
               "arg1": {"type": "string", "description": "..."}
           },
           "required": ["arg1"],
       }
   )
   def my_new_tool(arguments: dict, db) -> dict:
       arg1 = arguments["arg1"]
       # ... do the work, return structured result ...
       return {"result": ...}
   ```

2. The tool automatically appears in `tools/list` responses and is
   callable via `tools/call`. No other wiring needed.

3. Add a smoke test:

   ```python
   def test_my_new_tool(client):
       resp = client.post("/mcp", json={
           "jsonrpc": "2.0",
           "id": 1,
           "method": "tools/call",
           "params": {"name": "my_new_tool", "arguments": {"arg1": "..."}}
       })
       assert resp.status_code == 200
       assert "result" in resp.json()
   ```

## See also

- [`server.md`](server.md) — backend deployment, REST surface, FastAPI implementation
- [`client-integration.md`](client-integration.md) — REST consumer perspective
- [`architecture.md`](architecture.md) — overall system architecture
- [translation-helps-mcp](https://github.com/unfoldingWord/translation-helps-mcp) — reference Door43-MCP
- [aquifer-mcp](https://github.com/klappy/aquifer-mcp) — reference Aquifer-MCP
