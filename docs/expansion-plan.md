# Expansion plan: corpus + retrieval surface

A planned next phase of bt-docker-mcp that takes the system from a
deliberately-scoped Titus + Ruth Door43/Aquifer index to a much richer
Bible-translation and Bible-study index — incorporating the
borrow-worthy patterns from
[`comparisons/studybible-mcp.md`](comparisons/studybible-mcp.md) and
[`comparisons/bible-study-assistant.md`](comparisons/bible-study-assistant.md).

This is a *plan*, not yet built. None of these changes have been
implemented. Reading order if you're picking up the work: this doc
first, then [`architecture.md`](architecture.md) and
[`decisions.md`](decisions.md) to understand what stays untouched.

## Goal

Extend the index from ~1.3k chunks (Titus + Ruth, English) to a much
broader Bible-translation + Bible-study corpus, while keeping every
load-bearing property intact:

- **Single-file SQLite + sqlite-vec.** One `index.db`, even when ~1 GB.
- **LLM only at synthesis.** Exactly one LLM call per query, server-side,
  after deterministic retrieval.
- **Deterministic citations.** Every cited provenance id reached the
  synthesis prompt deterministically; hallucinated ones dropped before
  render.
- **Source-agnostic `kind:*` taxonomy.** New content types tagged
  uniformly; no provider-specific retrieval code.
- **Live freshness via cron.** `indexer.refresh` keeps working;
  immutable snapshots are a *secondary* distribution channel only.

## What's new vs what's unchanged

| | Today (v2) | After expansion |
|---|---|---|
| Chunks | ~1.3k (Titus + Ruth) | ~110k+ (full Bible reachable through multiple lenses) |
| `kind:*` values | scripture, translator-note, question, term, methodology, study-note, book-intro, map, image | + lexicon, morphology, dictionary, ane-context, passage-cluster, video-transcript |
| Schema tables | documents, chunks, chunks_fts, documents_fts, chunks_vec, passage_refs, tags, meta | + entities, entity_relations, entity_passages |
| Retrievers | 6 (fts, title, passage, scripture, tag, vec) | 6 + lexicon_search, morphology_search, entity_search, ane_search, passage_cluster_search |
| Analyzer intents | 5 | + word-study, morphology, genealogy, ane-context, lexicon |
| Citation system | chunk_id → CitationCard | provenance_id → CitationCard (chunks, entities, lexicon entries, graph relations) |
| MCP tools | 5 visible + 1 hidden ask | + word_study, lexicon_search, explore_genealogy, get_ane_context, find_similar_chunks |
| Synthesis | one LLM call, chunk bodies in prompt | one LLM call, mixed-provenance content in prompt, per-kind body caps |

Everything in the **architecture** column of the existing docs stays
the same; this is purely additive.

## Sources to ingest (phase-1 subset)

From the licensing audit in
[`comparisons/studybible-mcp.md`](comparisons/studybible-mcp.md), the
safely-licensed subset that goes in first:

| Source | License | What we get |
|---|---|---|
| LSJ (Liddell-Scott-Jones) | Public domain | ~10k Greek lexicon entries |
| Abbott-Smith Greek NT | Public domain | ~5.9k NT-focused Greek entries |
| BDB (Brown-Driver-Briggs) | Public domain | ~8k Hebrew lexicon entries |
| STEPBible tagged texts | CC BY (per file) | Verse-level Greek/Hebrew + morphology + Strong's |
| ACAI named entities | CC BY (already in our Aquifer pipeline) | 4,299 person/place/keyterm entities — **dedupe** |
| Aquifer Open Study Notes | CC BY (already in our Aquifer pipeline) | 102k notes — **dedupe** |
| FIA Key Terms | CC BY (already in our Aquifer pipeline) | 200+ terms — **dedupe** |
| Theographic Bible Metadata | CC BY | Genealogy graph: 1,100+ persons + relations + events + places |
| BibleProject videos / PDFs | Per-asset (CC BY-SA on most video transcripts) | Long-form theological prose; ingest via the multi-strategy chunking pattern below |

Deferred until license is confirmed: Tyndale Bible Dictionary, Heiser
content, Torah Weave (Moshe Kline). Add later as a phase-2 batch.

The "redistribute studybible-mcp's compiled `study_bible.db`" path is
**not** the strategy. We re-derive from upstream sources so:
1. License footprint is clear per-source.
2. Cron refresh continues to work — their snapshot is frozen.
3. Citation provenance is auditable to the original source.

## Schema additions

### New `kind:*` values

Source-agnostic content shape, tagged uniformly on `chunks.tags`:

| Tag | Applies to |
|---|---|
| `kind:lexicon` | LSJ, Abbott-Smith, BDB entries |
| `kind:morphology` | Verse-level word parses (Strong's + lemma + parse code) |
| `kind:dictionary` | Tyndale articles, FIA Key Terms (when distinct from `kind:term`) |
| `kind:ane-context` | ANE entries by dimension |
| `kind:passage-cluster` | Section/passage groupings (5,290 from their corpus) |
| `kind:video-transcript` | BibleProject and similar long-form video content |

These slot into the existing `tags` table; no schema change needed for
the chunk side.

### Per-kind FTS5 tables (v3 content)

Adding ~25k lexicon chunks to a single `chunks_fts` shifts BM25 corpus
stats (IDF, average doc length) for the whole index, which re-ranks even
*unrelated* v2 results. So v3 content lives in dedicated FTS5 tables
keyed by kind, each `content='chunks'` (sharing rowids — no body
duplication):

```sql
CREATE VIRTUAL TABLE chunks_fts_lexicon USING fts5(
  body, content='chunks', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 2'
);
-- one per v3 kind as it lands: morphology, dictionary, ane-context,
-- passage-cluster, video-transcript
```

`chunks_fts` is auto-populated for every chunk (existing trigger), then
`indexer.build` routes v3 chunks into their per-kind table and removes
them from `chunks_fts`. The dict driving the routing
(`V3_KIND_TO_FTS` in `indexer/build.py`) is one-line-extensible per new
kind — pair with one `CREATE VIRTUAL TABLE` in `schema.sql`.

This is what makes studybible-mcp's per-content-type isolation valuable
(clean BM25, room for per-kind tokenizer choice) without paying the
cost of splitting `chunks` itself (which would force every passage /
tag / cross-ref join to UNION across N tables). Single chunks table,
multiple FTS tables.

### New tables for graph data

Genealogy / events / places are *graph*, not text. Modeling them as
chunks ("Boaz, who is the son of Salmon and the husband of Ruth…") is
worse than a real graph for traversal queries.

```sql
CREATE TABLE entities (
  id TEXT PRIMARY KEY,         -- 'person:boaz', 'place:bethlehem', 'event:exodus'
  type TEXT NOT NULL,          -- 'person' | 'place' | 'event' | 'deity'
  name TEXT NOT NULL,
  metadata TEXT                -- JSON: alternate names, dates, cross-refs to chunks
);

CREATE TABLE entity_relations (
  source_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  relation TEXT NOT NULL,      -- 'parent-of' | 'spouse-of' | 'occurred-at' | ...
  metadata TEXT,
  PRIMARY KEY (source_id, target_id, relation)
);

CREATE TABLE entity_passages (
  entity_id TEXT NOT NULL,
  start_bbcccvvv INTEGER NOT NULL,
  end_bbcccvvv INTEGER NOT NULL
);
```

`entities.id` is content-derived and stable across rebuilds (same
property as chunk ids). Citations to entities are first-class
provenance ids alongside chunk ids.

## Multi-strategy chunking (ingest principle from bible-study-assistant)

The single most useful idea from
[`comparisons/bible-study-assistant.md`](comparisons/bible-study-assistant.md):
**a single long-form source document gets chunked multiple ways in
parallel, each chunk strategy serving a different question shape.**

The motivating example is BibleProject PDFs (~192 files): one PDF
yields three parallel chunk sets:

| Chunk strategy | Anchor | Question shape it serves |
|---|---|---|
| **Timestamp** | Video time markers | "What does Tim say around the 12-minute mark?" |
| **Bible-reference** | Each passage citation in the PDF | "What BibleProject content discusses Romans 5?" |
| **Semantic** | Coherent argument segments | "What's BibleProject's view on covenant?" |

In our schema all three live in the same `chunks` table, all linked to
the same `documents.id` (the PDF), distinguished by a tag like
`chunk_strategy:timestamp | bible-ref | semantic`. RRF naturally
selects the right strategy per question because each strategy retrieves
strongly only when the question shape matches.

Ingest pipeline shape (mirrors bible-study-assistant `imports/tbp/`
flow):

```
PDF download             ingest/_staging/bibleproject/files/<slug>.pdf
        │
        ▼ (text extraction + reference detection + timestamp parsing)
Metadata extraction      ingest/_staging/bibleproject/extracted/<slug>.json
        │
        ▼ (three parallel chunkers)
Chunked outputs          ingest/_staging/bibleproject/chunks/<slug>.{timestamp,bibleref,semantic}.json
        │
        ▼ (indexer.build picks them up like any other staged source)
chunks + tags + passage_refs
```

This pattern generalizes. When we add Tyndale articles or other
long-form content later, the same three-strategy default applies; for
purely structured content (lexicons, morphology) one strategy is
sufficient.

## Retrievers (additions)

Each new retriever fits the existing interface (returns
`(provenance_id, score, retriever_name)` tuples for RRF). Implementation
sits next to `query/retrieve.py`'s existing six retrievers.

| Retriever | What it queries | Returns |
|---|---|---|
| `lexicon_search` | FTS5 over lexicon entries by gloss / English meaning | `lexicon:<id>` |
| `morphology_search` | Exact lookup by Strong's number, lemma, parse code | `chunk:<verse-morphology-id>` |
| `entity_search` | Graph queries by name / relation / passage overlap | `entity:<id>` |
| `ane_search` | FTS + vec over ANE entries, faceted by dimension/period | `chunk:<ane-id>` |
| `passage_cluster_search` | Passage-range intersection with thematic tags | `chunk:<cluster-id>` |

Vector embeddings get applied selectively per content type (not
uniformly):

| Content type | Embed? | Reason |
|---|---|---|
| Lexicon entries | Yes | Definition prose paraphrases well |
| Morphology rows | No | Structured data; query by Strong's # / lemma directly |
| Dictionary articles | Yes | Long-form prose; paraphrased queries miss FTS |
| ANE entries | Yes | Conceptual material |
| Theographic graph rows | No | Structured edges; query via graph traversal |
| Passage clusters | Yes | Same justification as our existing passages |
| Video transcripts | Yes (per chunk strategy) | Long-form, paraphrased queries common |

Total new vectors: ~110k. At 1536d × 4 bytes ≈ 700 MB of vector
storage. Index file goes from ~10 MB to ~1 GB. Trivial for SQLite +
sqlite-vec; Railway volume handles it.

## Analyzer (additions)

Today: 5 intent classes, each biases the RRF weights toward the
relevant retrievers.

Add: `word-study`, `morphology`, `genealogy`, `ane-context`, `lexicon`.

Each new intent is detected via deterministic regex/keyword cues
(matching how the existing analyzer works — see
[`query-pipeline.md`](query-pipeline.md)). For example:

- `word-study`: phrases like "what does the Greek word X mean", "the
  Hebrew for Y", presence of a transliterated lemma
- `morphology`: "what tense is X", explicit Strong's number reference
- `genealogy`: "who is the father of X", "ancestors of Y"
- `ane-context`: "what did people in the ANE think about", "cultural
  background"

The analyzer stays deterministic. We do **not** adopt agentic
tool-routing (where Claude decides which retrievers to call); that
would conflict with "exactly one LLM call per query."

## Citations: generalize from chunk_id to provenance_id

Today: `synthesize.py` validates citations against the set of
`chunk_ids` that reached the prompt.

After: validates against the set of **provenance ids**, where a
provenance id is one of:

- `chunk:<chunk_id>` — text content (existing shape)
- `entity:<entity_id>` — person, place, event
- `lexicon:<lexicon_id>` — LSJ entry, BDB entry, etc.
- `relation:<source_id>:<rel>:<target_id>` — graph fact

Each has a `CitationCard` with the human-readable form (entity name +
canonical Bible refs, lexicon headword + gloss, etc.). The synthesis
prompt sees provenance ids; the LLM is told to cite them; the validator
drops anything not in the input set.

Implementation footprint: extend `indexer/citations.py` and
`query/synthesize.py`. The `CitationCard` shape grows a `kind` field.
The synthesis prompt becomes "cite using provenance ids of any of these
forms." Approximately 150 lines of mostly mechanical code.

This is the only place the existing pipeline needs internal cleanup,
and it preserves the load-bearing property: every cited id reached the
prompt deterministically, and hallucinated ones are dropped before
render.

## Synthesis: same shape, richer prompt

`query/synthesize.py` stays one LLM call per query, after deterministic
retrieval. The prompt now includes a richer mix of provenance-id'd
content (chunks + entity rows + lexicon entries + relations). The
validator still drops hallucinated citations.

Tuning required for the larger / heterogeneous content:

- **Per-kind body caps.** Lexicon entries are short (~400 char cap);
  Tyndale articles are long (existing 1500-char cap); morphology rows
  are tiny. Replaces the global `MAX_BODY_CHARS=1500`.
- **Per-intent content-mix policies.** A `word-study` question gets,
  e.g., 3 lexicon + 2 morphology + 2 scripture rather than the
  generic top-10. Mirrors how the existing
  intent-weighted RRF biases retrievers; this time it biases the
  *prompt composition*.
- **Hermeneutic system-prompt upgrade** (borrowed from
  studybible-mcp's `prompts/system_prompt.md`): genre-specific reading
  guidance, "say 'no answer' before guessing", explicit
  no-paraphrase-without-citation rule. Drops into the existing system
  message; no architectural change.

The TPM ceiling at Groq still applies. Larger prompts plus more chunk
types means we may need to drop `TOP_K` slightly or more aggressively
filter irrelevant retrievers per-intent. Re-run the eval after each
intent class lands to confirm regressions don't sneak in.

## MCP tool catalog (additions)

With the retrievers and entity tables in place, exposing them as MCP
tools is mechanical — same shape as our existing `passage_lookup` /
`entity_lookup`. We don't need 18 tools; ~5 new ones cover the same
query space because our analyzer + RRF compresses several of theirs
into one.

| New MCP tool | Wraps |
|---|---|
| `word_study` | `query.analyzer` (intent=word-study) + `lexicon_search` + `morphology_search` |
| `lexicon_search` | direct lexicon FTS / vec |
| `explore_genealogy` | `entity_search` + graph traversal |
| `get_ane_context` | `ane_search` faceted by dimension/period |
| `find_similar_chunks` | direct vec retrieval, no RRF (discovery framing — borrowed from studybible-mcp's framing of `find_similar_passages`) |

All default to no-LLM (consistent with our existing MCP convention —
see [`mcp.md`](mcp.md) "MCP convention: no model calls in the default
path"). Same auth gate model: open by default, only AI-using tool calls
require the password.

## Order of work

The user-agreed order is **horizontal**, not vertical: schema first,
then retrievers, then analyzer, then MCP tool exposure. Each stage
fully lands before the next.

| Stage | What lands | Acceptance criterion |
|---|---|---|
| 1. Schema | New `kind:*` values; entities + entity_relations + entity_passages tables; provenance_id system in `indexer/citations.py` | Eval set still passes (12/12); new `kind:*` rows visible in DB; entities table populated by a smoke ingest |
| 2. Ingest | Lexicon + STEPBible + Theographic ingest modules write into the new schema; BibleProject ingest with multi-strategy chunking | Index size grows to expected scale; smoke queries against the new content return results via existing retrievers (where applicable) |
| 3. Retrievers | Five new retrievers wired into `query/retrieve.py`; vector embeddings for new content types via `indexer.embed` | Per-retriever unit smoke tests; no regression in existing eval |
| 4. Analyzer | Five new intent classes + RRF weight tuning per intent | New intents detected on hand-crafted prompts; eval set extended with new cases |
| 5. Synthesis | Per-kind body caps; per-intent content-mix policies; hermeneutic system-prompt upgrade | Eval set passing on the extended cases |
| 6. MCP surface | Five new MCP tools registered, exposed under existing auth/rate-limit gates | Tool calls return expected shape; existing MCP clients still work |

After stage 6, expand the eval set substantially to lock in the new
behavior, then revisit the deferred-license sources (Tyndale, Heiser,
Torah Weave) as a phase-2 batch.

## What this plan does *not* change

To keep the plan honest about the invariants it preserves:

- **Server-side synthesis stays.** `/api/ask` continues to return finished
  prose with validated citations. We don't move synthesis to the client.
- **One LLM call per query.** No agentic loop; analyzer remains deterministic.
- **Single-file index.** All new content lives in `index.db`. No
  polyglot stores. No mounting `study_bible.db` alongside.
- **Live freshness primary.** Cron refresh remains the primary path.
  Periodic immutable snapshots are a secondary distribution channel
  (borrowed framing from studybible-mcp), not a replacement.
- **Source-agnostic `kind:*` taxonomy.** Every new content type tagged
  uniformly; no provider-specific retrieval branches.
- **MCP convention: no model calls in the default path.** New tools
  follow the existing convention; semantic / synthesis tools are opt-in
  and password-gated.

## Open questions to track

Will surface during implementation, recording here so they don't get
lost:

- **Greek/Hebrew Unicode in FTS5.** Porter stemming doesn't apply;
  test that lexicon FTS handles polytonic Greek and Hebrew vowel
  pointing without normalization surprises.
- **Entity disambiguation across sources.** Theographic's "Boaz" and
  ACAI's "Boaz" need to merge to one `entity:boaz` id, or queries
  return duplicates. Likely by-name-with-passage-overlap heuristic.
- **Citation card density.** Five new content types each contribute
  citation rows; the UI may need to group/collapse to keep the answer
  readable. Frontend concern, but worth flagging early.
- **Eval set expansion.** Twelve hand-curated cases isn't enough to
  validate this much new content. Need to grow it to ~40–60 covering
  each new intent class.
