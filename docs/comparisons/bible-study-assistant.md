# Comparison: bible-study-assistant

A side-by-side architectural read of `examples/bible-study-assistant/` against
this project. The two share the same skeleton (one LLM call per query, at
synthesis time, after deterministic retrieval) but diverge sharply on
scope, storage, and the determinism guarantees they put around the LLM.

## At a glance

| | **bt-docker-mcp (this project)** | **bible-study-assistant** |
|---|---|---|
| Audience | Bible translators (Door43 toolchain) | "Serious Bible study" + translators, web users |
| Corpus scope | English; Titus + Ruth; 1,311 chunks; ~10 MB | en/fr/id; 1,100+ BibleProject docs + translation helps; ~175 MB |
| Index store | Single SQLite file (FTS5 + sqlite-vec) | ChromaDB (vector-only, multiple collections) + JSON for Bible text |
| Embeddings | OpenAI `text-embedding-3-small` (1536d) | OpenAI `text-embedding-ada-002` (legacy) |
| Retrievers | 6 channels (FTS, title, passage, scripture, tag, vec) → RRF fusion → top-K | regex Bible-ref → BibleService JSON; vector across all Chroma collections; cross-refs |
| Citation property | Cited chunk_ids validated against retrieved set; hallucinated ones dropped | Retrieved resources passed alongside response; **no validation that response sentences trace to retrieved text** |
| LLM strategy | Groq Llama 3.3 70B → OpenAI gpt-4o-mini fallback | OpenAI only; gpt-4o (complex) vs gpt-4o-mini (simple) heuristic; no fallback |
| Conversation state | None — every query independent | Sessions per user, last 10 messages injected into prompt |
| Cost surfacing | None per response | Per-response `cost_usd` + token counts |
| Surfaces | CLI + REST `/api/*` + MCP (HTTP `/mcp` + stdio) | REST `/chat` + static HTML web UI; **no MCP** |
| Auth / rate limit | `BTMCP_API_PASSWORD` gate + slowapi per-IP | Not configured |
| Deploy target | Railway (volume + Dockerfile) | fly.io (Chainguard wolfi-base, nonroot) |
| Freshness | `indexer.refresh` cron module (off until enabled) | Manual re-ingest (PDF scrape → chunk → load) |

Reference points in their code: orchestration in
`bs_assistant/services/chat_service.py`; retrieval in
`bs_assistant/core/rag/retriever.py`; LLM client in
`bs_assistant/core/llm/client.py`.

## Three things worth borrowing

### 1. Multi-strategy chunking on long-form content

For a single BibleProject PDF, they generate three parallel chunk sets
(timestamp / Bible-reference / semantic) and store them as siblings in
the vector index. Different question shapes hit different chunk types —
"what's the timestamp where Tim talks about X?" wants timestamp chunks;
"what passages back up Y?" wants Bible-reference chunks.

Slot for us: when the corpus expands beyond Titus + Ruth into long-form
study notes (e.g. Aquifer commentaries, Tyndale dictionary articles),
this beats one-size-fits-all chunking. It fits *underneath* our existing
schema — same `chunks` table, same chunk_id system, just more chunks per
source doc.

### 2. Per-response cost surfacing

Every `POST /chat` response in their system carries `cost_usd`,
`tokens_used`, `model_used`. Returning the same shape from `/api/ask`
costs us nothing, lets the frontend show users (and us) per-query spend
without an external dashboard, and makes Groq-vs-OpenAI fallback events
visible from the response alone.

### 3. Two-tier model heuristic

They route simple lookups to gpt-4o-mini and complex questions to gpt-4o
based on message-length / pattern heuristics. Our Groq Llama 3.3 70B
primary is already cheaper than gpt-4o-mini, so this matters less for
us today. Worth filing under "if Groq goes away" — the *idea* of
intent-aware model selection is portable.

## Three "conflicts" — and where I was wrong

The first read flagged three of their patterns as conflicting with our
load-bearing invariants. After the design conversation, the picture is
more nuanced. Recording the refined take here so future-self doesn't
re-walk the wrong path.

### 1. Citation validation: prompt tweak ≠ deterministic provenance

**Their pattern.** LLM gets retrieved chunks in the system prompt and
generates free prose. Nothing enforces that claims in the answer trace
to a specific retrieved chunk.

**Our pattern.** `query/synthesize.py` parses cited chunk_ids out of the
LLM output and **drops any not in the retrieved set** before the answer
ships to the user.

**Refined view.** "Just prompt the LLM to cite sources" *almost* gets
their codebase to our property — but not all the way. Three pieces are
load-bearing:
1. **Stable chunk_ids** (they have them — Chroma has IDs).
2. **Pass chunk_ids in the prompt as the cite-from set.** (Prompt change.)
3. **Parse citations out of the answer and validate each against the
   retrieved set.** (~30 lines of code, not a prompt change.)

Without (3) the LLM can hallucinate a citation, smuggle in
training-data claims under a real citation, or paraphrase without
citing. The validation pass is the load-bearing piece.

So this isn't a deep architectural conflict — it's a small mechanical
gap. They could close it; we have it closed.

### 2. Conversation history: only conflicts if it enters the synthesis prompt

**Their pattern.** `CONVERSATION_HISTORY_MAX_MESSAGES=10` injects the
last ten turns into the LLM prompt. Same question, different history,
different answer.

**Our pattern.** Each `/api/ask` is independent. Citation stability is
trivial because the prompt is reproducible from the question alone.

**Refined view.** The conflict is not "history" — it's "history in the
synthesis prompt." Two clean ways to add session UX without breaking
determinism:

- **Server-side session table** keyed by user_id, storing
  `(question, answer, citation_chunk_ids)`. Used for sidebar / recent
  questions / "did you also want to know..." suggestions. The synthesis
  prompt for question N still sees only question N.
- **Suggestion generation** can be deterministic templates ("Show me TIT
  1:6 in UST" given a recent passage) or a separate, cheap LLM call
  whose output isn't part of the authoritative answer.

The line we hold: each `/api/ask` is independent and citations on it
are validated against that call's retrieved set only. Everything else
is fair game.

### 3. Multi-collection retrieval: not a hybrid opportunity — we already are the hybrid

**Their pattern.** One Chroma collection per content type. At query
time, loop over all collections, score by cosine similarity, take the
top results.

**Our pattern.** One `chunks` table tagged with `kind:scripture |
translator-note | term | ...`. Six retrieval channels (some
deterministic — FTS, passage-overlap, exact tag match — some semantic),
fused via intent-weighted RRF.

**Refined view.** Their "query all collections" pattern is not a
positive design choice we'd hybridize with; it's a workaround for not
having a tag/taxonomy layer. We solved the same problem differently and
better.

When we expand the corpus (e.g. BibleProject videos):
- New `kind:video-transcript` tag, sits in the same `chunks` table with
  the same chunk_id system.
- Possibly a new RRF channel like `transcript_search` if the structure
  (timestamps, video refs) deserves a specialized retriever — same
  pattern as our existing `scripture_search` dual-pass.
- Citations stay deterministic because chunk_ids stay stable.

The genuine borrow from this section is **multi-strategy chunking on
long-form content** (see "Three things worth borrowing" #1). That
extends our schema; it doesn't replace our retrieval architecture.

## Net characterization

They're optimizing for **breadth and conversational UX over a larger
corpus**. We're optimizing for **auditable provenance and
translator-grade citations over a deliberately scoped corpus**. Same
architectural skeleton; very different design weight on each invariant.
