# Architecture

The big picture. Two layers, one SQLite file, one ingest pipeline, six
retrievers, an LLM at the end. Read this once and most everything else makes
sense as detail filling in this skeleton.

## Two layers

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 2 — Query pipeline       (user-facing surface)        │
│   free-form question  →  cited answer                       │
│                                                             │
│   query/analyzer.py    intent + passages + tags             │
│   query/retrieve.py    six retrievers + RRF                 │
│   query/synthesize.py  LLM with strict citation constraint  │
│   query/llm.py         Groq → OpenAI fallback               │
│   query/ask.py         CLI entry point                      │
└──────────────────────────┬──────────────────────────────────┘
                           │ reads
┌──────────────────────────▼──────────────────────────────────┐
│ Layer 1 — Index           (one SQLite file: index.db)       │
│                                                             │
│   documents     one row per source unit                     │
│   chunks        addressable text units (LLM-bound + vec)    │
│   chunks_fts    FTS5 over chunks.body  (porter stemming)    │
│   chunks_vec    sqlite-vec, cosine, 1536d                   │
│   documents_fts FTS5 over documents.title                   │
│   passage_refs  encoded BBCCCVVV range overlap              │
│   tags          kind:* / book:* / resource:* / acai:* …     │
│   meta          schema_version, embedding_model, …          │
└─────────────────────────────────────────────────────────────┘
                           ▲
                           │ written by
┌──────────────────────────┴──────────────────────────────────┐
│ Ingest pipeline           (run when sources change)         │
│                                                             │
│   ingest/door43.py    USFM/TSV → Document objects           │
│   ingest/aquifer.py   BibleAquifer JSON → Document objects  │
│   ingest/cli.py       multi-source orchestration            │
│                                                             │
│   indexer/build.py    walks staging dir, writes documents   │
│   indexer/embed.py    OpenAI embeddings → chunks_vec        │
└─────────────────────────────────────────────────────────────┘
```

**Layer 1 = the index.** Source content normalized into a single SQLite file
with multiple retrieval-shape-specific tables. Source-of-truth is whatever's
in `indexer/schema.sql`.

**Layer 2 = the question pipeline.** A free-form question goes in, a cited
answer comes out. Every step in between is in `query/`.

**Ingest** is the third piece — it lives between sources and Layer 1, runs
when sources change (not per-query). It produces staging markdown files that
`indexer/build.py` consumes.

## End-to-end query flow

What happens when a user runs `python -m query.ask "Who was Boaz?"`:

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. CLI entry                                       query/ask.py     │
│    - load_env() (.env → process env)                                │
│    - open_db(index.db)  with sqlite-vec loaded                      │
│    - check has_vec(db) — vector retrieval enabled?                  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 2. Analyze the question                            query/analyzer.py│
│    QueryAnalysis(                                                   │
│      raw="Who was Boaz?",                                           │
│      fts_query="boaz",                          ← stopwords stripped│
│      passages=[],                               ← no ref pattern    │
│      tags=["term:boaz"],                        ← entity extracted  │
│      intent="entity_lookup")                    ← classified        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 3. Embed the question                          indexer/embed.py     │
│    query_vec = embed_texts(["Who was Boaz?"])[0]                    │
│      → OpenAI text-embedding-3-small  → 1536-d float vector         │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 4. Retrieve                                    query/retrieve.py    │
│    Six retrievers run in turn (cheap; SQLite is local):             │
│      fts_search        chunks_fts MATCH "boaz"          → list[Hit] │
│      title_search      documents_fts MATCH "boaz"       → list[Hit] │
│      passage_search    passage_refs overlap (none)      → []        │
│      scripture_search  vec+FTS within kind:scripture    → []        │
│      tag_search        tags WHERE tag = "term:boaz"     → list[Hit] │
│      vector_search     chunks_vec MATCH query_vec       → list[Hit] │
│                                                                     │
│    RRF fusion with intent-specific weights:                         │
│      entity_lookup → title × 2.5, tag × 1.5  (others 1.0/0.8)       │
│                                                                     │
│    → top 10 fused chunk_ids                                         │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 5. Resolve to citation cards               indexer/citations.py     │
│    Each chunk_id → CitationCard(                                    │
│      document_title, passage, source, excerpt-240c, tags, metadata) │
│    Excerpt is 240-char preview for UI display.                      │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 6. Synthesize                              query/synthesize.py      │
│    - Fetch FULL chunk bodies (capped at 1500c) from chunks table    │
│    - Build prompt:                                                  │
│        SYSTEM_PROMPT  (rules + source-type description)             │
│        USER  = "SOURCES:\n[chunk_id] body...\n\nQUESTION: ..."      │
│    - chat_completion()  → Groq (Llama 3.3 70B)                      │
│        on rate-limit / 5xx → OpenAI (gpt-4o-mini) fallback          │
│    - Parse JSON: {answer, citations, confidence}                    │
│    - Validate: drop any inline [chunk_id] not in retrieved set      │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ 7. Render                                       query/ask.py        │
│    "Boaz was a relative of Naomi's husband [1] and the father of    │
│     Obed [2]."                                                      │
│                                                                     │
│    Sources:                                                         │
│      [1] TW — Boaz (names) — Ruth 2:1                               │
│          door43/tw/names/boaz.md                                    │
│          "Boaz was a wealthy relative of Naomi…"                    │
│      [2] ULT — Ruth 4:21 — Ruth 4:21                                │
│          ...                                                        │
└─────────────────────────────────────────────────────────────────────┘
```

For deep-dives on each step, see [query-pipeline.md](query-pipeline.md).

## End-to-end ingest flow

Run once when sources change. Idempotent on re-run.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Sources                                                             │
│   Door43            git.door43.org/unfoldingWord/{en_ult, en_ust,   │
│                       en_tn, en_tq, en_twl, en_tw, en_ta}           │
│   BibleAquifer      github.com/BibleAquifer/* (per-language)        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Source-specific normalization (parallel HTTP fetch)                 │
│                                                                     │
│   ingest/door43.py            ingest/aquifer.py                     │
│   - fetch USFM/TSV per book   - list org repos                      │
│   - parse_usfm_verses()       - filter by skip-list                 │
│   - parse TN/TQ/TWL TSVs      - fetch eng/json/<NN>.content.json    │
│   - extract tw_refs, ta_refs  - HTML→text strip                     │
│   - fetch referenced TW/TA    - derive book + ACAI tags from        │
│     (with passage inheritance)  associations                        │
│                                                                     │
│   For each row → Document → markdown file with YAML frontmatter:    │
│      ingest/_staging/door43/<resource>/<id>.md                      │
│      ingest/_staging/aquifer/<RepoName>/<content_id>.md             │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Index build                                  indexer/build.py       │
│   - schema.sql applied (idempotent CREATE … IF NOT EXISTS)          │
│   - walk _staging/ for *.md                                         │
│   - indexer/adapters/markdown.py:                                   │
│       parse frontmatter → Document(id, title, chunks, tags,         │
│                                    passage_refs, metadata)          │
│   - INSERT into documents (CASCADE-deletes old chunks/passages/tags)│
│   - INSERT chunks → triggers chunks_fts + documents_fts populate    │
│   - INSERT passage_refs, tags                                       │
│   - cleanup orphan chunks_vec                                       │
└─────────────────────────────────┬───────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│ Embed                                          indexer/embed.py     │
│   - LEFT JOIN chunks_vec to find un-embedded chunks                 │
│   - Batch 100 at a time → OpenAI text-embedding-3-small (1536d)     │
│   - Pack via sqlite_vec.serialize_float32 → INSERT chunks_vec       │
│   - Record embedding_model in meta (refuses to mix models)          │
└─────────────────────────────────────────────────────────────────────┘
```

For deep-dives on staging format and schema, see
[data-pipeline.md](data-pipeline.md).

## Why the generative LLM is only at the end

The query path runs exactly **one generative LLM call per query**, at the
synthesis step. Everything before it — analyzer, retrievers, RRF, citation
resolution — is deterministic code (regex + SQL + sqlite-vec ANN). One
hosted *encoder* model (`text-embedding-3-small`) runs once per query at
embed time, but it produces a vector, not text — it can't hallucinate
because it has nothing to generate.

**This shape is load-bearing for the citation-constraint guarantee.** If
the LLM were involved at retrieval time (query rewriting, content
selection, re-ranking, intent classification), citations would be claims
about content the model *chose* to look at — and that choice surface
would be unauditable. With the current design, citations are claims
about content that **deterministically reached the prompt** through
explicit retrieval logic. The provenance chain is verifiable end-to-end:

```
question  →  passages/tags/intent  →  retrieved chunk_ids  →  prompt  →  answer + citations
            (regex)                  (SQL)                              (LLM, validated)
            verifiable               verifiable                         claims constrained to prompt
```

Every step except the last is reproducible byte-for-byte from the question
and the database state. The last step — the LLM's answer — is constrained
by downstream validation (`query/synthesize.py:_extract_inline_citations`)
that drops any cited chunk_id not in the retrieved set, before render. A
hallucinated source can't survive into the user-visible answer.

**Operational consequence:** debugging a wrong answer is structured. If
the right chunk wasn't retrieved, that's an analyzer or retrieval issue
(both inspectable via `query.ask --no-llm --json`). If the right chunk
was in the prompt but the answer didn't use it, that's a synthesis
issue (inspectable via the run's `answer` text). The two failure modes
never blur. The eval framework leans on this separation: `passage_recall`
/ `tag_recall` measure retrieval, `substring_recall` / `refusal_correct`
measure synthesis, and the metrics don't co-vary mysteriously.

This is the single most important architectural commitment in the
project. Any future change that puts an LLM upstream of retrieval —
LLM-based query rewriting, agentic tool-use loops, model-driven
re-ranking — has to either preserve this property explicitly (record
the LLM's contribution into a deterministic structure that still gates
the prompt) or accept losing it. Both are reasonable engineering
choices in context; what's not reasonable is losing the property
without realizing it.

## Module map

```
indexer/                          Layer 1 + ingest target
├── schema.sql                    table + FTS5 + vec0 definitions
├── build.py                      directory walker + DB writer
├── embed.py                      OpenAI vectors → chunks_vec
├── adapters/
│   ├── base.py                   Document dataclass + Adapter Protocol
│   └── markdown.py               YAML-frontmatter markdown adapter
├── references.py                 BBCCCVVV codec + parse_references()
├── citations.py                  chunk_id → CitationCard resolver
├── db.py                         open_db() with sqlite-vec auto-loaded
├── env.py                        .env loader (idempotent)
└── query.py                      diagnostic CLI (passage / tag / FTS / stats)

ingest/                           Source-specific normalizers
├── door43.py                     unfoldingWord catalog → staging
├── aquifer.py                    BibleAquifer GitHub org → staging
└── cli.py                        top-level multi-source CLI

query/                            Layer 2 — the question pipeline
├── analyzer.py                   intent + passages + tags
├── retrieve.py                   six retrievers + intent-weighted RRF
├── synthesize.py                 LLM prompt + citation validation
├── llm.py                        Groq + OpenAI fallback wrapper
└── ask.py                        CLI entry point

eval/
├── set/v1.yaml                   curated cases
├── run.py                        runner + metrics + JSON output
└── runs/                         per-run output (gitignored)

tools/                            standalone diagnostics (not part of pipeline)
├── map_data_sources.py           probe MCP servers for tool catalogs
└── probes/                       per-MCP-source probes
```

## Data formats at boundaries

| Boundary | Format |
|---|---|
| Source → ingest | HTTP fetch (USFM, TSV, JSON) |
| Ingest → build | Markdown files with YAML frontmatter (`title`, `tags`, `passages`) under `ingest/_staging/<source>/<resource>/<id>.md` |
| Build → schema | SQLite rows in `documents` + `chunks` + `passage_refs` + `tags` |
| Embed → vectors | sqlite-vec `chunks_vec` virtual table, cosine distance, 1536d |
| Retrieve → cards | `CitationCard` dataclass (chunk_id, title, passage, tags, source, excerpt, metadata) |
| Synth → output | JSON `{answer, citations, confidence}` + rendered text |

## Key invariants

- **`chunk_id` is stable** across rebuilds when source content doesn't
  change. Derived from `sha256(source_path)[:16] + ":" + chunk_index`.
  This means existing `chunks_vec` rows survive a rebuild — no re-embed
  needed if bodies didn't change.
- **`source_path` is the deduplication key** for documents. Same source
  path = same document, replaced on rebuild (CASCADE deletes chunks).
- **Tags are flat strings**, namespace-prefixed (`kind:scripture`,
  `book:TIT`, `term:boaz`, `acai:person:Paul`). Filter via `tag = ?`.
- **Passages are encoded as BBCCCVVV** (book × 1M + chapter × 1k + verse).
  Range overlap = `a.start ≤ b.end AND a.end ≥ b.start`.
- **Citations are deterministically validated**: a chunk_id in the LLM's
  output that wasn't in its prompt is dropped before render. The system
  cannot fabricate sources.

## Where each piece lives

For "which file does what" answers:

| Question shape | File |
|---|---|
| "How is X parsed at ingest?" | `ingest/door43.py` or `ingest/aquifer.py` |
| "What does the schema look like?" | `indexer/schema.sql` |
| "How does the analyzer extract intent?" | `query/analyzer.py` |
| "Why is the title-FTS retriever there?" | `query/retrieve.py:title_search` and [decisions.md](decisions.md) |
| "How does RRF work?" | `query/retrieve.py:rrf` |
| "What does the LLM see?" | `query/synthesize.py:_format_sources` |
| "Which API is called when?" | `query/llm.py` (Groq + OpenAI fallback) and `indexer/embed.py` (OpenAI embeddings) |
| "How is the eval scored?" | `eval/run.py` |
