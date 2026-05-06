# bt-docker-mcp

Local-first retrieval-augmented Q&A over Bible translation resources.

Users ask **free-form questions in natural language**. The system retrieves the
relevant passages from indexed sources (Door43 unfoldingWord catalog and
BibleAquifer content), generates an answer with a small LLM, and returns the
answer with **inline citations** linking back to the exact source location of
every claim. Users never need to know about indexes, tags, or passage refs;
they just ask, the system points them to the source.

## Architecture

Two layers — Layer 2 is the user-facing surface; Layer 1 is internal machinery.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 2 — Query pipeline (user-facing)                              │
│                                                                      │
│   free-form question                                                 │
│           │                                                          │
│     ┌─────▼──────┐                                                   │
│     │  analyzer  │  extracts passage refs / tags / free text        │
│     └─────┬──────┘                                                   │
│           │                                                          │
│     ┌─────▼─────────────────────────┐                                │
│     │  hybrid retrieval (parallel)  │                                │
│     │   • FTS5 over chunk bodies    │  ← filters narrow candidates   │
│     │   • passage range overlap     │                                │
│     │   • tag exact-match           │                                │
│     │   • vector ANN (v2 — sqlite-vec)                               │
│     └─────┬─────────────────────────┘                                │
│           │ Reciprocal Rank Fusion → top-K                           │
│     ┌─────▼──────┐                                                   │
│     │ synthesize │  LLM (Groq Llama 3.3 70B → OpenAI gpt-4o-mini)    │
│     │            │  constrained to cite only retrieved chunk_ids     │
│     └─────┬──────┘                                                   │
│           │                                                          │
│     ┌─────▼────────┐                                                 │
│     │ inline       │  "answer text [1] [2]"                          │
│     │ response     │  + citation cards: title, passage, source       │
│     └──────────────┘                                                 │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 1 — Index (one SQLite file: indexer/index.db)                 │
│                                                                      │
│   • documents       — one row per source unit (verse, note, …)       │
│   • chunks          — addressable text units (embedding-ready)       │
│   • chunks_fts      — FTS5 over chunks.body (porter stemming)        │
│   • chunks_vec      — sqlite-vec vector column (v2)                  │
│   • passage_refs    — encoded BBCCCVVV range overlap                 │
│   • tags            — exact-match metadata (resource, term, entity)  │
│   • meta            — schema version, source SHAs, embedding model   │
└──────────────────────────────────────────────────────────────────────┘
```

## Repo layout

| Path             | Purpose                                                                |
| ---------------- | ---------------------------------------------------------------------- |
| `indexer/`       | Layer 1 — schema, build/query helpers, references + citations         |
| `ingest/`        | Source-specific normalizers (Door43, Aquifer) → `Document` objects     |
| `query/`         | Layer 2 — analyzer, retrieve, synthesize, `ask` CLI                    |
| `tools/`         | Standalone diagnostic scripts (e.g. `map_data_sources.py`)             |
| `original-repo/` | The original `bt-servant-worker` Cloudflare Worker — kept for reference|

## Quick start

```bash
# 1. install
pip install -r indexer/requirements.txt
pip install -r ingest/requirements.txt
pip install -r query/requirements.txt

# 2. ingest English Titus from Door43 (ULT, UST, TN)
python3 -m ingest.cli --source door43 --book TIT --lang en

# 3. build the index from what was ingested
python3 -m indexer.build --source ingest/_staging --reset

# 4. ask
export GROQ_API_KEY=...           # primary LLM
export OPENAI_API_KEY=...         # fallback
python3 -m query.ask "what does Titus 1:1 say about being a servant of God?"
```

Output:

```
Paul identifies himself as a servant of God [1], using a term that connects to
the Old Testament prophets called servants of Yahweh [2]. The notes explain
that "servant" here means one who is wholly devoted to God's purposes [1].

[1] Translation Notes — Titus 1:1
    en_tn TIT 1:1 — "Paul, a servant of God…"
[2] unfoldingWord Literal Text — Titus 1:1
    en_ult TIT 1:1 — "Paul, a servant of God, and an apostle of Jesus Christ…"
```

## Design principles

1. **The user types prose; the system finds the source.** Free-form queries are
   the only public interface. Tags, passage encodings, chunk IDs are internal.
2. **Every claim is cited, and the provenance chain is auditable.** The
   generative LLM runs exactly once per query, only at the synthesis step.
   Everything before it (analyzer, retrievers, fusion, citation resolution)
   is deterministic regex + SQL — verifiable byte-for-byte. The LLM's only
   job is to compose an answer from sources that *deterministically reached
   the prompt*; downstream validation drops any cited chunk_id not in the
   retrieved set before render. A hallucinated source can't survive into
   the answer. See [docs/architecture.md](docs/architecture.md#why-the-generative-llm-is-only-at-the-end).
3. **"No answer" beats a confident wrong one.** When retrieval returns nothing
   above a similarity floor, the system says so — and points to the closest
   thing it found rather than inventing.
4. **Hybrid retrieval > pure vector.** Free-form doesn't mean "throw it at an
   embedding." The analyzer extracts structure (passage refs, entities) and
   uses it to pre-filter candidates. Vector ANN, FTS5, and structured filters
   compose, not compete.
5. **One file = one index.** Structured tables, FTS5, and (in v2) vectors live
   in a single SQLite file. Backup is `cp`, deploy is ship-the-file, A/B is
   swap-the-file.

## Phased rollout

| Phase  | Adds                                                                     |
| ------ | ------------------------------------------------------------------------ |
| **v1** | regex query analyzer · FTS5 + structured fusion · LLM synthesis · CLI   |
| **v2** | vector index (sqlite-vec) · RRF over FTS+vector+structured              |
| **v2.x** | content-shape `kind:*` tagging · title-FTS retriever · intent-weighted RRF · dual-pass scripture_search · per-source filter · eval framework |

## Sources

- **Door43 / unfoldingWord** — public Gitea-hosted catalog at
  `git.door43.org`. Resources: ULT (Literal Text), UST (Simplified Text),
  TN (Translation Notes), TQ (Translation Questions), TWL (Translation Word
  Links), TW (Translation Words), TA (Translation Academy). Licensed
  CC BY-SA 4.0.
- **BibleAquifer** — public GitHub org at `github.com/BibleAquifer`.
  Article-granular content with ACAI entity associations. Licensed per-repo.

v1 ingests **English** resources for **Titus only** (ULT, UST, TN). Other books
and resource types come online incrementally; the ingest pipeline is built so
adding a book is a CLI flag, not a code change.

## Deployment

Designed to run as a single Docker container on **fly.io** or **Railway**.
The whole index is one file (`/data/index.db` on the mounted volume). See
`Dockerfile` for the image and `docs/deploy.md` for fly.io / Railway notes
(when v2 lands with the HTTP layer).

## License

Code: MIT. Indexed content retains its source license (Door43 CC BY-SA 4.0,
Aquifer per-repo).
