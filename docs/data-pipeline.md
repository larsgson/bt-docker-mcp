# Data pipeline (Layer 1 + ingest)

How source content becomes a queryable index. Three stages: **fetch**,
**stage**, **build/embed**. The output is one SQLite file (`indexer/index.db`)
that Layer 2 reads from.

## Sources

| Source | Where | License |
|---|---|---|
| **Door43 / unfoldingWord** | `git.door43.org/unfoldingWord/{en_ult, en_ust, en_tn, en_tq, en_twl, en_tw, en_ta}` (Gitea, public) | CC BY-SA 4.0 |
| **BibleAquifer** | `github.com/BibleAquifer/<repo>/<lang>/json/<NN>.content.json` (~62 public repos, multi-language) | per-repo |

## Door43 ingest (`ingest/door43.py`)

Seven resource types, fetched per book, normalized to per-row Documents.

| Resource | URL pattern | Output | Body shape |
|---|---|---|---|
| ULT (Literal Text) | `<repo>/<branch>/<NN>-<BOOK>.usfm` | one verse per Document | clean prose, USFM markup stripped |
| UST (Simplified) | same | one verse per Document | same |
| TN (Translation Notes) | `tn_<BOOK>.tsv` | one note per Document | quote + note text |
| TQ (Translation Questions) | `tq_<BOOK>.tsv` | one Q/A pair per Document | "**Question**: ... \n**Response**: ..." |
| **TWL** | `twl_<BOOK>.tsv` | **NOT emitted** | passage→term metadata only |
| TW (Translation Words) | `bible/{kt,names,other}/<term>.md` | one term article per Document | full markdown body |
| TA (Translation Academy) | `<section>/<module>/01.md` | one module per Document | full markdown body |

### TWL is ingest-only signal

TWL rows aren't emitted as Documents — they're parsed for their cross-
references and discarded. The `tw_refs` map (`{tw_path: [(start, end), ...]}`)
flows into `_ingest_referenced_tw()`, which **inherits passage refs onto
the linked TW articles**. Same pattern for `_ingest_tn()` → TA module
inheritance via `SupportReference`.

This was a deliberate course-correction. TWL Documents had substantive titles
("TWL — Ruth 1:22 → bethlehem") but empty bodies — they were winning
title/FTS retrieval but contributing zero answer-value. Fix from
translation-helps-mcp's pattern: *don't index what you can derive on
demand*. See [decisions.md](decisions.md#TWL-is-ingest-only).

### File numbering quirk (Paratext)

Door43 USFM filenames use Paratext numbering (NT shifted +1 vs. canonical
Protestant — slot 40 reserved for apocrypha). `ingest/door43.py:_PARATEXT_FILE_NUM`
maps:

```python
{code: (n if n <= 39 else n + 1) for code, n in BOOK_NUMBERS.items()}
```

So Titus = canonical 56 = Paratext file 57 = `57-TIT.usfm`. Our internal
BBCCCVVV uses canonical (TIT = 56); only filename construction uses Paratext.

### Cross-resource fetching

`_ingest_referenced_tw()` and `_ingest_referenced_ta()` fetch only the TW
articles + TA modules **referenced from the requested books**, not the whole
catalog. Parallel via `ThreadPoolExecutor(max_workers=8)`. Reduces ~3000-term
TW catalog to ~150 actually-relevant articles.

## Aquifer ingest (`ingest/aquifer.py`)

Different shape: BibleAquifer publishes per-language `<NN>.content.json` files
keyed by canonical book number. Each file is a JSON array of articles with:

```jsonc
{
  "content_id": "25940",
  "title": "Titus 1:1",
  "language": "eng",
  "media_type": "Text",
  "index_reference": "56001001",            // BBCCCVVV
  "content": "<p>HTML body…</p>",
  "associations": {
    "passage": [...],
    "acai": [{"id": "person:Paul", "type": "person"}, ...]
  }
}
```

### Default skip list

Aquifer is treated as **supplementary** to Door43, not a competing primary.
Seven repos are skipped by default in `_SKIP_BY_DEFAULT`:

| Skipped repo | Why |
|---|---|
| `unfoldingWordLiteral` | mirror of Door43 en_ult |
| `unfoldingWordSimplified` | mirror of Door43 en_ust |
| `UWTranslationNotes` | mirror of Door43 en_tn |
| `UWTranslationQuestions` | mirror of Door43 en_tq |
| `BereanStandardBible` | alternative full-Bible translation |
| `WorldEnglishBible` | alternative full-Bible translation |
| `WorldEnglishBibleUpdated` | alternative full-Bible translation |

Override with `--include-skipped` (or `--repos r1 r2 …` for an explicit
allowlist). The decision is documented in
[decisions.md](decisions.md#Aquifer-as-supplementary).

### Per-repo content kind

Aquifer chunks get `kind:<x>` based on a curated `_REPO_KIND` table (~30
known repos mapped to scripture, study-note, translator-note, term, methodology,
book-intro, map, image). Unknown repos default to `kind:study-note`.

### HTML stripping

Article bodies are HTML; stdlib `html.parser.HTMLParser` extracts plain text
preserving block-level boundaries. Falls back to a regex strip on malformed
HTML (`<[^>]+>` → space).

## Staging format

Both ingesters output the same shape — markdown files with YAML frontmatter
under `ingest/_staging/<source>/<resource>/<id>.md`:

```markdown
---
title: "TN — Titus 1:6 (jen8)"
tags:
  - book:TIT
  - kind:translator-note
  - lang:en
  - org:unfoldingWord
  - resource:tn
  - support_ref:rc://*/ta/man/translate/figs-abstractnouns
passages:
  - [56001006, 56001006]
---

**εἴ τίς ἐστιν ἀνέγκλητος**

This is the beginning of the description of the character of an elder.
Paul assumes that Titus understands…
```

This is the universal contract. Any future source can ingest into this
format and the indexer consumes it via the standard `MarkdownAdapter` —
the indexer is source-agnostic.

## SQLite schema

Source of truth: [`indexer/schema.sql`](../indexer/schema.sql). Highlights:

```sql
documents       (id, source_path, source_sha, title, metadata, indexed_at)
chunks          (id, doc_id, chunk_index, body)            FK→docs CASCADE
chunks_fts      VIRTUAL FTS5 over chunks.body              triggers keep in sync
documents_fts   VIRTUAL FTS5 over documents.title          triggers keep in sync
chunks_vec      VIRTUAL vec0 (chunk_id, embedding[1536])   distance_metric=cosine
passage_refs    (doc_id, start_bbcccvvv, end_bbcccvvv)     FK→docs CASCADE
tags            (doc_id, tag)                              FK→docs CASCADE
meta            (key, value)                               schema_version, embedding_model, …
```

### Why two FTS5 virtual tables

`chunks_fts` for body matching ("contains the word *blameless*") and
`documents_fts` for title matching ("title contains *Boaz*"). The latter
is decisive for entity-lookup queries — bodies saturate with noise on common
proper nouns, but titles are short and term-focused.

### Why a separate `chunks_vec`

`sqlite-vec`'s `vec0` virtual table type. Stored alongside the structured
tables in the same SQLite file. Cosine distance metric. 1536-dim vectors
for `text-embedding-3-small`. One file = entire knowledge base.

### Triggers

- `documents_ai/ad/au` keep `documents_fts` in sync with `documents`.
- `chunks_ai/ad/au` keep `chunks_fts` in sync with `chunks`.
- No trigger for `chunks_vec` — embeddings are written by `indexer/embed.py`
  which is a separate pass (because OpenAI embedding calls cost money;
  we don't want them firing on every doc insert).

### Tag conventions

| Prefix | Meaning | Set by |
|---|---|---|
| `kind:<x>` | content shape (scripture / translator-note / question / term / methodology / study-note / book-intro / map / image / link) | both ingesters |
| `book:<CODE>` | USFM book code | ingest (incl. inherited from passages) |
| `lang:<x>` | language | ingest |
| `resource:<x>` | resource type identifier | ingest |
| `org:<x>` | source org | ingest |
| `term:<id>` | TW term identifier | Door43 TW ingest |
| `support_ref:<…>` | TA module reference from TN | Door43 TN ingest |
| `aquifer:<RepoName>` | Aquifer repo of origin | Aquifer ingest |
| `acai:<entity_id>` | ACAI entity association | Aquifer ingest |
| `category:<x>` | TW article category (kt / names / other) | Door43 TW ingest |
| `section:<x>` / `module:<x>` | TA module location | Door43 TA ingest |
| `tw_ref:<path>` | TW article path linked from TWL | (was used; TWL no longer emits Documents) |

The `kind:*` namespace is the **source-agnostic** content classification —
the eval and the analyzer query against `kind:scripture` / `kind:term` /
`kind:methodology` regardless of which provider supplied the chunk.

## Build (`indexer/build.py`)

```
python3 -m indexer.build --source ingest/_staging [--reset]
```

What it does:
1. Open `index.db` (creates if missing) with sqlite-vec extension loaded.
2. Apply `schema.sql` (`CREATE … IF NOT EXISTS` — idempotent).
3. Walk staging dir for `*.md`. For each:
   - `MarkdownAdapter.parse(path, root)` → `Document`
   - `DELETE FROM documents WHERE id = doc.id` (cascades chunks + tags + passages; FTS triggers fire to remove from `chunks_fts` / `documents_fts`)
   - `INSERT INTO documents` (triggers populate `documents_fts`)
   - `INSERT INTO chunks` (triggers populate `chunks_fts`)
   - `INSERT INTO passage_refs`, `tags`
4. Update `meta.indexed_at` and `meta.source_root`.
5. **Cleanup orphans**: `DELETE FROM chunks_vec WHERE chunk_id NOT IN (SELECT id FROM chunks)`. Removes vectors for chunks that no longer exist.
6. **Backfill `documents_fts`** if it was newly created on a pre-existing index (idempotent rebuild via `INSERT INTO documents_fts(documents_fts) VALUES('rebuild')`).

`--reset` deletes the DB file first; otherwise the build is incremental
and idempotent.

## Embed (`indexer/embed.py`)

```
python3 -m indexer.embed [--reset-vec]
```

What it does:
1. Open the DB with sqlite-vec loaded.
2. `ensure_vec_table(db)` — creates `chunks_vec` if absent.
3. Check `meta.embedding_model` — if it differs from `BTMCP_EMBEDDING_MODEL`, refuse with a clear error pointing to `--reset-vec`. Prevents silently mixing vectors from different models.
4. Find chunks with no `chunks_vec` row:
   ```sql
   SELECT chunks.id, chunks.body
   FROM chunks
   LEFT JOIN chunks_vec ON chunks_vec.chunk_id = chunks.id
   WHERE chunks_vec.chunk_id IS NULL
   ```
5. Batch (100 per call) → OpenAI `embeddings.create(model=text-embedding-3-small, input=batch)`.
6. Pack via `sqlite_vec.serialize_float32(vec)` → `INSERT INTO chunks_vec(chunk_id, embedding)`.
7. Record `embedding_model` and `embedding_dim` in `meta`.

**Idempotent**: re-running with the same model is a no-op when nothing's
new. After a content rebuild that didn't change bodies, this step adds
zero new vectors.

## Re-running the pipeline

| Scenario | Command |
|---|---|
| Same source, just re-run | `python -m indexer.build --source ingest/_staging` |
| Source content changed | re-run `ingest.cli`, then `indexer.build`, then `indexer.embed` |
| Add a book | re-run `ingest.cli --book NEW`, then build + embed (only the new chunks get embedded) |
| Switch embedding models | `python -m indexer.embed --reset-vec` (drops `chunks_vec`, re-embeds everything) |
| Full rebuild | `rm indexer/index.db && python -m indexer.build --source ingest/_staging --reset && python -m indexer.embed` |

## Costs

For Titus + Ruth, English, Door43 + 7 supplementary Aquifer repos:

- **~1,300 chunks** in the index
- **~$0.005** per full embed (OpenAI `text-embedding-3-small` @ $0.02 / 1M tokens; ~250K total tokens for 1,300 chunks of ~200 tokens each)
- **~10 MB** SQLite file
- **~30 seconds** end-to-end ingest + build + embed (cold)

Scaling to all 66 books, all resources, multiple languages would multiply
chunks by maybe 50× → ~65K chunks → ~$0.25 to embed → ~500 MB file.
Well within sqlite-vec's comfort zone.
