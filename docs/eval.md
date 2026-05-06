# Eval framework

How retrieval and synthesis quality is measured. Hand-curated cases →
runner produces metrics → JSON output for diff/regression tracking.

## Layout

```
eval/
├── set/v1.yaml          curated cases (12 in v1)
├── run.py               runner (CLI)
├── __init__.py
└── runs/
    ├── latest.json      always overwritten with most recent run
    └── <UTC>.json       per-run snapshot (gitignored)
```

`eval/runs/*.json` is gitignored — runs are derivable by replay.

## Eval set format (`eval/set/v1.yaml`)

```yaml
version: 1
cases:
  - id: tit_1_1_servant
    tags: [direct-passage, single-book]
    question: "What does Titus 1:1 say about being a servant of God?"
    expects:
      passages: ["Titus 1:1"]
      tags: [kind:scripture]

  - id: refusal_quantum
    tags: [refusal, off-topic]
    question: "What does Titus say about quantum mechanics?"
    expects:
      refusal: true
```

## Expectation grammar

| Field | Type | What it checks |
|---|---|---|
| `passages` | list of human refs | At least one retrieved chunk's passage_refs overlaps each. Refs are parsed via `indexer.references.parse_references` → BBCCCVVV pairs. |
| `resources` | list of short names | Shorthand for `tags: [resource:<x>]`. At least one chunk per tag must be in retrieved set. |
| `tags` | list of full tag strings | Same coverage check. Any tag namespace works (`kind:*`, `book:*`, `term:*`, …). |
| `substrings` | list of strings | Each must appear (case-insensitive) in the synthesized answer. Skipped under `--no-llm`. |
| `refusal` | bool | true → answer must contain the canonical refusal phrase. unset/false → answer must NOT be a refusal. |

A case passes when **all** declared expectations are satisfied. Anything
not declared isn't checked — start with a passage requirement, layer
on resources/substrings as you tighten.

## Running

```bash
python -m eval.run                        # full pipeline
python -m eval.run --source door43        # restrict to one source
python -m eval.run --no-llm               # retrieval only (no synthesis cost)
python -m eval.run --no-vec               # no vector retrieval (no OPENAI key needed)
python -m eval.run --ids id1 id2          # subset
python -m eval.run --top-k 15             # override retrieval count
```

Exit code is **0 only if every case passes** — useful for CI later.

## Output (`eval/runs/<UTC>.json`)

Top-level shape:

```jsonc
{
  "ran_at": "2026-05-06T12:34:56Z",
  "duration_s": 60.6,
  "config": {
    "eval_set": "eval/set/v1.yaml",
    "no_llm": false,
    "use_vec": true,
    "source": "all",
    "top_k": 10,
    "case_count": 12,
    "embedding_model": "text-embedding-3-small",
    "synthesis_model": "llama-3.3-70b-versatile (Groq, primary) → gpt-4o-mini (OpenAI, fallback)"
  },
  "summary": {
    "total": 12, "passed": 12, "failed": 0,
    "pass_rate": 1.0,
    "passage_recall_mean": 1.0,
    "tag_recall_mean": 1.0,
    "substring_recall_mean": 1.0,
    "refusal_correct_count": 12
  },
  "results": [
    {
      "id": "tit_1_1_servant",
      "tags": [...],
      "question": "...",
      "pass": true,
      "duration_s": 4.8,
      "metrics": {
        "passage_recall": 1.0,
        "tag_recall": 1.0,
        "substring_recall": 1.0,
        "refusal_correct": true
      },
      "missing": { "passages": [], "tags": [], "substrings": [] },
      "expects": {...},
      "analysis": { "fts_query": "...", "passages": [...], "tags": [...], "intent": "..." },
      "retrieved_top": [
        {"rank": 1, "chunk_id": "...", "title": "...", "passage": "...",
         "tags": [...], "source": "...", "excerpt": "..."},
        ... (top 5)
      ],
      "retrieved_count": 10,
      "answer": "...",
      "citations": [...],
      "confidence": "high",
      "refusal_observed": false
    },
    ...
  ]
}
```

`eval/runs/latest.json` always mirrors the most recent run for easy
diffing.

## Metrics

For each case:

- **`passage_recall`** = `matched_expected_passages / total_expected_passages`. 1.0 = all expected passage ranges had at least one overlapping chunk in top-K.
- **`tag_recall`** = `matched_expected_tags / total_expected_tags`. Across the union of all retrieved chunks' tags.
- **`substring_recall`** = `matched_substrings / total_substrings` in the synthesized answer (case-insensitive).
- **`refusal_correct`** = `true` if expected_refusal == observed_refusal.

For the run summary:

- **`pass_rate`** = strict pass/fail ratio.
- **`<metric>_mean`** = mean across cases (informative even when a case fails).

## Adding a case

1. Open `eval/set/v1.yaml`, copy an existing case as template.
2. Pick an `id` (snake_case, descriptive).
3. Add tags for filtering / grouping.
4. Write the question.
5. Declare expectations — start minimal:
   - if a specific passage should always retrieve, list it
   - if a particular `kind:*` should appear, list it under `tags`
   - if the answer should mention a specific word, add to `substrings`
   - if it's an off-topic / unindexed query, set `refusal: true`
6. Run `python -m eval.run --ids your_new_case_id` to validate.

## When eval fails

The output JSON has everything you need. Inspect:

```bash
jq '.results[] | select(.pass == false)' eval/runs/latest.json
```

Common failure shapes and what they mean:

| Symptom | Likely cause |
|---|---|
| `passage_recall < 1.0` | Retrieval missed the expected passage range. Check `analysis.passages` (did the analyzer extract it?), then `retrieved_top` (is the relevant chunk in top-5? in top-K? not at all?). |
| `tag_recall < 1.0` with `kind:*` missing | The expected content shape isn't in top-K. Often signals an over-tight `doc_filter`. Try `--source all` to see if Aquifer fills the gap; if it does, your eval expectation is provenance-dependent. |
| `substring_recall < 1.0` | Either the expected word isn't in any retrieved chunk's body, or the LLM declined to use it. Check chunk bodies via `query.ask --no-llm --json`; if the word's there but the LLM didn't echo it, refine the synthesis prompt or the case's expectation. |
| `refusal_observed: true` when expected `false` | LLM judged the retrieved sources don't answer. Check `answer` text — if it explains why, the gap is real (sources don't actually contain answer-bearing content); if it's a strict-prompt over-refusal, soften the case or the prompt. |
| `refusal_observed: false` when expected `true` | LLM hallucinated an answer to an off-topic query. Bug. Tighten the synthesis prompt or eval. |

## Diff between runs

Two ways:

```bash
diff <(jq '.summary' eval/runs/A.json) <(jq '.summary' eval/runs/B.json)

jq -c '.results[] | {id, pass, metrics}' eval/runs/A.json > /tmp/A.txt
jq -c '.results[] | {id, pass, metrics}' eval/runs/B.json > /tmp/B.txt
diff /tmp/A.txt /tmp/B.txt
```

For a richer comparison, `jq` over `.results[] | select(.id == "...")`
on each file shows exactly which retrieved chunks changed.
