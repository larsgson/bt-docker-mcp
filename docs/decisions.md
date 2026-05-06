# Design decisions

Numbered, chronologically-ordered record of the load-bearing design
decisions. Each entry has the problem we faced, the options on the
table, and what we chose with the reasoning. Useful when reverting feels
tempting, or when extending the system in a way that pressures one of
these decisions.

## 1. Two-layer architecture (Layer 1 = index, Layer 2 = query)

**Problem.** Should the system be one big request handler, or split?

**Options.**
- Monolithic: one module that ingests, indexes, retrieves, synthesizes per request.
- Split into ingest-time work and query-time work, with a stable storage interface between.

**Choice.** Split. `indexer/` owns Layer 1 storage and contracts; `query/`
owns Layer 2 logic. Ingest is a third bag separate from both.

**Why.** The most expensive operations (HTTP fetches, embeddings) are
ingest-time. The most latency-sensitive operations (analyze, retrieve,
synthesize) are query-time. Keeping them separate means embedding work
isn't repeated per query, and changing the analyzer doesn't risk the
schema. Also matches what every successful retrieval system does
(Aquifer, translation-helps, both Bible-translation MCPs we studied).

## 2. SQLite + sqlite-vec (vs Qdrant / pgvector / LanceDB)

**Problem.** Where do vectors live?

**Options.**
- `sqlite-vec`: embedded extension, vectors in the same SQLite file as structured data
- `pgvector`: Postgres + extension; serious database, more ops weight
- Qdrant / Weaviate / Pinecone: separate vector store as a service or sidecar
- LanceDB: embedded but separate file

**Choice.** `sqlite-vec`.

**Why.**
- One file = entire knowledge base. Backup is `cp`, deploy is ship-the-file, A/B is swap-the-file.
- Hybrid retrieval is one SQL query — JOIN across structured + FTS + vector tables.
- Zero extra ops on fly.io / Railway. No sidecar containers.
- At our scale (10k–500k chunks) every option performs equivalently; the differentiator is operational not algorithmic.

The `vector_store.py` interface is intentionally thin. Migration to
LanceDB or Qdrant if scale demands later is a half-day swap.

## 3. Aquifer's structured-indexes-first pattern, plus vectors

**Problem.** What's the retrieval primitive?

**Options.**
- Pure semantic: vector ANN against everything; ranking by cosine similarity.
- Pure structured: FTS5 + exact-match tables; no semantics.
- Hybrid: structured signals as filters + boosts, vector for semantic recall.

**Choice.** Hybrid, with structured paths as the spine. Borrowed from
aquifer-mcp's design (passage range, title substring, entity ID).

**Why.** Aquifer's design specifically sidesteps the "semantic search
returns vague matches for specific queries" failure mode. Structured
indexes pin queries that *can* be exact (passage refs, term IDs). Vector
adds semantic recall for the residual. RRF lets them compose. Pure
vector under-performs on rare-term queries (proper nouns, technical
terms, scripture references). Pure structured can't handle paraphrased
questions.

## 4. `kind:*` source-agnostic content tags

**Problem.** Eval cases tied to `resource:ult` started failing when
Aquifer added competing scripture mirrors.

**Options.**
- Filter Aquifer mirrors out (deduplicate at ingest)
- Add per-source priority weights in retrieval
- Tag every chunk with its **content shape** (scripture, translator-note, term, methodology, …) regardless of provenance

**Choice.** Add `kind:<x>` tags. Mirror filter happened separately
(decision #6).

**Why.** A query like "is some scripture content surfacing?" doesn't
care whether it's Door43's ULT or Aquifer's BSB — both are scripture.
Provenance-based tagging (`resource:ult`) was over-specific. The
content-shape taxonomy generalizes naturally to any future source.

## 5. TWL is ingest-only signal (no Documents emitted)

**Problem.** TWL chunks (passage→term link metadata) were winning
title/FTS retrieval with their substantive titles ("TWL — Ruth 1:22 →
bethlehem") but contributing nothing to answers (bodies were
"At Ruth 1:22, the word *bethlehem* maps to translation word: …").

**Options.**
- Lower their weight in retrieval
- Filter `kind:link` out of `title_search` only
- Stop emitting them as Documents entirely; use them only for cross-reference passage inheritance

**Choice.** Stop emitting as Documents.

**Why.** Lifted directly from translation-helps-mcp's design philosophy:
*don't index what you can derive on demand*. The TWL data is metadata
about the corpus structure, not content. Their useful information (which
TW articles cover which passages) is captured at ingest time when
`_ingest_referenced_tw()` inherits passage refs onto the linked TW
articles. Documents are reserved for things that have answer-value.

## 6. Aquifer is supplementary, not primary

**Problem.** Adding all 14 Aquifer English-content repos for TIT+RUT
created 1487 chunks, four of which directly mirrored Door43 content.
Top-K filled with near-duplicates, displacing curated Door43 originals.

**Options.**
- Index everything and tune retrieval to deduplicate
- Skip mirrors at ingest (curated denylist)
- Run Door43 only

**Choice.** Curated default-skip list (`_SKIP_BY_DEFAULT` in
`ingest/aquifer.py`) for the four mirrors plus three alternative
full-Bible translations. Override with `--include-skipped` or
`--repos`.

**Why.** Aquifer's value is **breadth** — study notes, commentary,
methodology, alternative perspectives. Its harm is **redundancy** —
mirrors of Door43 don't add information, only competition. Skipping
mirrors at ingest is the cheapest, clearest separation. The
seven-repo allow-list (~241 articles) is genuinely supplementary.

## 7. Whole-book passage filters as soft hints, not hard filters

**Problem.** `"according to Titus"` extracted to whole-Titus passage
range. With this as a hard `doc_filter`, FTS / vec were limited to
Titus chunks only — which excluded TW articles that cover both Titus
and Ruth (e.g., `term:faithful`, `term:covenantfaith`).

**Options.**
- Drop whole-book parsing
- Keep it but constrain only some retrievers (the scripture-aware ones)
- Treat narrow vs broad ranges differently

**Choice.** Narrow ranges (verse-level, < 999 verses) still act as
hard `doc_filter`; broad ranges (whole-book) are soft hints — used by
`scripture_search` and `passage_search` but not as a hard filter for
FTS / vec.

**Why.** A specific verse query *should* narrow the corpus. A
whole-book scope from "according to Titus" should *bias* retrieval, not
exclude cross-book content that's genuinely on-topic. Different
intents, different filter semantics.

## 8. `chapter` keyword required in references (no bare book names)

**Problem.** Earlier iteration matched bare book names ("Titus" →
whole Titus). But two-letter book aliases ("is" → ISA, "am" → AMO,
"ti" → TIT) collide with English words. The query *"What is a
metaphor and how should it be translated?"* had `is` matched as
Isaiah, narrowing the entire query to Isaiah content (zero hits, refusal).

**Options.**
- Min-length filter on bare aliases (≥ 4 chars)
- Require chapter number always
- Require chapter number, but ALSO recognize book-context phrases
  ("according to X", "in the book of X")

**Choice.** Third option. `parse_references()` requires a chapter
number. `_book_context_passages()` separately matches `(?:in|of|from|
according to|the book of|the gospel of)\s+X` and produces whole-book
ranges. Two distinct code paths, neither of which fires on bare
ambiguous tokens.

**Why.** Surrounding language disambiguates "John the book" from "John
the person". Without that signal, you can't tell.

## 9. RRF weights are per-intent, never zero

**Problem.** Five intents, six retrievers. How to compose?

**Options.**
- Uniform RRF, no intent classification
- Per-intent retriever **selection** (run only the right ones)
- Per-intent **weights**, all retrievers run

**Choice.** Per-intent weights, all retrievers always run. Weights
range 0.5–2.5; never zero.

**Why.** Selection is brittle — a wrong intent guess produces zero hits
from skipped retrievers. Weighting preserves graceful degradation: if
the analyzer mis-classifies, the system still works, just slightly
sub-optimally. Composability over precision.

## 10. Full chunk bodies in synthesis prompt (not the 240-char excerpt)

**Problem.** `CitationCard.excerpt` is 240 chars for UI display. The
synthesizer was using it for the LLM prompt. Translator notes routinely
bury the operative word past 240 chars — TN — Titus 1:6 (jen8) has
"blameless" in *"Alternate translation: [an elder must be **blameless**]"*
which falls past the cutoff. The LLM never saw the word.

**Options.**
- Increase the excerpt cap for everyone
- Have separate "display excerpt" and "synth body" fields
- Synthesizer fetches full bodies from `chunks` table

**Choice.** Synthesizer fetches full bodies (capped at 1500c per chunk).
Excerpt stays 240c for UI.

**Why.** The two consumers have different requirements — UI wants a
short preview, the LLM needs context. Conflating them was a silent
quality cap that masked several "stuck" eval failures across multiple
debugging cycles.

## 11. Title-FTS retriever separate from body-FTS

**Problem.** Entity lookups ("Who was Boaz?") had the right TW article
in the corpus but ranking ~10 in body-FTS due to noise from narrative
passages literally mentioning the name.

**Options.**
- Boost `kind:term` in body-FTS
- Add a `term:<x>` tag heuristic from analyzer
- Run a separate FTS5 search over `documents.title` only

**Choice.** All three, but title-FTS is the dominant lift. Aquifer's
design uses an analogous title index.

**Why.** Titles are short and term-focused (`TW — Boaz (names)`).
Body-FTS saturates. Two separate FTS5 virtual tables — one per body,
one per title — give RRF independent signals for entity-shaped queries.

## 12. `top_k = 10` (not 8 or 12)

**Problem.** `text-embedding-3-small` ranks the answer-bearing chunk
at rank 9-12 for several thematic queries. `top_k = 8` cuts them off.

**Options.**
- Switch to `text-embedding-3-large` (3× cost, better ranking)
- Bump `top_k` to compensate
- Both

**Choice.** Bump `top_k` to 10 (after dual-pass scripture_search lifted
relevant chunks into the 3-6 range). Stay on the smaller embedding
model.

**Why.** Cheaper and sufficient. We can re-evaluate when scale changes.
The cost-side of `text-embedding-3-large` matters more for re-embeds
than per-query inference.

## 13. Groq primary, OpenAI fallback (not the other way around)

**Problem.** Two LLM provider keys configured. Which is primary?

**Choice.** Groq (`llama-3.3-70b-versatile`) primary; OpenAI
(`gpt-4o-mini`) fallback on rate-limit / 5xx / connection errors.

**Why.** Groq is fast and free-tier-ample. Llama 3.3 70B is strong
enough for our synthesis task. OpenAI as the safety net for outages.
Both are OpenAI-compatible, so one SDK pointed at two endpoints — no
provider-specific code paths.

The 4xx errors other than 429 propagate (don't fall back). Those
indicate client-side bugs (malformed prompt, bad key) — falling back
on those would mask the real issue.

## 14. `.env` loading is opt-in via explicit `load_env()`, not auto

**Problem.** Where do API keys come from?

**Options.**
- Auto-load `.env` at module import
- Explicit `load_env()` in CLI entry points
- Process env only

**Choice.** Explicit `load_env()` at the start of every CLI entry's
`main()`. Idempotent — safe to call multiple times.

**Why.** Auto-load at import time triggers in unit tests and other
unintended contexts. Process-env-only is hostile to local development.
Explicit-in-CLI is the cheapest contract that works for both `python
-m query.ask` (loads `.env`) and unit testing (doesn't).

## 15. The eval set is YAML, runs are JSON

**Problem.** Format choice for eval inputs and outputs.

**Choice.** Inputs: YAML (human-curated, hand-edited). Outputs: JSON
(machine-readable, easy to diff with `jq`, append-friendly).

**Why.** YAML's tolerance for comments and multi-line strings serves
case authors. JSON's schema-stability serves diff tools and CI.
Different consumers, different formats.

---

## Decisions we explicitly haven't made yet

These are open and tracked here so they don't get rediscovered later:

- **HTTP / web layer for fly.io / Railway deployment**. Pending; see
  [README.md](../README.md) phased rollout. The CLI shape is good
  enough for v1.x; FastAPI wrapper is a half-day when ready.
- **Per-source SQLite databases**. Currently a column-filter via
  `_docs_by_source()`. If it becomes useful to A/B run incompatible
  schemas, we'd split. Not yet.
- **Vector store abstraction**. `query/retrieve.py:vector_search`
  speaks `sqlite-vec` directly. The interface is small enough that
  swapping to LanceDB / Qdrant is a half-day if scale demands.
- **Per-language ingest beyond English**. The infrastructure is
  there (Aquifer publishes 10+ languages, Door43 publishes per-language
  repos with predictable URLs). Just hasn't been wired through CLI.
- **`get` vs `search` separation in citations**. Currently citations
  return excerpt + full body via the synth path. A future UI may want
  a `get_full_chunk` endpoint to lazy-fetch full bodies on user click,
  reducing default response size. Item 4 from the architecture-alignment
  TODO.
