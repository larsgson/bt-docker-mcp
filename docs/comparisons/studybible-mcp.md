# Comparison: studybible-mcp

`examples/studybible-mcp/` is a different *kind* of system than ours, not
a different RAG. It answers a different question: not "how do we orchestrate
the LLM around retrieval" but "where does the LLM live at all?" Their
answer: not on the server.

This doc captures the architectural read plus the planning conversation
that followed, where we worked out what to borrow and what would conflict
with our load-bearing invariants. The actual implementation plan that
emerged from this conversation lives in
[`expansion-plan.md`](../expansion-plan.md).

## At a glance

| | **bt-docker-mcp (this project)** | **studybible-mcp** |
|---|---|---|
| What it is | RAG service: deterministic retrieval + server-side LLM synthesis | Pure-tool MCP server. **Zero LLM calls server-side.** |
| LLM call site | Server: 1× per query at synthesis (Groq → OpenAI fallback) | Client (Claude / Cursor / etc.). Server has no API keys. |
| Index | SQLite + sqlite-vec, single file, ~10 MB, ~1.3k chunks | SQLite + sqlite-vec, single file, **~600 MB, full Bible + 18 reference layers** |
| Corpus | Door43 + Aquifer; Titus + Ruth; English | Greek + Hebrew lexicons (LSJ, Abbott-Smith, BDB); 31,280 verses with morphology; 5,290 passage clusters; 4,299 ACAI named entities; 102k Aquifer notes; 500+ Tyndale dictionary articles; 200+ FIA Key Terms; 87 ANE entries × 12 dimensions × 314 book-chapter mappings; 1,100+ genealogical persons (Theographic); Heiser content; Torah Weave structural data |
| Embeddings | OpenAI `text-embedding-3-small` (1536d), used as a *primary retrieval signal* | Same model, but **only for one tool** (`find_similar_passages`) — semantic discovery, not core retrieval |
| Retrieval surface | One internal pipeline: analyzer → 6 retrievers → RRF → synthesize | **18 independent MCP tools** the client orchestrates: `lookup_verse`, `word_study`, `search_lexicon`, `parse_morphology`, `search_by_strongs`, `lookup_name`, `get_cross_references`, `explore_genealogy`, `get_ane_context`, `find_similar_passages`, … |
| Synthesis owner | Server (`query/synthesize.py`) | **Client.** Server emits structured tool results; Claude composes the answer |
| Citation property | LLM-generated citations validated against retrieved set; hallucinated chunk_ids dropped | Citations are *Claude's*; trust delegated to the client LLM. Tool outputs are deterministic, but Claude's prose isn't validated. |
| Hermeneutic guidance | None (we leave this to the user) | **Embedded in `prompts/system_prompt.md`** — Fee & Stuart genre-specific exegesis, ANE cognitive defaults; ships with the project as the recommended Claude system prompt |
| Surfaces | CLI + REST `/api/*` + MCP (`/mcp` HTTP + stdio) | MCP only (stdio + SSE legacy + Streamable HTTP `/mcp`). No standalone CLI. |
| Auth | `BTMCP_API_PASSWORD` gate on AI-using paths | None |
| Rate limit | slowapi per-IP, configurable | In-memory sliding window, 100 req/min hardcoded |
| Freshness | `indexer.refresh` cron module (off until enabled); live ingest from upstream | **Static snapshot** — `study_bible.db` built once, downloaded as a 600 MB file. Manual rebuild + redeploy. |
| Distribution | Docker image + index on volume | Same — plus they expose `GET /download/study_bible.db` for self-hosters |
| Deploy target | Railway | Fly.io |

Code reference points: server entry `src/study_bible_mcp/server.py:1318`;
tool catalog `src/study_bible_mcp/tools.py`; system prompt
`prompts/system_prompt.md`; their own architecture write-up
`ARCHITECTURE.md`.

## The single biggest contrast

We encapsulate retrieval **and** synthesis. They encapsulate retrieval
only and push synthesis to the calling LLM.

This is a different architectural philosophy:
- **Ours.** Server owns the LLM call. The MCP/REST consumer gets a
  finished, citation-validated answer.
- **Theirs.** Server has no LLM. The MCP consumer (which *is* an LLM)
  does the synthesis itself, deciding which tools to chain and how to
  compose.

These aren't strictly comparable as "better/worse" — they answer different
audiences:
- Their MCP is *consumed only by an LLM agent.* Claude chooses 1–N tool
  calls per turn and synthesizes from the structured results.
- Our MCP is also LLM-consumed but additionally fronts a REST API that
  serves a **non-LLM frontend** (the planned Netlify client). The
  frontend can't synthesize; it needs `/api/ask` to return finished
  prose.

We *can't* adopt their "no server-side LLM" model without losing the
REST `/api/ask` use case. They *can't* adopt our "synthesis on the
server" model without forcing their MCP consumers through a
double-LLM round-trip.

This axis is already documented as our position — see
[`architecture.md`](../architecture.md) "Why the generative LLM is only
at the end" and [`mcp.md`](../mcp.md) "MCP convention: no model calls
in the default path." Their MCP follows the same convention as our MCP
defaults; the difference is they took it all the way and removed the
synthesis layer entirely.

## Things worth borrowing

### 1. Hermeneutic framework as system prompt, not as data

`prompts/system_prompt.md` is the load-bearing UX piece. Not facts in
the index — *interpretive guidance for the model*: genre-aware reading
rules, ancient near-east cognitive defaults, citation expectations,
when to use which tool. They ship it as the canonical Claude system
prompt for their MCP.

For us: the equivalent slot is `query/synthesize.py`'s system message.
We could lift the structure of theirs (genre-specific guidance, "say
'no answer' before guessing", explicit no-paraphrase-without-citation
rule) into our synthesis prompt with no architectural change.

### 2. Graph data as queryable structure, not text

Genealogy, events, places, name relationships are MCP tools
(`explore_genealogy`, `lookup_name`, shortest-path-between-people)
backed by graph tables — not chunks. This makes "is Boaz an ancestor
of David?" a deterministic graph traversal, not a retrieval problem.

We can't fit relational graph data into our chunks-and-tags shape.
That's fine — it's a candidate for a parallel `entities` +
`entity_relations` schema sitting alongside `chunks`. Their schema is a
workable reference for that.

### 3. Vector embeddings reframed as discovery, not retrieval

For us, vector ANN is one of six channels in the RRF-fused primary
retriever. For them, it's the source for a single dedicated tool
(`find_similar_passages`) — explicitly framed as semantic *discovery*,
not as part of fact-grounded retrieval.

A `find_similar_chunks` tool exposed alongside our existing `search`
and `passage_lookup` is a thin layer on top of existing infra: same
embedding pipeline, different framing (paraphrased-query rescue),
different cost expectation.

### 4. Public database download as a distribution channel

`GET /download/study_bible.db` gives anyone the entire index as a
single file. Self-hosters can stand up an MCP locally with no rebuild
step. For our 10 MB index this is even cheaper to ship — a periodic
export to a public CDN URL (or a release artifact) would let users run
a fully local stdio MCP without running our ingest pipeline. Doesn't
pressure any invariant; purely distribution.

## "Conflicts" with our load-bearing invariants — refined view

The first read flagged three patterns of theirs as conflicting. After
the planning conversation, the picture is more nuanced.

### 1. Zero server-side LLM

**Their pattern.** Server has no API keys, no LLM calls. Claude
synthesizes from tool results.

**Why this looks like a conflict.** Adopting their model would mean
removing `query/synthesize.py` and `/api/ask`, leaving us as a pure-tool
MCP/REST surface. That breaks the planned Netlify client (which is not
an LLM and can't synthesize).

**Refined view.** This isn't actually a tension we have to resolve —
nothing's pulling us to remove server-side synthesis. The interesting
question is whether our **MCP surface** could be enriched so that
agentic Claude clients can compose from raw tools without falling back
to `/api/ask` — that's a yes, and it's part of the expansion plan
(more deterministic MCP tools as a parallel surface to the synthesis
endpoint).

### 2. Implicit citation trust

**Their pattern.** Tool outputs are deterministic, so if Claude cites
them, they're real. They don't validate that the prose Claude *writes*
is grounded in the tool outputs it *received*.

**Refined view.** This is not a borrow opportunity; it's a reminder of
*why* our citation validation exists. We keep synthesis on the server →
we own the burden of citation correctness → validator drops cited
provenance ids that didn't reach the prompt. Adopting their corpus +
tools (next section) doesn't relax this; it expands the *kinds* of
provenance ids we validate.

### 3. Static snapshot as the primary distribution model

**Their pattern.** Build once, ship as a 600 MB download. Citations
against tool results are perfectly stable because the data never
changes between releases.

**Refined view.** Their immutability is not the *primary* model we
want — we deliberately optimize for upstream freshness via cron
refresh. But the *option* is a borrow: produce periodic public
snapshots as a *secondary* distribution channel for users who want
immutability over freshness. Our cron refresh remains the primary path.

## The actually-interesting consequence: their corpus + their tool concepts run through our methodology

The biggest finding from the planning conversation isn't on the conflicts
list at all. It's that:

- Their corpus is largely re-derivable from public-domain and
  CC-BY upstream sources (LSJ, Abbott-Smith, BDB, STEPBible, ACAI,
  Theographic, plus the Aquifer/FIA content we already ingest).
- Their 18 tools' query shapes (lookup_verse, word_study, search_lexicon,
  explore_genealogy, get_ane_context, etc.) map cleanly onto our
  retriever architecture — each is one or two new deterministic
  retrievers plus a new analyzer intent class.
- Our citation property (drop hallucinated provenance ids) generalizes
  from chunk_ids to a broader provenance_id pattern that covers
  chunks + entity rows + lexicon entries + graph relations.

Net: we can absorb most of what makes their system useful **without
adopting their architecture** — keeping server-side synthesis,
deterministic citations, and the source-agnostic taxonomy intact. The
expansion plan that came out of this is in
[`expansion-plan.md`](../expansion-plan.md).

## Net characterization

studybible-mcp is **a reference library exposed as MCP tools, with
synthesis pushed entirely to the calling LLM**. We are **a
citation-validating RAG service that also speaks MCP**.

Same data substrate (single-file SQLite + sqlite-vec), overlapping
domain audience, different answers to "where does the LLM live and who
validates its output." Their corpus and tool concepts are largely
borrow-compatible with our methodology; their architecture is not (and
doesn't need to be).
