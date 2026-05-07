# Client integration guide

How to build a static frontend (React / Astro / SvelteKit, hosted on Netlify
or similar) that talks to the bt-docker-mcp backend (FastAPI on Railway).
Self-contained — you don't need to read the rest of the docs to use this.

## What this system gives you

A REST API over a corpus of Bible-translation and Bible-study resources
(Door43 unfoldingWord catalog + BibleAquifer + Berean Standard Bible +
classical lexicons + STEPBible morphology + Theographic + TIPNR +
OpenBible Geocoding + Nave's Topical + BibleProject transcripts —
English, full Bible coverage). The API serves four things:

1. **Hierarchical browse** — twelve independent tree views over the
   same underlying chunk store. Every chunk is reachable from at least
   one tree; most are reachable from several. Trees are ranked by
   priority below; you don't need all twelve for v0.
2. **Cross-reference + topic + entity lookups** — verse → cross-refs
   (TSK + parallel passages), verse → topics (Nave's), entity → graph
   (people, places, events with relations), word → English concordance.
3. **Search** — keyword + semantic + structured filters, returns ranked
   chunks with citation cards.
4. **Ask** — full RAG: free-form question → cited answer with provenance
   chain back to specific chunks.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Netlify (static)            yourapp.netlify.app            │
│  React / Astro / SvelteKit                                  │
│  Tree-view, citation rendering, search/ask UI               │
└──────────────────────┬──────────────────────────────────────┘
                       │  HTTPS, JSON
                       ↓
┌─────────────────────────────────────────────────────────────┐
│  Railway                     api.yourapp.dev                │
│  FastAPI + SQLite + sqlite-vec                              │
│  /api/tree, /api/chunk, /api/search, /api/ask,              │
│  /api/topic, /api/entity, /api/cross-references,            │
│  /api/concordance                                           │
└─────────────────────────────────────────────────────────────┘
```

The frontend is **fully static** (or SSG) — Netlify hosts only HTML, CSS,
JS, and images. Every dynamic piece lives behind the API. The SQLite +
vector index doesn't fit Netlify Functions (50 MB bundle limit, ephemeral
filesystem); Railway supports persistent volumes naturally.

## Core concepts

### Chunks and citations

The atomic unit is a **chunk** — a piece of source content (one verse,
one translation note, one term article, one study-note paragraph, one
lexicon entry, one verse-morphology row, one BibleProject transcript
window) addressable by a stable `chunk_id` like `ef303bb6670e192e:0000`.
Every chunk has:

- **`chunk_id`** — opaque, stable across rebuilds. Use as a permalink.
- **`title`** — human-readable, e.g. "ULT — Titus 1:1".
- **`body`** — full source text.
- **`passage`** — human-readable Bible reference if one applies.
- **`tags`** — flat strings, namespace-prefixed (see below).
- **`source`** — relative path to the source file (informational; don't use in URLs).

When the backend synthesizes an answer, it cites chunk_ids inline like
`[ef303bb6670e192e:0000]`. The frontend renders these as numbered
footnotes and resolves each to a tree-friendly URL plus a hover-card
preview.

### Provenance ids (post-expansion)

Citations are now generalized beyond chunk_ids — the backend cites
`provenance_id` strings with these shapes:

| Provenance prefix | Resolves via |
|---|---|
| `<chunk_id>` (no prefix) | `/api/chunk/<chunk_id>` |
| `entity:<id>` | `/api/entity/<id>` |
| `topic:<id>` | `/api/topic/<id>` |
| `lexicon:<strongs>` | usually maps to a `kind:lexicon` chunk; see `/api/chunk/...` after lookup |
| `relation:<source_id>:<rel>:<target_id>` | structured graph fact; `/api/entity/<source_id>/relations` |

For v0 you only need to handle the first form (chunks). The others come
into play when stage-3 retrievers start surfacing entity / topic /
lexicon citations.

### Tag namespaces

Every chunk is tagged. Most tags fit one of these namespaces:

| Prefix | Meaning | Example |
|---|---|---|
| `kind:<x>` | content shape (source-agnostic) | `kind:scripture`, `kind:bible`, `kind:lexicon` |
| `book:<USFM>` | USFM book code | `book:TIT` |
| `lang:<x>` | language of the body | `lang:en`, `lang:grc`, `lang:hbo` |
| `resource:<x>` | resource identifier | `resource:ult`, `resource:bsb`, `resource:bibleproject` |
| `org:<x>` | source organization | `org:unfoldingWord`, `org:berean`, `org:bibleproject` |
| `source:<x>` | lexicon / dictionary source identifier | `source:lsj`, `source:bdb`, `source:abbott-smith` |
| `strongs:<X####>` | Strong's number (zero-padded) | `strongs:G0026`, `strongs:H0001` |
| `lemma:<translit>` | lowercased ASCII transliteration of a lemma | `lemma:agape`, `lemma:phileo` |
| `morph:<code>` | morphology parse code | `morph:N-NSF`, `morph:Ncfsa` |
| `term:<id>` | Door43 TW term identifier | `term:justify` |
| `category:<x>` | TW article category | `category:kt`, `category:names` |
| `section:<x>` / `module:<x>` | TA module location | `section:translate`, `module:figs-metaphor` |
| `aquifer:<RepoName>` | Aquifer repo of origin | `aquifer:AquiferOpenStudyNotes` |
| `acai:<type>:<id>` | ACAI entity association | `acai:person:Paul` |
| `support_ref:<rc-link>` | TA module reference | `support_ref:rc://*/ta/man/translate/figs-abstractnouns` |
| `chunk_strategy:<x>` | multi-strategy chunking discriminator (BibleProject) | `chunk_strategy:timestamp` |
| `series:<slug>` | series / collection within a source | `series:deuterocanon-apocrypha` |
| `heading-level:<x>` | section-heading depth (BSB) | `heading-level:s1` |

Use these to drive faceted-search UI and to construct tree paths.

### Source-agnostic `kind:*` taxonomy

The `kind` value on every chunk tells you what shape of content it is.
Used to drive the Kind tree and to set per-content-type rendering
(verse cards vs. lexicon entries vs. transcripts look different).

| Kind | Meaning | Example resource |
|---|---|---|
| `scripture` | Translator-grade Bible text | ULT, UST |
| `bible` | Reader-grade Bible text | BSB |
| `translator-note` | Verse-level translation guidance | TN, SIL Translator's Notes |
| `question` | Comprehension questions for a passage | TQ |
| `term` | Term articles (theological / proper-noun definitions) | TW, ACAI |
| `methodology` | Translation-method articles | TA, FIA Translation Guide |
| `study-note` | Devotional / study commentary | Aquifer / Biblica study notes |
| `book-intro` | Book-level introductions | per-book intros |
| `lexicon` | Greek/Hebrew lexicon entry | LSJ, BDB, Abbott-Smith |
| `morphology` | Verse-level word-by-word parse | TAGNT (NT), TAHOT (OT) |
| `section-heading` | Pericope titles | BSB headings |
| `video-transcript` | Long-form transcript chunks | BibleProject |
| `dictionary` *(planned)* | Bible dictionary articles | Tyndale, ISBE |
| `ane-context` *(planned)* | Ancient near-east cultural context | studybible-mcp ANE entries |
| `passage-cluster` *(planned)* | Themed passage groupings | studybible-mcp passages |
| `map` / `image` | Non-text assets | Aquifer FIA Maps / Images |

Planned kinds are documented here for shape stability — your frontend
should already handle them defensively (treat unknown `kind` values as
"generic chunk" rather than throwing).

## Tree views

After the stage-2 corpus expansion the system has **twelve hierarchical
trees plus three lookup views**. Not all are equal in priority. Build
your v0 frontend around tier 1; tier 2 plugs in once the basics work;
tier 3 is for specialist users (translators, linguists).

### Tier 1 — must-have core trees

These cover ~95% of "I want to read X" navigation.

#### 1. Scripture tree (translator pair: Door43 ULT/UST + helps)

```
Old Testament                   ← /scripture/ot
  Genesis                       ← /scripture/ot/GEN
    Chapter 1                   ← /scripture/ot/GEN/1
      Verse 1                   ← /scripture/ot/GEN/1/1   ← leaf
      ...
New Testament                   ← /scripture/nt
  ...
  Titus
    Chapter 1
      Verse 1                   ← /scripture/nt/TIT/1/1
```

A leaf returns ULT + UST + TN + TQ + linked TW + linked TA chunks for
that verse — anything tagged `kind:scripture | translator-note |
question | term | methodology` whose passage range overlaps the verse.
This is the Door43 translator workflow tree.

#### 2. Bible tree (BSB — readable full Bible)

Same shape as scripture but returns only `kind:bible` chunks (BSB).

```
/bible/ot/GEN/1/1               ← BSB Genesis 1:1 verse
/bible/nt/JHN/3/16              ← BSB John 3:16
```

Use this for the casual reader UI ("show me John 3:16"). The Scripture
tree is for translators who want ULT + UST + notes side-by-side; the
Bible tree is for readers who want a single clean translation.

#### 3. Source tree (provenance)

Universal fallback — every chunk fits somewhere here.

```
Door43                          ← /source/door43
  ULT / UST / TN / TQ / TW / TA
Aquifer                         ← /source/aquifer
  AquiferOpenStudyNotes / BiblicaStudyNotes / FIATranslationGuide / ...
Berean                          ← /source/berean
  BSB / BSB-headings
STEPBible                       ← /source/stepbible
  TAGNT / TAHOT / LSJ / BDB / Abbott-Smith
BibleProject                    ← /source/bibleproject
  Insight-Videos / Redemption / Deuterocanon-Apocrypha / Root / Script-References
Theographic                     ← /source/theographic
OpenBible                       ← /source/openbible
CCEL                            ← /source/ccel
  Nave's
```

#### 4. Term tree (Door43 TW + Aquifer ACAI, unified)

Cross-source entity browser for theological terms + named entities.

```
keyterm
  Faith / Grace / Justification
person
  Paul / Boaz / Naomi
place
  Bethlehem / Crete
deity / event ...
```

Backend handles the union — clicks on "Paul" return chunks tagged with
*either* `term:paul` (Door43 TW) *or* `acai:person:Paul` (Aquifer).
**Note:** this is text-content-only; for the *graph* of relationships
on a person, use the Entity tree (#7).

### Tier 2 — high-leverage specialist trees

Add these after tier 1 ships.

#### 5. Topic tree (Nave's Topical Bible)

```
Topics A–Z                      ← /topic
  Aaron / Aaron's Rod / Abaddon / Abana / Abba / Abel / Abigail / ...
  ...
  Faith                         ← /topic/faith       ← leaf
    [list of ~140 verses tagged with this topic]
  Covenant                      ← /topic/covenant
  Creation                      ← /topic/creation
  ...
```

A topic leaf returns the verse list (BBCCCVVV pairs + decoded human
form) plus optional context. Click a verse → load that verse's chunks
via the Scripture or Bible tree. Useful for thematic study workflows.

#### 6. Pericope view (passage-range pivot)

Inherits the BSB section-heading anchors. Less hierarchical, more
"neighborhood view".

```
/pericope/<start>-<end>
e.g. /pericope/56001005-56001009  →  all chunks at Titus 1:5–9 + section heading
```

Same chunks as the Scripture tree at any given verse, but grouped by
source/kind instead of by verse, and anchored to a section heading
("The Qualifications of Elders" rather than just "1:5–9"). Good for
sermon/study prep UIs.

#### 7. Entity tree (people, places, events with graph relations)

Browse the Theographic + TIPNR + OpenBible-enriched entity graph.

```
/entity/person                  ← all biblical people
  Aaron / Abel / Abraham / ...
/entity/place                   ← all biblical places
  Bethlehem / Jerusalem / Galilee / ...
/entity/event                   ← biblical events
  Exodus / Conquest / Babylonian Exile / ...
/entity/<id>                    ← single entity detail
  e.g. /entity/person:david_994 → David's bio, family graph, verse list
```

Different from the Term tree: the Term tree returns *text articles* about
"David"; the Entity tree returns the *structured record* — birth/death,
relations (parent-of, spouse-of, …), enriched with TIPNR's Strong's
data and OpenBible's lat/long. Use for genealogy explorers, map
overlays, "ancestors of David" queries.

#### 8. Methodology tree (TA + FIA Translation Guide)

```
translate                       ← /methodology/translate
  figs-metaphor / figs-abstractnouns / figs-activepassive / ...
checking / process / intro
```

Each leaf shows the TA module body **plus** back-references — TN/TQ
chunks that cite it via `support_ref:`. The cross-reference is the
high-value relationship here (translators reading TA wonder *"where is
this principle used?"*). This is one of the core Door43-translator
workflows.

### Tier 3 — specialist trees

For linguists, students, and power users. Surface them via a "more"
section in the nav.

#### 9. Lexicon tree (Greek + Hebrew)

```
/lexicon/grc                    ← Greek (LSJ + Abbott-Smith)
  Strong's 1–999
    G0001 / G0002 / ...
  Strong's 1000–1999
  ...
/lexicon/hbo                    ← Hebrew (BDB)
  Strong's 1–999
  ...
```

Or alternative entry-point: `/lexicon/strongs/<X####>` (e.g.
`/lexicon/strongs/G0026`) returns the LSJ + Abbott-Smith entries for
that number side-by-side. Useful for word-study workflows.

#### 10. Morphology tree (verse-level word-by-word parse)

```
/morphology/ot/GEN/1/1          ← Genesis 1:1 word-by-word Hebrew + Strong's + parse
/morphology/nt/TIT/1/1          ← Titus 1:1 word-by-word Greek + Strong's + parse
```

For each verse, returns the morphology chunk that has the word-by-word
table. Useful as a layer beside the Bible/Scripture trees rather than
its own browse path.

#### 11. Video transcript tree (BibleProject)

```
/transcript                     ← /transcript
  Insight-Videos                ← /transcript/Insight-Videos
    Nissah-Test / Midbar-Wilderness / Outcry / ...
  Redemption / Deuterocanon-Apocrypha / Root / Script-References
```

Each video has up to three parallel chunk sets (timestamp /
bible_reference / semantic) — the leaf can default to one and offer a
strategy facet. See `chunk_strategy:` tag.

#### 12. Kind tree (faceted by content shape)

```
scripture / bible / translator-note / question / term /
methodology / study-note / lexicon / morphology / video-transcript /
section-heading / book-intro / map / image
```

Each `kind:*` value is a top-level branch. Sub-organization mirrors
Source. Useful as a "show me everything of type X" facet picker.

### Lookup views (input → list)

Not hierarchical, but core to the UI. Render as separate panes or
hover-cards.

#### Cross-references view

```
/cross-references/<bbcccvvv>    e.g. /cross-references/45005001  (Romans 5:1)
```

Returns the TSK + BSB-parallel cross-reference list: a curated set of
related verses (with source attribution and confidence rank). Click a
target verse → drill into the Scripture or Bible tree.

#### Concordance view

```
/concordance/<word>             e.g. /concordance/righteousness
```

Returns every BSB verse containing the given word (case-insensitive,
no stemming — exhaustive listing, not BM25-ranked).

#### Section heading anchor (as part of pericope/scripture)

Just metadata exposed alongside Bible/Scripture verse leaves. The
section heading title ("The Qualifications of Elders") is returned with
the chunks it covers; clicking opens a Pericope view of that section.

## URL scheme for your frontend app

```
/                                                home — tree picker
/c/<chunk_id>                                    canonical chunk permalink

/<lang>/scripture/<testament>/<book>[/<chapter>[/<verse>]]
/<lang>/bible/<testament>/<book>[/<chapter>[/<verse>]]
/<lang>/source/<provider>[/<resource>[/<sub>[/<doc>]]]
/<lang>/term/<type>[/<entity>]
/<lang>/topic[/<topic-id>]
/<lang>/entity/<type>[/<entity-id>]
/<lang>/methodology/<section>[/<module>]
/<lang>/pericope/<start>-<end>
/<lang>/lexicon/<lang3>[/<strongs-range>[/<strongs>]]
/<lang>/morphology/<testament>/<book>/<chapter>/<verse>
/<lang>/transcript[/<series>[/<video>]]
/<lang>/kind/<kind>[/<sub>[/<id>]]

/<lang>/cross-references/<bbcccvvv>             lookup view
/<lang>/concordance/<word>                      lookup view

/q?q=…&lang=en                                   search results page
/ask?q=…&lang=en                                 RAG (full LLM answer) page
```

`/c/<chunk_id>` is the **always-resolvable permalink** — citation links
in answers point here. Tree paths are the *navigation* layer; chunk_ids
are the *citation* layer. If trees restructure, citations still resolve.

`<lang>` is reserved as the outermost segment from day one (currently
only `en` is populated). Bake it into URLs even before adding more
languages, so future i18n doesn't break URLs.

## API contract

Base URL: `https://api.yourapp.dev` (your Railway deployment).
Auth: see [Auth and CORS](#auth-and-cors) below — most read endpoints
are public; gated endpoints need a password header.

All responses JSON unless noted. Errors are `{"detail": "..."}` with
appropriate HTTP status.

### Endpoint catalog (full surface)

The complete list of endpoints the client should target. **Build the
client against this whole surface from day 1** — including the 🚧
stage-3-pending ones. Stub the planned routes so they light up without
client changes when the backend lands them.

| Endpoint | Status | Auth | Returns | Used by client for |
|---|---|---|---|---|
| `GET /api/health` | ✅ | none | `HealthResponse` | startup readiness check, version display |
| `GET /api/tree/<name>` | ✅ * | none | `TreeBranch` (children) | tree root listing |
| `GET /api/tree/<name>/<path...>` | ✅ * | none | `TreeBranch` (children or chunks) | tree drill-down |
| `GET /api/chunk/<chunk_id>` | ✅ | none | `Chunk` | chunk full body + cross-refs page |
| `GET /api/search?q=…` | ✅ | none | `SearchResponse` | search box |
| `GET /api/search?q=…&semantic=true` | ✅ | password | `SearchResponse` | search box (semantic mode) |
| `POST /api/ask` | ✅ | password | `AskResponse` | RAG / Ask page |
| `POST /api/ask` (SSE) | 🚧 | password | event stream | streaming Ask page |
| `GET /api/topic/<id>` | 🚧 | none | `TopicDetail` | Topic detail page |
| `GET /api/entity/<id>` | 🚧 | none | `EntityDetail` | Entity detail page (graph + map + verses) |
| `GET /api/cross-references/<bbcccvvv>` | 🚧 | none | `CrossReferenceResponse` | "see also" panel on verse pages |
| `GET /api/concordance/<word>` | 🚧 | none | `ConcordanceResponse` | "every occurrence of" view |
| `GET /mcp` | ✅ | none / password (per-tool) | MCP discovery JSON | not used by browser frontend |
| `POST /mcp` | ✅ | none / password (per-tool) | JSON-RPC 2.0 | not used by browser frontend |

\* Tree builders for `scripture`, `source`, `kind`, `term`,
`methodology`, `pericope`, `aquifer` are available now. Builders for
`bible`, `topic`, `entity`, `lexicon`, `morphology`, `transcript` are
planned for stage 3 — same `/api/tree/...` endpoint shape, additional
`<name>` values become valid as builders ship.

**Status legend:**
- ✅ Available now (tested, eval-passing).
- 🚧 Planned for stage 3. Spec is frozen — build the client against it;
  the route handlers are pending.

**Versioning policy:** the `schema_version` in `/api/health` bumps when
the on-disk schema changes (currently `"2"`). Breaking response-shape
changes will bump it; additive fields will not.

### `GET /api/health`

Sanity check + corpus metadata.

```json
{
  "status": "ok",
  "ready": true,
  "schema_version": "2",
  "indexed_at": 1717689600,
  "embedding_model": "text-embedding-3-small",
  "vec_loaded": true,
  "counts": { "documents": 90681, "chunks": 90681, "vectors": 90681 }
}
```

When the index has not been bootstrapped yet, `status: "uninitialized"`
and `ready: false` are returned with HTTP 200 (so platform health
checks pass while the volume populates).

### `GET /api/tree/<tree_name>?lang=en`

Top-level tree listing for one of the registered trees. `tree_name` is
one of: `scripture`, `bible`, `source`, `kind`, `term`, `topic`,
`entity`, `methodology`, `pericope`, `lexicon`, `morphology`,
`transcript`, `aquifer`.

```http
GET /api/tree/scripture?lang=en

200 OK
{
  "tree": "scripture",
  "lang": "en",
  "nodes": [
    { "id": "ot", "label": "Old Testament", "child_count": 39, "url": "/en/scripture/ot" },
    { "id": "nt", "label": "New Testament", "child_count": 27, "url": "/en/scripture/nt" }
  ]
}
```

### `GET /api/tree/<tree_name>/<path...>?lang=en`

Drill down. The `path` is the same as the URL path you'd use in your
frontend (without the `<lang>` prefix). Intermediate nodes return
`children`; leaves return `chunks`.

```http
GET /api/tree/scripture/nt/TIT/1/6?lang=en

200 OK
{
  "tree": "scripture",
  "lang": "en",
  "node": {
    "passage": "Titus 1:6",
    "bbcccvvv": 56001006,
    "section_heading": "The Qualifications of Elders"
  },
  "chunks": [
    {
      "chunk_id": "f8a3...:0000",
      "title": "ULT — Titus 1:6",
      "kind": "scripture",
      "passage": "Titus 1:6",
      "tags": ["kind:scripture", "book:TIT", "resource:ult"],
      "excerpt": "if anyone is blameless, a husband of one wife, having faithful children not accused of reckless behavior or rebellion.",
      "primary_path": "/en/scripture/nt/TIT/1/6",
      "permalink": "/c/f8a3...:0000"
    },
    ...
  ]
}
```

### `GET /api/chunk/<chunk_id>`

Full chunk body + cross-references + every tree path the chunk lives in.

```http
GET /api/chunk/f8a3...:0000

200 OK
{
  "chunk_id": "f8a3...:0000",
  "doc_id": "f8a3...",
  "title": "ULT — Titus 1:6",
  "body": "if anyone is blameless...",
  "passage": "Titus 1:6",
  "passage_refs": [[56001006, 56001006]],
  "tags": ["kind:scripture", "book:TIT", "resource:ult"],
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
    "passage": [...],   // other chunks at the same verse (UST, TN, BSB, etc.)
    "support_ref": [...], // TA articles cited by TN rows for this verse
    "term": [...]       // related TW articles
  }
}
```

### `GET /api/topic/<topic-id>?lang=en` *(planned — stage 3)*

Topic detail with verse list. `topic-id` is the slug from the Topic tree
(e.g. `creation`, `faith`, `righteousness-imputed`).

```json
{
  "id": "creation",
  "name": "Creation",
  "source": "naves",
  "passage_count": 1,
  "passages": [
    { "bbcccvvv": 1001001, "human": "Genesis 1:1", "url": "/en/scripture/ot/GEN/1/1" }
  ]
}
```

### `GET /api/entity/<entity-id>?lang=en` *(planned — stage 3)*

Entity detail with relations + verse list.

```json
{
  "id": "person:david_994",
  "type": "person",
  "name": "David",
  "metadata": {
    "alsoCalled": ["David of Bethlehem"],
    "gender": "m",
    "birthYear": -1085,
    "deathYear": -1015,
    "tipnr_unique_name": "David@1Sa.16.13",
    "names": [
      { "ESV_translation": "David", "Hebrew_Greek": "דָּוִד", "extendedStrongs": "H1732" }
    ],
    "openbible_id": null
  },
  "relations": [
    { "relation": "father-of", "target": "person:absalom_59", "name": "Absalom" },
    { "relation": "father-of", "target": "person:solomon_2762", "name": "Solomon" },
    { "relation": "partner-of", "target": "person:bathsheba_X", "name": "Bathsheba" },
    ...
  ],
  "passages": [
    { "bbcccvvv": 9016013, "human": "1 Samuel 16:13", "url": "/en/scripture/ot/1SA/16/13" },
    ...
  ]
}
```

### `GET /api/cross-references/<bbcccvvv>?lang=en&source=tsk` *(planned — stage 3)*

Cross-references for a single verse. `source` filter is optional
(`tsk`, `bsb-parallel`, or omitted for all sources merged by rank).

```json
{
  "source_passage": { "bbcccvvv": 45005001, "human": "Romans 5:1" },
  "cross_references": [
    {
      "target_start_bbcccvvv": 45003028,
      "target_end_bbcccvvv":   45003028,
      "human": "Romans 3:28",
      "url": "/en/scripture/nt/ROM/3/28",
      "source": "tsk",
      "rank": 1
    },
    ...
  ]
}
```

### `GET /api/concordance/<word>?lang=en` *(planned — stage 3)*

Every BSB verse containing the given English word.

```json
{
  "word": "righteousness",
  "verse_count": 257,
  "verses": [
    { "bbcccvvv": 1015006, "human": "Genesis 15:6", "url": "/en/bible/ot/GEN/15/6" },
    ...
  ]
}
```

### `GET /api/search?q=…&lang=en[&filters…]`

Same retrieval as the CLI's `query.ask --no-llm` — no LLM call, just
ranked hits.

```http
GET /api/search?q=qualifications+for+church+leaders&lang=en&kind=scripture&book=TIT

200 OK
{
  "query": "qualifications for church leaders",
  "lang": "en",
  "filters": { "kind": "scripture", "book": "TIT", "source": "all" },
  "semantic": false,
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
      "excerpt": "...",
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
- `book=<USFM>` — restrict to one book.
- `source=<door43|aquifer|all>` — restrict by provenance (default `all`).
- `top_k=<N>` — number of results (default 10, max 50).
- `semantic=true` — opt in to vector retrieval (gated, see Auth below).

### `POST /api/ask`

Full RAG. Free-form question → cited answer. **Gated** behind the
configured API password.

```http
POST /api/ask
Content-Type: application/json
Authorization: Bearer <password>

{
  "question": "What does Titus 1:1 say about being a servant of God?",
  "lang": "en",
  "scope": { "source": "all", "book": null }
}

200 OK
{
  "question": "...",
  "answer": "Paul identifies himself as a servant of God [56001001:0000] and an apostle of Jesus Christ. The translation note explains that the term \"servant\" connects to the Old Testament prophets [...].",
  "citations": [
    {
      "n": 1,
      "chunk_id": "...",
      "title": "ULT — Titus 1:1",
      "passage": "Titus 1:1",
      "kind": "scripture",
      "excerpt": "Paul, a servant of God...",
      "primary_path": "/en/scripture/nt/TIT/1/1",
      "permalink": "/c/...:0000"
    },
    ...
  ],
  "confidence": "high",
  "analysis": { "intent": "passage_specific", "passages": [[56001001, 56001001]] }
}
```

When the system can't answer:

```jsonc
{
  "answer": "I don't see an answer to that in the indexed sources.",
  "citations": [...closest related, in case useful...],
  "confidence": "low",
  ...
}
```

Detect refusal via the canonical opening phrase plus `confidence: "low"`.

### `POST /api/ask` with streaming (Server-Sent Events) *(planned)*

For long answers, stream progressively. Send `Accept: text/event-stream`
to opt in.

```
event: status      → {"phase": "analyzing"}
event: status      → {"phase": "retrieving", "intent": "passage_specific"}
event: hits        → {"count": 8, "preview": [...]}
event: status      → {"phase": "synthesizing"}
event: token       → {"text": "Paul"}
event: token       → {"text": " identifies"}
...
event: complete    → {"answer": "...", "citations": [...], "confidence": "high"}
```

Use the `EventSource` API or `fetch` with a manual stream parser. The
`hits` event lets you show retrieval results before the LLM finishes.

## TypeScript types

```ts
// Common
export type ChunkId = string;       // "ef303bb6670e192e:0000"
export type DocId = string;
export type Lang = "en";            // extend as more languages added
export type Kind =
  | "scripture" | "bible" | "translator-note" | "question"
  | "term" | "methodology" | "study-note" | "book-intro"
  | "lexicon" | "morphology" | "section-heading" | "video-transcript"
  | "dictionary" | "ane-context" | "passage-cluster"
  | "map" | "image"
  | string;  // tolerate future kinds

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
  node: {
    id?: string; label?: string;
    passage?: string; bbcccvvv?: number;
    testament?: "ot" | "nt";
    section_heading?: string;
  };
  children?: TreeNode[];
  chunks?: ChunkPreview[];
}

// Topic
export interface TopicDetail {
  id: string;
  name: string;
  source: "naves" | string;
  passage_count: number;
  passages: { bbcccvvv: number; human: string; url: string }[];
}

// Entity
export interface EntityDetail {
  id: string;                  // 'person:david_994' | 'place:bethlehem_218' | …
  type: "person" | "place" | "event" | "deity";
  name: string;
  metadata: Record<string, unknown>;  // see schema docs for fields
  relations: {
    relation: string;          // 'father-of' | 'spouse-of' | 'occurred-at' | …
    target: string;
    name: string;              // resolved target.name for display
  }[];
  passages: { bbcccvvv: number; human: string; url: string }[];
}

// Cross-references
export interface CrossReference {
  target_start_bbcccvvv: number;
  target_end_bbcccvvv: number;
  human: string;
  url: string;
  source: "tsk" | "bsb-parallel" | string;
  rank?: number | null;
}

export interface CrossReferenceResponse {
  source_passage: { bbcccvvv: number; human: string };
  cross_references: CrossReference[];
}

// Concordance
export interface ConcordanceVerse {
  bbcccvvv: number; human: string; url: string;
}
export interface ConcordanceResponse {
  word: string;
  verse_count: number;
  verses: ConcordanceVerse[];
}

// Search
export interface QueryAnalysis {
  fts_query: string;
  passages: [number, number][];
  tags: string[];
  intent: "thematic" | "entity_lookup" | "passage_specific" | "passage_book" | "methodology"
        | "word-study" | "morphology" | "genealogy" | "ane-context" | "lexicon"; // last 5: stage 4
}

export interface SearchHit extends ChunkPreview {
  score: number;
  retrievers: string[];
}

export interface SearchResponse {
  query: string;
  lang: Lang;
  filters: Record<string, string | null>;
  semantic: boolean;
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

## Data fetching layer (the one architectural rule)

**All data fetching goes through a single `lib/api/` module.** Components
never call `fetch('/api/...')` directly; they call typed functions like
`getTreeNode(...)`, `getChunk(...)`. This is the single rule that keeps
the v1 hybrid migration (static export of stable content) a one-file
change instead of a refactor across components.

### File layout

```
src/
├── lib/
│   ├── api/
│   │   ├── client.ts          ← single fetcher, auth + error handling
│   │   ├── tree.ts            ← getTreeNode, getTreeRoot
│   │   ├── chunk.ts           ← getChunk
│   │   ├── topic.ts           ← getTopic, listTopics
│   │   ├── entity.ts          ← getEntity, listEntitiesByType
│   │   ├── crossrefs.ts       ← getCrossReferences
│   │   ├── concordance.ts     ← getConcordance
│   │   ├── search.ts          ← search
│   │   ├── ask.ts             ← ask, askStream
│   │   ├── health.ts          ← getHealth
│   │   └── index.ts           ← re-exports
│   └── types.ts               ← all TypeScript types from this doc
└── components/
    └── ...                    ← only call lib/api functions
```

### `client.ts` — the single fetcher

Every API call goes through one wrapper. This is where you inject the
password header, normalize errors, and (later) layer in the static-file
fallback.

```ts
// src/lib/api/client.ts
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'https://api.yourapp.dev';
const API_PASSWORD = import.meta.env.VITE_API_PASSWORD ?? '';

export interface ApiError extends Error {
  status: number;
  detail: string;
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit & { authed?: boolean },
): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set('accept', 'application/json');
  if (init?.authed && API_PASSWORD) {
    headers.set('authorization', `Bearer ${API_PASSWORD}`);
  }

  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, mode: 'cors' });

  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    const err = new Error(detail.detail ?? response.statusText) as ApiError;
    err.status = response.status;
    err.detail = detail.detail ?? response.statusText;
    throw err;
  }

  return response.json() as Promise<T>;
}
```

### One module per resource

Each resource module exposes typed functions. Components import these
by name; the URL construction stays inside the module.

```ts
// src/lib/api/tree.ts
import { apiFetch } from './client';
import type { TreeBranch } from '../types';

export function getTreeRoot(tree: string, lang = 'en'): Promise<TreeBranch> {
  return apiFetch<TreeBranch>(`/api/tree/${tree}?lang=${lang}`);
}

export function getTreeNode(
  tree: string,
  path: string[],
  lang = 'en',
): Promise<TreeBranch> {
  const segs = path.map(encodeURIComponent).join('/');
  return apiFetch<TreeBranch>(`/api/tree/${tree}/${segs}?lang=${lang}`);
}
```

```ts
// src/lib/api/chunk.ts
import { apiFetch } from './client';
import type { Chunk } from '../types';

export function getChunk(chunkId: string): Promise<Chunk> {
  return apiFetch<Chunk>(`/api/chunk/${encodeURIComponent(chunkId)}`);
}
```

```ts
// src/lib/api/search.ts
import { apiFetch } from './client';
import type { SearchResponse } from '../types';

export interface SearchParams {
  q: string;
  lang?: string;
  kind?: string;
  book?: string;
  source?: 'door43' | 'aquifer' | 'all';
  top_k?: number;
  semantic?: boolean;
}

export function search(params: SearchParams): Promise<SearchResponse> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  }
  // semantic mode is password-gated
  return apiFetch<SearchResponse>(`/api/search?${qs}`, {
    authed: params.semantic === true,
  });
}
```

```ts
// src/lib/api/ask.ts
import { apiFetch } from './client';
import type { AskResponse } from '../types';

export interface AskParams {
  question: string;
  lang?: string;
  scope?: { source?: string; book?: string | null };
}

export function ask(params: AskParams): Promise<AskResponse> {
  return apiFetch<AskResponse>(`/api/ask`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(params),
    authed: true,
  });
}

// Streaming variant (when /api/ask SSE lands; stage 3)
export async function* askStream(params: AskParams) {
  const API_BASE = import.meta.env.VITE_API_BASE_URL;
  const API_PASSWORD = import.meta.env.VITE_API_PASSWORD ?? '';
  const r = await fetch(`${API_BASE}/api/ask`, {
    method: 'POST',
    headers: {
      'accept': 'text/event-stream',
      'content-type': 'application/json',
      'authorization': `Bearer ${API_PASSWORD}`,
    },
    body: JSON.stringify(params),
  });
  if (!r.ok || !r.body) throw new Error(`ask SSE: ${r.status}`);
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const event = /^event: (.+)$/m.exec(block)?.[1];
      const data = /^data: (.+)$/m.exec(block)?.[1];
      if (event && data) yield { event, data: JSON.parse(data) };
    }
  }
}
```

```ts
// src/lib/api/topic.ts                  (🚧 stage-3-pending)
export function getTopic(id: string, lang = 'en') {
  return apiFetch<TopicDetail>(`/api/topic/${encodeURIComponent(id)}?lang=${lang}`);
}

// src/lib/api/entity.ts                 (🚧 stage-3-pending)
export function getEntity(id: string, lang = 'en') {
  return apiFetch<EntityDetail>(`/api/entity/${encodeURIComponent(id)}?lang=${lang}`);
}

// src/lib/api/crossrefs.ts              (🚧 stage-3-pending)
export function getCrossReferences(bbcccvvv: number, source?: 'tsk' | 'bsb-parallel') {
  const qs = source ? `?source=${source}` : '';
  return apiFetch<CrossReferenceResponse>(`/api/cross-references/${bbcccvvv}${qs}`);
}

// src/lib/api/concordance.ts            (🚧 stage-3-pending)
export function getConcordance(word: string, lang = 'en') {
  return apiFetch<ConcordanceResponse>(`/api/concordance/${encodeURIComponent(word)}?lang=${lang}`);
}
```

For the 🚧 stage-3-pending modules: write them now, give them the same
function signatures the API will accept, and stub the call in the
calling component (e.g., show "Topics tree coming soon" with a link
to a future-implementation page). When stage 3 lands, the routes go
live; the client doesn't change.

### Cache key convention

If you use TanStack Query / SWR / SvelteKit data load, standardize the
cache keys so the same content cached once is shared across mounts.

```ts
export const queryKeys = {
  tree: (tree: string, path: string[], lang: string) =>
    ['tree', tree, ...path, lang] as const,
  chunk: (chunkId: string) =>
    ['chunk', chunkId] as const,
  topic: (id: string) =>
    ['topic', id] as const,
  entity: (id: string) =>
    ['entity', id] as const,
  crossRefs: (bbcccvvv: number, source?: string) =>
    ['xrefs', bbcccvvv, source ?? 'all'] as const,
  concordance: (word: string) =>
    ['concordance', word.toLowerCase()] as const,
  search: (params: SearchParams) =>
    ['search', params.q, params.kind ?? '', params.book ?? '', params.source ?? 'all',
     params.semantic ?? false, params.top_k ?? 10] as const,
};
```

### Suggested cache TTLs

The backend serves with reasonable defaults; you don't need to override
unless you're being aggressive client-side. Defaults that are safe:

| Endpoint | Client cache (`staleTime`) | Notes |
|---|---|---|
| Tree (intermediate nodes) | 5 min | invalidate on `meta.indexed_at` change via the health endpoint poll |
| Tree (leaf with chunks) | 5 min | same — content rarely changes mid-day |
| Chunk full body | 1 hour | chunk_ids are stable; only changes if corpus is rebuilt |
| Topic / entity detail | 1 hour | static-ish |
| Cross-references | 1 hour | derived from TSK/BSB; changes only on rebuild |
| Concordance | 1 hour | derived from BSB chunks; same |
| Search | 0 (don't cache) | query-dependent |
| Ask | 0 (don't cache) | LLM output, query-dependent |

Set a global `staleTime: 5 * 60 * 1000` and override per-resource in
each `lib/api/*.ts` if you want longer.

### Component integration (React + TanStack Query)

Components stay thin — they ask for typed data and render it.

```tsx
// src/routes/scripture/[testament]/[book]/[chapter]/[verse].tsx
import { useQuery } from '@tanstack/react-query';
import { getTreeNode, queryKeys } from '@/lib/api';

export function VersePage({ testament, book, chapter, verse }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.tree('scripture', [testament, book, chapter, verse], 'en'),
    queryFn: () => getTreeNode('scripture', [testament, book, chapter, verse]),
    staleTime: 5 * 60 * 1000,
  });

  if (isLoading) return <Skeleton />;
  if (error) return <ErrorPanel error={error} />;
  if (!data?.chunks) return null;

  return (
    <article>
      <h1>{data.node.passage}</h1>
      {data.node.section_heading && <h2 className="muted">{data.node.section_heading}</h2>}
      <ChunkList chunks={data.chunks} />
    </article>
  );
}
```

### SvelteKit equivalent

```svelte
<!-- src/routes/scripture/[testament]/[book]/[chapter]/[verse]/+page.server.ts -->
<script context="module">
  import { getTreeNode } from '$lib/api/tree';
  export async function load({ params, fetch }) {
    const { testament, book, chapter, verse } = params;
    return {
      branch: await getTreeNode('scripture', [testament, book, chapter, verse], 'en'),
    };
  }
</script>
```

The point: in BOTH frameworks, the only place URL paths are constructed
is `lib/api/tree.ts`. Routes can move, response shapes can grow, and you
change one file.

## Citation rendering

Server returns answer text with inline `[chunk_id]` markers. Render
client-side as numbered footnotes:

```ts
function renderAnswer(response: AskResponse): { text: string; citations: Citation[] } {
  let text = response.answer;
  const idToN = new Map(response.citations.map(c => [c.chunk_id, c.n]));
  text = text.replace(/\[([A-Za-z0-9:_-]+)\]/g, (_, id) => {
    const n = idToN.get(id);
    return n ? `[${n}]` : "";   // backend already drops hallucinated ids;
                                // this is defense-in-depth
  });
  return { text, citations: response.citations };
}
```

Each `[N]` becomes a hover-card or anchor link to the citation's
`primary_path`. Permalink fallback: `/c/<chunk_id>`.

## Search vs. Ask — when to use which

| User action | UX | API |
|---|---|---|
| typing in a search box, browsing results | live results, click to drill | `/api/search` (~50–500 ms) |
| asking a natural-language question, expecting a synthesized answer | progressive (streaming), citations as footnotes | `/api/ask` (~2–6 s; gated) |

Both produce citations, but search returns *only* chunks (let the user
read sources themselves), while ask returns a *generated answer* with
citations attached. For a translation-helps tool, **default to search**;
ask is more LLM-cost-intensive, slower, and password-gated.

## Pagination

Tree leaves and search results return up to ~50 items by default. For
larger sets, use `?offset=N&limit=N`:

```http
GET /api/tree/source/aquifer/AquiferOpenStudyNotes?lang=en&offset=50&limit=50
```

Topic and Entity verse lists can be long (hundreds for popular topics
like "faith"). Apply the same pagination there.

## Auth and CORS

The backend runs on a different origin from the frontend, so CORS
matters.

- **Allowed origins** are configured server-side via
  `CORS_ALLOWED_ORIGINS` (your Netlify preview + production domains,
  plus localhost for dev).
- **Read endpoints** (`GET /api/health`, `/api/tree`, `/api/chunk`,
  `/api/topic`, `/api/entity`, `/api/cross-references`,
  `/api/concordance`, plain `GET /api/search`) are **public**. No
  password needed.
- **Gated endpoints** (consume an AI provider API key):
  - `POST /api/ask` — always password-gated.
  - `GET /api/search?semantic=true` — password-gated when the embedding
    layer is invoked.
  Pass the password via either header:
  - `Authorization: Bearer <password>`
  - `X-API-Key: <password>`
- Per-IP rate limits apply: see `docs/server.md`.
- **No cookies needed**. Use `mode: "cors"` on `fetch` calls.

Local development: set `VITE_API_BASE_URL=http://localhost:8765` (or
your framework's equivalent prefix) in `.env.local`. Production:
`VITE_API_BASE_URL=https://api.yourapp.dev`.

## Environment configuration

Frontend env vars (use the prefix your framework expects — `VITE_`,
`PUBLIC_`, `NEXT_PUBLIC_`, etc.):

| Var | Purpose | Default |
|---|---|---|
| `*_API_BASE_URL` | backend origin | `https://api.yourapp.dev` |
| `*_DEFAULT_LANG` | starting language | `en` |
| `*_ENABLE_ASK` | feature flag for `/ask` route (cost gate) | `true` |
| `*_API_PASSWORD` | password for gated endpoints (optional) | unset |

If `*_API_PASSWORD` is not set, the frontend can still use everything
*except* `/ask` and `?semantic=true` searches.

## Frontend implementation patterns

Framework-agnostic patterns that fit React, Astro, or SvelteKit. The
trade-offs that matter for this app:

- **Astro** — best fit for the content-browse parts (tree views, chunk
  pages) because most content is read-mostly and benefits from SSG +
  islands. Use Svelte / React islands only where interactive
  (search box, ask form, chunk hover cards).
- **SvelteKit** — cleanest end-to-end if you want everything in one
  framework. Server-side data loading via `+page.server.ts` matches
  the API perfectly.
- **React (Next.js)** — most flexible, biggest ecosystem. Use App
  Router with React Server Components for tree pages; client
  components for interactive search/ask UI.

All three work. Pick by team familiarity.

### URL-as-state pattern

Tree paths and chunk permalinks *are* the state. Don't store the current
node in client-side state; let the URL drive everything. Each route
loads its own data from `/api/tree/...` or `/api/chunk/...`.

Benefit: deep links work, browser back/forward works, sharing works,
SSG works. Don't fight this.

### Suggested component decomposition

Most of the UI fits this small set of components:

```
<TreePicker />                  — home page (lists the 12 trees)
<TreeNode level=N>              — generic tree node (children or chunks)
  <TreeChildren />              — when intermediate
  <ChunkList />                 — when leaf
<ChunkCard preview />           — used in lists (240-char excerpt + tags)
<ChunkPage chunk />             — full body + cross-refs + tree paths
<CitationHoverCard chunkId />   — citation footnote popup
<EntityCard entity />           — for /entity/* pages (graph + map + verses)
<TopicCard topic />             — for /topic/* pages (verse list + brief)
<SearchBox />                   — global; navigates to /q?q=…
<SearchResults />               — list of <ChunkCard> + filters
<AskForm />                     — interactive; renders <AskResult />
<AskResult streaming />         — answer text + citation footnotes
<XrefList passage />            — embedded in verse pages: TSK + parallel-passage refs
```

Render strategy:
- Tree pages: SSG or SSR. Cacheable for ~5 minutes (back-end ETag is
  `meta.indexed_at`).
- Chunk pages: SSR + per-request cache. Long-cacheable (`max-age=3600`)
  because chunk_ids are stable.
- Search & Ask: client-side only (interactive forms).

### Data fetching

See [Data fetching layer](#data-fetching-layer-the-one-architectural-rule)
above for the canonical module structure. The short version: every API
call goes through `lib/api/<resource>.ts`, never directly via `fetch()`
in components. Wrap with TanStack Query / SWR / SvelteKit `load` for
caching.

### Chunk page (canonical pattern)

```tsx
// /c/<chunk_id> route
async function ChunkPage({ chunkId }: { chunkId: string }) {
  const chunk = await fetch(`${API_BASE}/api/chunk/${chunkId}`).then(r => r.json());
  return (
    <article>
      <h1>{chunk.title}</h1>
      {chunk.passage && <p className="passage">{chunk.passage}</p>}
      <Body kind={chunk.kind} body={chunk.body} />
      <TreePaths paths={chunk.all_paths} />
      <CrossRefs refs={chunk.cross_refs} />
    </article>
  );
}
```

`<Body kind=...>` switches on the chunk kind for layout (verse text vs.
lexicon entry vs. morphology table look very different). Keep one
component per kind, switch on the discriminator.

### Streaming Ask

```ts
async function streamAsk(question: string, onEvent: (event: AskEvent) => void) {
  const response = await fetch(`${API_BASE}/api/ask`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "accept": "text/event-stream",
      "authorization": `Bearer ${API_PASSWORD}`,
    },
    body: JSON.stringify({ question, lang: "en" }),
  });
  const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const eventName = /^event: (.+)$/m.exec(block)?.[1];
      const data = /^data: (.+)$/m.exec(block)?.[1];
      if (eventName && data) onEvent({ type: eventName, data: JSON.parse(data) });
    }
  }
}
```

Plug `onEvent` into your render: `status` events → progress UI, `hits`
event → preview citations early, `token` events → append to the streaming
answer, `complete` event → swap streaming text for the final validated
answer.

### Dealing with multiple kinds in the same list

A single Scripture-tree leaf can return ULT + UST + TN + TQ + TW + TA
chunks side by side. Group them by `kind` for sane reading:

```tsx
function ChunkList({ chunks }: { chunks: ChunkPreview[] }) {
  const grouped = groupBy(chunks, c => c.kind);
  const ORDER: Kind[] = [
    "scripture", "bible", "translator-note", "question",
    "term", "methodology", "study-note", "section-heading",
    "lexicon", "morphology", "video-transcript", "book-intro",
  ];
  return ORDER.flatMap(k => (grouped[k] ?? []).map(c => <ChunkCard preview={c} key={c.chunk_id} />));
}
```

The order above puts the most reader-relevant content first (scripture
text → notes/questions → terms → methodology → niche / specialist
content last).

## Suggested v0 frontend scope

To get something useful in the smallest number of routes, ship tier 1
trees + chunk view + search:

```
/                       home: tree picker, search box
/<lang>/scripture/...   Scripture tree (translator pair)
/<lang>/bible/...       Bible tree (BSB)
/<lang>/source/...      Source tree (universal)
/<lang>/term/...        Term tree (entity articles)
/c/<chunk_id>           chunk view (cross-refs)
/q                      search results
/ask                    RAG (gated by feature flag + password)
```

Eight routes; covers ~95% of navigation. Tier 2 trees (Topic, Entity,
Pericope, Methodology) plug in once the basics work — same data
fetching pattern, different layouts.

Tier 3 trees (Lexicon, Morphology, Transcript, Kind) are specialist —
ship after eval feedback says they're worth surfacing.

## Eight gotchas worth knowing

1. **Citation chunk_ids are opaque, but stable.** They're hashed from
   source paths. Don't try to parse them; treat them as opaque permalink
   tokens. `/api/chunk/<chunk_id>` is the only thing that resolves them.

2. **Multi-passage chunks appear in many tree leaves.** A TN covering
   Ruth 1:14–22 will show up at all 9 verse leaves in the Scripture
   tree. This is correct — don't dedupe at the route level. But within
   a single page render, dedupe by chunk_id when the same chunk would
   otherwise appear twice.

3. **Some chunks have no passage.** TA modules, lexicon entries, some
   Aquifer book-intros aren't book-anchored. They don't appear in
   Scripture / Bible / Pericope trees. Surface via Source / Kind /
   Methodology / Lexicon trees.

4. **ACAI vs Door43 entity-name collision.** Aquifer uses
   `acai:person:Paul`, Door43 uses `term:paul`. Same person, different
   namespaces. The Term tree merges them server-side. The Entity tree
   is a *third* view (graph relations, not text content). Treat the
   three as complementary, not duplicates.

5. **The `book:RUT` tag means "this chunk is about Ruth"**, NOT
   necessarily "this chunk's passage is in Ruth". TW articles inherit
   `book:*` tags from inherited passages — a TW article linked from
   both Titus and Ruth carries both tags.

6. **Refusal is structured, not exceptional.** When the backend can't
   answer (`/api/ask` with insufficient sources), the response has the
   same shape as a successful one — just `confidence: "low"` and the
   canonical opening phrase. Don't treat refusal as an error condition.

7. **The corpus has multiple English Bibles.** ULT / UST / BSB all
   tag verses with the same passage range. Different `kind:` values
   distinguish them: `kind:scripture` is Door43 ULT/UST; `kind:bible`
   is BSB. The Scripture tree returns `kind:scripture`; the Bible tree
   returns `kind:bible`. Don't conflate.

8. **Lexicon and morphology entries cite as chunks but read very
   differently.** Render a `kind:lexicon` chunk as a definition card
   (headword + transliteration + glosses + sense hierarchy); a
   `kind:morphology` chunk as a word-by-word table. Don't render them
   as plain prose — they look broken without structured layout.

## Migration path: API-first → hybrid (v1, when offline / cost / latency demands it)

The v0 design is **all-API**. Don't pre-optimize. But the doc-encoded
architecture above (single `lib/api/` module + standard response
shapes) keeps the v1 hybrid migration to roughly a half-day of work.

### What "hybrid" means

A subset of *single-source flat lookups* gets pre-exported at deploy
time and served as static JSON files alongside the frontend bundle.
The API stays as the source of truth; static files are a CDN-cached
fast path the client tries first and falls back from on miss.

Endpoints that are good candidates for static export (deterministic,
single-source, stable across requests):

| Endpoint | Static-export candidate | Why |
|---|---|---|
| `GET /api/tree/bible/<testament>/<book>/<chapter>/<verse>` | ✓ ideal | Single BSB verse; ~30k flat files, ~5 MB compressed |
| `GET /api/chunk/<chunk_id>` | ✓ optional | Chunks are stable; ~90k files, larger but CDN-friendly |
| `GET /api/topic/<id>` | ✓ ideal | ~4.6k topics, each ~30 verses |
| `GET /api/cross-references/<bbcccvvv>` | ✓ ideal | ~30k bbcccvvv keys, each with a verse-list |
| `GET /api/concordance/<word>` | ✓ ideal | ~14k words |
| `GET /api/entity/<id>` | possible | ~5.6k entities, but verse lists can be big |

Endpoints that **can't** go static (compute multi-source merges or
LLM-driven):

- `GET /api/tree/scripture/...` — leaves merge ULT + UST + TN + TQ
- `GET /api/tree/source/...`, `kind/...`, etc. — facet queries
- `GET /api/search` — BM25 / vec / RRF computation
- `POST /api/ask` — LLM call

### Build-time export

A backend script transforms SQLite tables into per-key JSON files.
Roughly:

```python
# scripts/export_static.py — runs after indexer.build, before deploy
import json, sqlite3
from pathlib import Path

OUT = Path("static/api")
db = sqlite3.connect("indexer/index.db")

# Per-verse Bible (BSB)
OUT.mkdir(parents=True, exist_ok=True)
for chunks_id, body, doc_id in db.execute("""
    SELECT chunks.id, chunks.body, chunks.doc_id FROM chunks
    JOIN tags ON tags.doc_id = chunks.doc_id AND tags.tag = 'kind:bible'
    JOIN passage_refs ON passage_refs.doc_id = chunks.doc_id
"""):
    # ... shape into the same TreeBranch as /api/tree/bible/<...>
    # write to OUT / "tree" / "bible" / testament / book / str(chapter) / f"{verse}.json"
    ...

# Per-topic, per-bbcccvvv cross-refs, per-word concordance — same pattern.
```

The output directory ships alongside your built frontend; Netlify hosts
it at `/static/api/...`.

### Client change (v1)

Update **only** the affected modules in `lib/api/`. Components stay
unchanged. Two-line example:

```ts
// src/lib/api/tree.ts — v1
export async function getTreeNode(
  tree: string,
  path: string[],
  lang = 'en',
): Promise<TreeBranch> {
  // v1 hybrid: try static first for stable single-source trees
  if (STATIC_TREES.has(tree)) {
    const segs = path.map(encodeURIComponent).join('/');
    const r = await fetch(`/static/api/tree/${tree}/${segs}.json`);
    if (r.ok) return r.json();
    // miss → fall through to API (handles trees that weren't exported)
  }
  return apiFetch<TreeBranch>(`/api/tree/${tree}/${path.map(encodeURIComponent).join('/')}?lang=${lang}`);
}

const STATIC_TREES = new Set(['bible', 'topic', 'entity']);
```

Same pattern for `getCrossReferences`, `getConcordance`. ~5 lines per
module. Components don't change.

### Trade-offs at hybrid time

| Pro | Con |
|---|---|
| Bible-text page loads from CDN (sub-100 ms vs ~150 ms API) | Build pipeline gains an export step |
| Bible-text page works if API is down | Static data goes stale until next deploy |
| API cost: drops to near-zero for bibled / topic / xref / concordance reads | Two storage locations to keep in sync |
| Service Worker can prefetch the static pack for offline reads | More files to invalidate on deploy |

**Recommendation:** don't implement v1 until you have specific evidence
(latency complaints, API cost, or an explicit offline requirement).
The v0 API-first design with HTTP caching is fast enough for almost
all use cases, and the migration is cheap when you actually need it.

## See also

- [`server.md`](server.md) — backend implementation plan
- [`architecture.md`](architecture.md) — full system architecture
- [`expansion-plan.md`](expansion-plan.md) — what stage-2 added (this
  doc reflects the post-expansion contract)
- [`mcp.md`](mcp.md) — same data via the Model Context Protocol
- [`query-pipeline.md`](query-pipeline.md) — how `/api/search` and
  `/api/ask` work internally
