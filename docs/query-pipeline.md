# Query pipeline (Layer 2)

How a free-form question becomes a cited answer. Three stages:
**analyze**, **retrieve+fuse**, **synthesize+validate**. All in `query/`.

## Stage 1 — Analyzer (`query/analyzer.py`)

Input: raw question string. Output: `QueryAnalysis` dataclass.

```python
@dataclass
class QueryAnalysis:
    raw: str
    fts_query: str                              # FTS5 MATCH expression
    passages: list[tuple[int, int]]             # BBCCCVVV pairs
    tags: list[str]                             # tag candidates (RRF boost)
    intent: str                                 # see below
```

### What the analyzer extracts

1. **Bible references** via `parse_references()` (in `indexer/references.py`).
   Matches `Titus 1:1`, `Rom 3:24-25`, `Ruth chapter 1`, etc. Chapter
   number is required — *bare book names don't trigger* because aliases
   like "is" (Isaiah) and "am" (Amos) collide with English.

2. **Book-context phrases** — when a question says *"according to Titus"*,
   *"in the book of Ruth"*, *"the gospel of John"*, the analyzer adds the
   whole-book BBCCCVVV range. Different code path from (1); this one is
   safe because the lexical context disambiguates "John the book" from
   "John the person".

3. **Entity-lookup terms** — short questions matching patterns like
   `"Who was X?"` / `"What is X?"` / `"Define X"` extract X as a
   `term:<x>` tag candidate. Capped at ≤10 words to avoid false positives
   on complex queries.

4. **FTS keywords** — text with passages stripped, stopwords removed,
   joined as `kw1 OR kw2 OR …`. OR-fusion biases recall over precision;
   RRF re-ranks anyway.

5. **Intent** — categorical classification used by `retrieve()` to weight
   the six retrievers. Five values:

| Intent | Trigger | What it pivots toward |
|---|---|---|
| `entity_lookup` | term candidate extracted | title × 2.5, tag × 1.5 |
| `passage_specific` | passage with verse-level range (< 999 verses) | scripture × 1.5, passage × 1.2 |
| `passage_book` | book-context phrase only | scripture × 1.4, passage × 1.1 |
| `methodology` | matches `translate / figs-X / how do I` regex | title × 1.5, vec × 1.2 |
| `thematic` | none of the above (default) | uniform, with title × 0.5 |

Intent classification is deliberately heuristic. The regex-based
classifier handles every shape we've encountered in the eval set; if
future failures concentrate on shapes the regex can't cover, this is
the natural place to invest.

## Stage 2 — Retrieve (`query/retrieve.py`)

Input: `QueryAnalysis` + optional `query_vec` (1536d) + optional
`source_filter` (`all`/`door43`/`aquifer`). Output: top-K `Hit` list.

### The six retrievers

Each returns a `list[Hit(chunk_id, score, retrievers)]`.

| # | Retriever | What it does | Best for |
|---|---|---|---|
| 1 | `fts_search` | FTS5 MATCH on `chunks.body` (porter stemming) | keyword presence in body text |
| 2 | `title_search` | FTS5 MATCH on `documents.title` | entity / term lookups |
| 3 | `passage_search` | range overlap on `passage_refs` | "what's at Titus 1:1?" |
| 4 | `scripture_search` | dual-pass (vec + FTS) within `kind:scripture` chunks in passage range | scripture-specific recall when commentary out-ranks verses |
| 5 | `tag_search` | exact tag match (`term:<x>`) | term article hits from analyzer's entity extraction |
| 6 | `vector_search` | sqlite-vec ANN on `chunks_vec` (cosine, 1536d) | semantic recall, paraphrased queries |

#### scripture_search dual-pass

The trickiest retriever, exists for one specific reason: when a query
implies a passage but uses thematic vocabulary (*"What qualifications
must church leaders have according to Titus?"*), `text-embedding-3-small`
ranks greeting verses above the qualifications verses because the
greeting uses the user's vocabulary directly. Two passes compensate:

1. **Vec pass** — `vec` over scripture chunks, ranked by query embedding
   similarity. Catches semantic matches.
2. **FTS pass** — `chunks_fts` MATCH `fts_query` over scripture chunks.
   Catches literal-word matches (e.g., ULT 1:7's "must be blameless").

Both passes feed RRF, so a verse winning either signal lands in the
fused top-K.

#### Filter semantics: hard vs soft

A `passages` list with **narrow** ranges (any pair under 999 verses
wide) acts as a **hard `doc_filter`** for FTS and vec. A pure
**whole-book** range is a **soft hint**: it drives `scripture_search`
and `passage_search` but does *not* exclude content from FTS / vec.

This is the single most important fix learned in eval-tuning.
Whole-book filters were too aggressive — they'd exclude TW articles
inheriting cross-book passages, breaking cross-book theme queries.
Soft hints lift relevant scripture without smothering everything else.

### RRF fusion

Reciprocal Rank Fusion combines ranked outputs. The formula:

```
score(chunk) = Σ over retrievers  weight_r / (k + rank_r(chunk))
              k = 60 (constant)
```

A chunk that appears in multiple retrievers' top results compounds its
score. Per-intent weights tilt fusion without zeroing any retriever:

```python
_INTENT_WEIGHTS = {
    "thematic":         [fts:1.0, title:0.5, passage:1.0, scripture:1.0, tag:1.0, vec:1.0],
    "entity_lookup":    [fts:1.0, title:2.5, passage:0.8, scripture:0.8, tag:1.5, vec:1.0],
    "passage_specific": [fts:1.0, title:0.6, passage:1.2, scripture:1.5, tag:1.0, vec:1.0],
    "passage_book":     [fts:1.0, title:0.6, passage:1.1, scripture:1.4, tag:1.0, vec:1.0],
    "methodology":      [fts:1.0, title:1.5, passage:1.0, scripture:0.8, tag:1.0, vec:1.2],
}
```

Tilts always preserve every retriever's contribution (no zeros). This
keeps composability — wrong intent → still useful retrieval, just
slightly off-optimum.

### Source filter

`source_filter='door43'` and `source_filter='aquifer'` produce comparable
runs by intersecting `doc_filter` with a source-shaped doc-id set.
Defined in `_docs_by_source()`. Diagnostic value: lets you isolate
regressions when corpus scope changes.

## Stage 3 — Synthesize (`query/synthesize.py`)

Input: question + list of `CitationCard` + optional `db`. Output:
`{answer, citations, confidence, raw}`.

### Why the synthesizer takes `db`

`CitationCard.excerpt` is a **240-char display preview** — what you
show alongside a citation in the UI. The synthesizer needs **full
chunk bodies** in the LLM prompt, otherwise the model can't see content
past the excerpt boundary. (Translator notes routinely bury the
operative word past 240 chars.) The synthesizer queries `chunks.body`
directly for full text, capped at `MAX_BODY_CHARS = 1500` per chunk.

### Prompt structure

```
SYSTEM:                                   query/synthesize.py:SYSTEM_PROMPT
  - Describes source types (ULT, UST, TN, TQ, TW, TA, TWL)
  - States: TQ rows ARE answers (not meta)
  - Hard rules: use only sources, every claim cites a [chunk_id],
                refuse with the exact instructed phrase if sources
                don't speak to the question
  - JSON-only response shape

USER:                                     query/synthesize.py:USER_TEMPLATE
  SOURCES:
  [chunk_id_1] document title — passage
    full body up to 1500 chars

  [chunk_id_2] ...

  QUESTION: <user's question>
```

### LLM call (`query/llm.py`)

Groq primary → OpenAI fallback. Both via the `openai` SDK pointed at
each provider's OpenAI-compatible endpoint:

- `GROQ_API_KEY` + `https://api.groq.com/openai/v1` → `llama-3.3-70b-versatile`
- `OPENAI_API_KEY` (default endpoint) → `gpt-4o-mini`

Fallback triggers: `RateLimitError`, `APIConnectionError`, `APIStatusError`
with status ≥ 500 or status == 429. Other 4xx errors propagate (those
indicate client-side bugs, not provider outages).

API keys are stripped of whitespace and validated as ASCII before use —
catches the common copy-paste artifact where a non-breaking space or
smart quote sneaks into `.env` and fails opaquely deep inside `httpx`.

### Citation validation

After the LLM responds, citations are validated:

1. Parse JSON. On invalid JSON, return raw text + low confidence.
2. Extract inline `[chunk_id]` markers from `answer` text.
3. Combine inline + explicit `citations` list.
4. **Drop any chunk_id NOT in the retrieved set.** A hallucinated
   chunk_id is silently removed from both the citations list and the
   answer text. The system cannot fabricate sources.
5. Re-emit `{answer, citations, confidence, raw}`.

### "No answer" path

Two distinct failure modes:

1. **Empty cards** (retrieval found nothing) — bypass the LLM, return
   the canonical refusal phrase directly:
   ```
   "I don't see an answer to that in the indexed sources."
   ```
2. **LLM-judged refusal** (sources retrieved, LLM concluded none answer
   the question) — LLM emits the same canonical phrase per the system
   prompt. The eval's refusal regex matches this exact phrase.

This shared phrase is the trust contract: when the system can't
answer, both paths say so the same way.

## CLI (`query/ask.py`)

```
python -m query.ask "free-form question"
python -m query.ask --no-llm "..."         # retrieval only, no synthesis
python -m query.ask --no-vec "..."         # skip vector retriever
python -m query.ask --source door43 "..."  # one source only
python -m query.ask --json "..."           # raw JSON output
python -m query.ask --top-k 15 "..."       # override retrieval count (default 10)
```

Default `top_k = 10`. With `text-embedding-3-small` ranking noise, this
gives the LLM enough breadth to see relevant chunks at ranks 6-10
that vec alone would miss at the top.

## Failure modes worth knowing

| Failure | Cause | Visible as |
|---|---|---|
| 0 hits returned | passage filter intersected with source filter is empty | empty `cards` → canonical refusal |
| Vector retrieval skipped | OpenAI key missing or `chunks_vec` empty | only `fts` / `passage` etc. retrievers fire — relevant for `--no-vec` and pre-embed states |
| FTS5 syntax error | user query contains reserved chars | retriever returns `[]` and prints warning; other retrievers still run |
| LLM rate-limited | Groq free-tier limits | latency spike (10-30s) due to OpenAI fallback retry |
| Inline citation dropped | LLM cited a chunk_id not in prompt | silent strip — prevents hallucinated sources from rendering |
| sqlite-vec not loaded | `pip install sqlite-vec` missing or system SQLite lacks extensions | warning printed at `open_db`, vec retrieval disabled |

## Tuning knobs in priority order

When eval results regress, adjust in this order:

1. **`MAX_BODY_CHARS`** (synthesize.py) — controls per-chunk LLM context budget.
2. **`TOP_K`** (eval/run.py) and `--top-k` (ask.py) — number of chunks
   sent to the LLM.
3. **`_INTENT_WEIGHTS`** (retrieve.py) — per-intent retriever balance.
4. **`scripture_search` `limit`** parameter — number of scripture
   candidates per pass.
5. **Synthesis prompt** (`SYSTEM_PROMPT`) — how the LLM interprets sources.

Avoid the temptation to bolt on retrievers when the issue is at the
prompt or the weights.
