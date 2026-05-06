# Client integration guide

How to build a static frontend (React / Astro / SvelteKit, hosted on Netlify
or similar) that talks to the bt-docker-mcp backend (FastAPI on fly.io or Railway).
Self-contained — you don't need to read the rest of the docs to use this.

## What this system gives you

A REST API over a corpus of Bible-translation resources (Door43
unfoldingWord catalog + BibleAquifer content, English to start). The API
serves three things:

1. **Hierarchical browse** — eight independent tree views over the same
   underlying chunks. Every chunk is reachable from at least one tree;
   most are reachable from several.
2. **Search** — keyword + semantic + structured filters, returns ranked
   chunks with citation cards.
3. **Ask** — full RAG: free-form question → cited answer with provenance
   chain back to specific chunks.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Netlify (static)            yourapp.netlify.app            │
│  React / Astro / SvelteKit                                  │
│  Tree-view components, citation rendering, search/ask UI    │
└──────────────────────┬──────────────────────────────────────┘
                       │  HTTPS, JSON
                       ↓
┌─────────────────────────────────────────────────────────────┐
│  fly.io / Railway            api.yourapp.dev                │
│  FastAPI + SQLite + sqlite-vec                              │
│  /api/tree, /api/leaf, /api/chunk, /api/search, /api/ask    │
└─────────────────────────────────────────────────────────────┘
```

The frontend is **fully static** (or SSG) — Netlify hosts only HTML, CSS,
JS, and images. Every dynamic piece lives behind the API. This split is
deliberate: the SQLite + vector index doesn't fit Netlify Functions
(50 MB bundle limit, ephemeral filesystem); fly.io / Railway support
mounted volumes and persistent SQLite naturally.

## Core concepts

### Chunks and citations

The atomic unit is a **chunk** — a piece of source content (one verse,
one translation note, one term article, one study-note paragraph, etc.)
addressable by a stable `chunk_id` like `ef303bb6670e192e:0000`. Every
chunk has:

- **`chunk_id`** — opaque, stable across rebuilds. Use as a permalink.
- **`title`** — human-readable, e.g. "ULT — Titus 1:1".
- **`body`** — full source text.
- **`passage`** — human-readable Bible reference if one applies, e.g. "Titus 1:1".
- **`tags`** — flat strings, namespace-prefixed (see [Tag namespaces](#tag-namespaces)).
- **`source`** — relative path to the source file (informational only — don't use in URLs).

When the backend synthesizes an answer, it cites chunk_ids inline like
`[ef303bb6670e192e:0000]`. The frontend renders these as numbered footnotes
and resolves each to a tree-friendly URL plus a hover-card preview.

### Tag namespaces

Every chunk is tagged. Most tags fit one of these namespaces:

| Prefix | Meaning | Example |
|---|---|---|
| `kind:<x>` | content shape (source-agnostic) | `kind:scripture` |
| `book:<CODE>` | USFM book code | `book:TIT` |
| `lang:<x>` | language | `lang:en` |
| `resource:<x>` | resource type identifier | `resource:ult` |
| `org:<x>` | source organization | `org:unfoldingWord` |
| `term:<id>` | TW term identifier | `term:justify` |
| `category:<x>` | TW article category | `category:kt` |
| `section:<x>` / `module:<x>` | TA module location | `section:translate`, `module:figs-metaphor` |
| `aquifer:<RepoName>` | Aquifer repo of origin | `aquifer:AquiferOpenStudyNotes` |
| `acai:<type>:<id>` | ACAI entity association (Aquifer) | `acai:person:Paul`, `acai:keyterm:Faith` |
| `support_ref:<rc-link>` | TA module reference (TN rows) | `support_ref:rc://*/ta/man/translate/figs-abstractnouns` |

Use these to drive faceted-search UI and to construct tree paths.

### Source-agnostic `kind:*` taxonomy

Nine values, important to know:

| Kind | Meaning |
|---|---|
| `scripture` | Bible text (ULT, UST, BSB, WEB, …) |
| `translator-note` | TN, SIL Translator's Notes |
| `question` | TQ comprehension questions |
| `term` | TW term articles, ACAI dictionary entries |
| `methodology` | TA modules, FIA Translation Guide |
| `study-note` | Aquifer / Biblica study notes |
| `book-intro` | book introductions |
| `map` / `image` | non-text assets (Aquifer FIA Maps / Images) |

## The eight perspective trees

Same chunks, eight ways to walk them. Pick which trees your UI surfaces;
all eight are valid. Most apps will start with **Source**, **Scripture**,
and **Term** — those three cover ~95% of navigation.

### 1. Scripture tree (canonical Bible)

Dominant tree. Anything with a passage reference lives here.

```
Old Testament                   ← /scripture/ot
  Genesis                       ← /scripture/ot/GEN
    Chapter 1                   ← /scripture/ot/GEN/1
      Verse 1                   ← /scripture/ot/GEN/1/1   ← leaf
      Verse 2
      …
  Exodus
  …
New Testament                   ← /scripture/nt
  Matthew
  …
  Titus
    Chapter 1
      Verse 1                   ← /scripture/nt/TIT/1/1
      …
```

Multi-passage chunks (e.g., a TN covering Ruth 1:14–22) appear at every
overlapping verse leaf. Don't dedupe at the route level — that's the
correct behavior.

### 2. Source tree (provenance)

```
Door43                          ← /source/door43
  ULT                           ← /source/door43/ult
    Titus                       ← /source/door43/ult/TIT
      1:1, 1:2, …
  UST
  TN
  TQ
  TW
    kt                          ← /source/door43/tw/kt
      faith
      grace
      justify
    names
    other
  TA
    translate                   ← /source/door43/ta/translate
      figs-metaphor
      …
Aquifer                         ← /source/aquifer
  AquiferOpenStudyNotes         ← /source/aquifer/AquiferOpenStudyNotes
  BiblicaStudyNotes
  FIATranslationGuide
  SILOpenTranslatorsNotes
  …
```

Universal — every chunk fits somewhere here.

### 3. Kind tree (content shape)

```
scripture
translator-note
question
term
methodology
study-note
book-intro
map
image
```

Each `kind:*` value is a top-level branch; sub-organization mirrors the
Source tree underneath.

### 4. Term tree (Door43 TW + Aquifer ACAI, unified)

Cross-source entity browser.

```
keyterm                         ← /term/keyterm
  Faith
  Grace
  Justification
person
  Paul
  Boaz
  Naomi
place
  Bethlehem
  Crete
…
```

**Important**: a click on "Paul" should return chunks tagged with **either**
`term:paul` (Door43 TW) **OR** `acai:person:Paul` (Aquifer). Backend
handles the union; frontend just navigates.

### 5. Methodology tree (TA + FIA Translation Guide)

```
translate                       ← /methodology/translate
  figs-metaphor
  figs-abstractnouns
  figs-activepassive
  …
checking
process
intro
```

Each leaf shows the TA module body **plus** back-references — TN/TQ chunks
that cite it via `support_ref:`. That cross-reference is the highest-value
relationship in this tree (translators reading TA wonder *"where is this
principle used?"*).

### 6. Pericope view (passage range pivot)

```
/pericope/<start>-<end>
e.g. /pericope/56001005-56001009  →  all chunks at Titus 1:5–9
```

Less hierarchical, more "neighborhood view". Same chunks as the Scripture
tree at any given verse, but grouped by source/kind instead of by verse.

### 7. Aquifer collection view

Surface each Aquifer repo as its own browse:

```
/aquifer/AquiferOpenStudyNotes
/aquifer/BiblicaStudyNotes
/aquifer/FIATranslationGuide
…
```

Useful when an editorial voice matters (e.g., "show me what Biblica says
about Titus 1").

### 8. Language tree (extensible)

```
/en/...
/es/...   ← future
/hi/...   ← future
```

Currently only `en` is populated. **Bake `<lang>` as the outermost path
segment from day one** so adding languages doesn't break URLs.

## URL scheme for your frontend app

```
/                                                home — tree picker
/c/<chunk_id>                                    canonical chunk permalink

/<lang>/scripture/<testament>/<book>[/<chapter>[/<verse>]]
/<lang>/source/<provider>[/<resource>[/<sub>[/<doc>]]]
/<lang>/kind/<kind>[/<sub>[/<id>]]
/<lang>/term/<type>[/<entity>]
/<lang>/methodology/<section>[/<module>]
/<lang>/pericope/<start>-<end>
/<lang>/aquifer/<repo>[/<id>]

/q?q=…&lang=en                                   search results page
/ask?q=…&lang=en                                 RAG (full LLM answer) page
```

`/c/<chunk_id>` is the **always-resolvable permalink** — citation links
in answers point here. Tree paths are the *navigation* layer; chunk_ids
are the *citation* layer. If trees restructure, citations still resolve.

## API contract

Base URL: `https://api.yourapp.dev` (your fly.io or Railway deployment).

All responses JSON unless noted. Errors are `{"error": "...", "code": "..."}` with appropriate HTTP status.

### `GET /api/health`

Sanity check.

```json
{ "status": "ok", "schema_version": "1", "indexed_at": 1717689600 }
```

### `GET /api/tree/<tree_name>?lang=en`

Top-level tree listing for one of the eight trees.

```http
GET /api/tree/scripture?lang=en

200 OK
{
  "tree": "scripture",
  "lang": "en",
  "nodes": [
    {
      "id": "ot",
      "label": "Old Testament",
      "child_count": 39,
      "url": "/en/scripture/ot"
    },
    {
      "id": "nt",
      "label": "New Testament",
      "child_count": 27,
      "url": "/en/scripture/nt"
    }
  ]
}
```

### `GET /api/tree/<tree_name>/<path...>?lang=en`

Drill down one level. The `path` is the same as the URL path you'd use
in your frontend (without the `<lang>` prefix).

```http
GET /api/tree/scripture/nt/TIT?lang=en

200 OK
{
  "tree": "scripture",
  "lang": "en",
  "node": {
    "id": "TIT",
    "label": "Titus",
    "testament": "nt"
  },
  "children": [
    { "id": "1", "label": "Chapter 1", "child_count": 16, "url": "/en/scripture/nt/TIT/1" },
    { "id": "2", "label": "Chapter 2", "child_count": 15, "url": "/en/scripture/nt/TIT/2" },
    { "id": "3", "label": "Chapter 3", "child_count": 15, "url": "/en/scripture/nt/TIT/3" }
  ]
}
```

```http
GET /api/tree/scripture/nt/TIT/1/6?lang=en

200 OK
{
  "tree": "scripture",
  "lang": "en",
  "node": {
    "passage": "Titus 1:6",
    "bbcccvvv": 56001006
  },
  "chunks": [
    {
      "chunk_id": "f8a3...:0000",
      "title": "ULT — Titus 1:6",
      "kind": "scripture",
      "passage": "Titus 1:6",
      "tags": ["kind:scripture", "book:TIT", "resource:ult", "lang:en"],
      "excerpt": "if anyone is blameless, a husband of one wife, having faithful children not accused of reckless behavior or rebellion.",
      "primary_path": "/en/scripture/nt/TIT/1/6",
      "permalink": "/c/f8a3...:0000"
    },
    {
      "chunk_id": "...",
      "title": "UST — Titus 1:6",
      ...
    },
    {
      "chunk_id": "...",
      "title": "TN — Titus 1:6 (jen8)",
      ...
    }
  ]
}
```

Leaf nodes return chunks; intermediate nodes return children. The shape
distinguishes via the presence of `children` vs `chunks`.

### `GET /api/chunk/<chunk_id>`

Full chunk body + cross-references + every tree path the chunk lives in.

```http
GET /api/chunk/f8a3...:0000

200 OK
{
  "chunk_id": "f8a3...:0000",
  "doc_id": "f8a3...",
  "title": "ULT — Titus 1:6",
  "body": "if anyone is blameless, a husband of one wife, having faithful children not accused of reckless behavior or rebellion.",
  "passage": "Titus 1:6",
  "passage_refs": [[56001006, 56001006]],
  "tags": ["kind:scripture", "book:TIT", "resource:ult", "lang:en", "org:unfoldingWord"],
  "kind": "scripture",
  "primary_path": "/en/scripture/nt/TIT/1/6",
  "all_paths": [
    "/en/scripture/nt/TIT/1/6",
    "/en/source/door43/ult/TIT/1/6",
    "/en/kind/scripture/TIT/1/6",
    "/en/pericope/56001006-56001006"
  ],
  "permalink": "/c/f8a3...:0000",
  "cross_refs": {
    "passage": [
      { "chunk_id": "...", "title": "UST — Titus 1:6", "primary_path": "..." },
      { "chunk_id": "...", "title": "TN — Titus 1:6 (jen8)", "primary_path": "..." },
      ...
    ],
    "support_ref": [
      { "chunk_id": "...", "title": "TA — Abstract Nouns", "primary_path": "/en/methodology/translate/figs-abstractnouns" }
    ],
    "term": [
      { "chunk_id": "...", "title": "TW — Blameless (kt)", "primary_path": "/en/term/keyterm/Blameless" }
    ]
  }
}
```

This is what powers the citation hover-card and the "see also" panel on
chunk pages.

### `GET /api/search?q=…&lang=en[&filters…]`

Same retrieval as the CLI's `query.ask --no-llm` — no LLM call, just
ranked hits.

```http
GET /api/search?q=qualifications+for+church+leaders&lang=en&kind=scripture&book=TIT

200 OK
{
  "query": "qualifications for church leaders",
  "lang": "en",
  "filters": { "kind": "scripture", "book": "TIT" },
  "analysis": {
    "fts_query": "qualifications OR church OR leaders",
    "passages": [],
    "tags": [],
    "intent": "thematic"
  },
  "hits": [
    {
      "chunk_id": "...",
      "score": 0.85,
      "retrievers": ["fts", "title", "vec"],
      "title": "ULT — Titus 1:7",
      "passage": "Titus 1:7",
      "kind": "scripture",
      "excerpt": "For the overseer must be blameless, as a household manager...",
      "primary_path": "/en/scripture/nt/TIT/1/7",
      "permalink": "/c/...:0000"
    },
    ...
  ],
  "total": 10
}
```

Optional query params:
- `kind=<x>` — restrict to one content kind.
- `book=<CODE>` — restrict to one book.
- `source=<door43|aquifer|all>` — restrict by provenance (default `all`).
- `top_k=<N>` — number of results (default 10, max 50).
- `no_vec=true` — skip vector retrieval (faster, cheaper, lower recall).

### `POST /api/ask`

Full RAG. Free-form question → cited answer.

```http
POST /api/ask
Content-Type: application/json

{
  "question": "What does Titus 1:1 say about being a servant of God?",
  "lang": "en",
  "scope": { "source": "all", "book": null }
}

200 OK
{
  "question": "What does Titus 1:1 say about being a servant of God?",
  "answer": "Paul identifies himself as a servant of God [1] and an apostle of Jesus Christ [1]. The translation note explains that the term \"servant\" connects to the Old Testament prophets [2].",
  "citations": [
    {
      "n": 1,
      "chunk_id": "...",
      "title": "ULT — Titus 1:1",
      "passage": "Titus 1:1",
      "kind": "scripture",
      "excerpt": "Paul, a servant of God, and an apostle of Jesus Christ...",
      "primary_path": "/en/scripture/nt/TIT/1/1",
      "permalink": "/c/...:0000"
    },
    {
      "n": 2,
      "chunk_id": "...",
      "title": "TN — Titus 1:1 (xrtm)",
      ...
    }
  ],
  "confidence": "high",
  "analysis": { "intent": "passage_specific", "passages": [[56001001, 56001001]] }
}
```

When the system can't answer, the response shape is the same but:

```jsonc
{
  "answer": "I don't see an answer to that in the indexed sources.",
  "citations": [...closest related, in case useful...],
  "confidence": "low",
  ...
}
```

Detect this client-side via `confidence: "low"` plus the canonical phrase.

### `POST /api/ask` with streaming (Server-Sent Events)

For long answers, stream progressively. Send `Accept: text/event-stream`
to opt in.

```http
POST /api/ask
Accept: text/event-stream
Content-Type: application/json

{ "question": "...", "lang": "en" }

200 OK
Content-Type: text/event-stream

event: status
data: {"phase": "analyzing"}

event: status
data: {"phase": "retrieving", "intent": "passage_specific"}

event: hits
data: {"count": 8, "preview": [{"chunk_id": "...", "title": "...", "primary_path": "..."}, ...]}

event: status
data: {"phase": "synthesizing"}

event: token
data: {"text": "Paul"}

event: token
data: {"text": " identifies"}

...

event: complete
data: {"answer": "...", "citations": [...], "confidence": "high"}
```

Use the `EventSource` API or `fetch` with a manual stream parser. The
`hits` event lets you show retrieval results before the LLM finishes,
which is good UX.

## TypeScript types

Types you'll want to define:

```ts
// Common types
export type ChunkId = string;          // "ef303bb6670e192e:0000"
export type DocId = string;            // "ef303bb6670e192e"
export type Lang = "en";               // extend as more languages added
export type Kind =
  | "scripture" | "translator-note" | "question" | "term"
  | "methodology" | "study-note" | "book-intro" | "map" | "image";

export interface ChunkPreview {
  chunk_id: ChunkId;
  title: string;
  kind: Kind;
  passage: string | null;
  tags: string[];
  excerpt: string;             // up to ~240 chars, for display
  primary_path: string;
  permalink: string;           // "/c/<chunk_id>"
}

export interface Chunk extends ChunkPreview {
  doc_id: DocId;
  body: string;                // full text
  passage_refs: [number, number][];
  all_paths: string[];
  cross_refs: {
    passage: ChunkPreview[];
    support_ref: ChunkPreview[];
    term: ChunkPreview[];
  };
}

// Tree responses
export interface TreeNode {
  id: string;
  label: string;
  child_count?: number;
  url: string;
}

export interface TreeBranch {
  tree: string;
  lang: Lang;
  node: { id?: string; label?: string; passage?: string; bbcccvvv?: number; testament?: string; };
  children?: TreeNode[];   // when at non-leaf
  chunks?: ChunkPreview[]; // when at leaf
}

// Search
export interface QueryAnalysis {
  fts_query: string;
  passages: [number, number][];
  tags: string[];
  intent: "thematic" | "entity_lookup" | "passage_specific" | "passage_book" | "methodology";
}

export interface SearchHit extends ChunkPreview {
  score: number;
  retrievers: string[];
}

export interface SearchResponse {
  query: string;
  lang: Lang;
  filters: Record<string, string>;
  analysis: QueryAnalysis;
  hits: SearchHit[];
  total: number;
}

// Ask
export interface Citation extends ChunkPreview {
  n: number;
}

export interface AskResponse {
  question: string;
  answer: string;
  citations: Citation[];
  confidence: "low" | "medium" | "high";
  analysis: QueryAnalysis;
}
```

## Citation rendering

Server returns answer text with inline `[chunk_id]` markers. Render
client-side as numbered footnotes:

```ts
function renderAnswer(response: AskResponse): RenderedAnswer {
  let text = response.answer;
  const idToN = new Map(response.citations.map(c => [c.chunk_id, c.n]));
  text = text.replace(/\[([A-Za-z0-9:_-]+)\]/g, (_, id) => {
    const n = idToN.get(id);
    return n ? `[${n}]` : "";   // drop hallucinated; backend should already do this
  });
  return { text, citations: response.citations };
}
```

Each `[N]` becomes a hover-card or anchor link to the citation's
`primary_path`. Permalink fallback: `/c/<chunk_id>`.

## Search vs. Ask — when to use which

| User action | UX | API |
|---|---|---|
| typing in a search box, browsing results | live results, click to drill | `/api/search` |
| asking a natural-language question, expecting a synthesized answer | progressive (streaming), citations as footnotes | `/api/ask` (preferably with SSE) |

Both produce citations, but search returns *only* chunks (let the user
read the source themselves), while ask returns a *generated answer* with
citations attached. For a translation-helps tool, **default to search**;
ask is more LLM-cost-intensive and slower (~2–6s vs <500ms).

## Pagination

Tree leaves and search results return up to ~50 items by default. For
larger sets, use `?offset=N&limit=N`:

```http
GET /api/tree/source/aquifer/AquiferOpenStudyNotes?lang=en&offset=50&limit=50
```

## Auth and CORS

The backend runs on a different origin from the frontend, so CORS matters.

- **Allowed origins** are configured server-side (whitelist your Netlify
  preview + production domains, plus localhost for dev).
- **Auth**: read endpoints (`GET /api/tree`, `/api/chunk`, `/api/search`)
  are public. `POST /api/ask` may be rate-limited and/or behind a token
  depending on deployment. Pass via `Authorization: Bearer <token>` if
  required.
- **No cookies needed** for typical browse + ask flows. Use `mode: "cors"`
  on `fetch` calls.

Local development: set `VITE_API_BASE_URL=http://localhost:8000` in your
`.env.local`; production: `VITE_API_BASE_URL=https://api.yourapp.dev`.

## Environment configuration

Frontend env vars (use the prefix your framework expects — `VITE_`,
`PUBLIC_`, `NEXT_PUBLIC_`, etc.):

| Var | Purpose | Default |
|---|---|---|
| `*_API_BASE_URL` | backend origin | `https://api.yourapp.dev` |
| `*_DEFAULT_LANG` | starting language | `en` |
| `*_ENABLE_ASK` | feature flag for `/ask` route (cost gate) | `true` |
| `*_API_TOKEN` | optional bearer token for rate-limited endpoints | unset |

## Six gotchas worth knowing

1. **Citation chunk_ids are opaque, but stable.** They're hashed from
   source paths. Don't try to parse them; treat them as opaque permalink
   tokens. The backend's `/api/chunk/<chunk_id>` is the only thing that
   resolves them.

2. **Multi-passage chunks appear in many tree leaves.** A TN covering
   Ruth 1:14–22 will show up at all 9 verse leaves in the Scripture
   tree. This is correct — don't try to dedupe at the route level.
   But within a single page render, dedupe by chunk_id when the same
   chunk would otherwise appear twice.

3. **Some chunks have no passage.** Most TA modules and a few Aquifer
   book-intros aren't book-anchored. They don't appear in the Scripture
   tree at all. Surface them via Source / Kind / Methodology trees.

4. **ACAI vs Door43 entity-name collision.** Aquifer uses `acai:person:Paul`,
   Door43 uses `term:paul`. Same person, different namespaces. The Term
   tree in this API merges them — your frontend doesn't need to handle
   it. But if you build a custom entity view, query both.

5. **The `book:RUT` tag means "this chunk is about Ruth"**, NOT
   necessarily "this chunk's passage is in Ruth". TW articles and TA
   modules inherit `book:*` tags from inherited passages — a TW article
   linked from both Titus and Ruth carries both `book:TIT` and `book:RUT`.

6. **Refusal is structured, not exceptional.** When the backend can't
   answer (`/api/ask` with insufficient sources), the response has the
   same shape as a successful one — just with a low-confidence answer
   that begins with the canonical phrase. Don't treat refusal as an
   error condition; render it as a normal-but-confidence-low result.

## Suggested v0 frontend scope

To get something useful in the smallest number of routes:

```
/                       home: tree picker, search box
/<lang>/source/...      Source tree — universal, simple
/<lang>/scripture/...   Scripture tree — high-value
/<lang>/term/...        Term tree — entity browse
/<lang>/c/<chunk_id>    chunk view (cross-refs)
/q                      search results
/ask                    RAG (gated by feature flag)
```

Three trees + chunk view + search + ask. ~7 routes; covers ~95% of the
navigation use case. Other trees (Kind, Methodology, Pericope, Aquifer
collection, Language) slot in without re-routing once the URL scheme
is solid.

## See also

- [`server.md`](server.md) — backend implementation plan (for understanding what the API is built on, if the backend doesn't exist yet)
- [`architecture.md`](architecture.md) — full system architecture
- [`query-pipeline.md`](query-pipeline.md) — how `/api/search` and `/api/ask` work internally
