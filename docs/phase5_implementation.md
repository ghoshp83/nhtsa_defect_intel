# Phase 5 — Implementation Notes

This document captures the *what* and *why* of the Phase 5 build —
MLflow tracing on the agent/tool surface, the 3-tier eval harness
(`evaluation.py`), the per-tier TSV question sets, the notebook
driver, the scheduled workflow, and the supporting test coverage.

## What Phase 5 ships

```
src/nhtsa_curator/
├── agent.py        (+ @mlflow.trace on run_turn; extracted _call_llm span;
│                    session/user tags propagated to the current trace)
├── mcp.py          (+ mlflow.start_span wrapper around execute_tool;
│                    span_type dispatch: RETRIEVER for Genie/VS, TOOL for fetch_*)
└── evaluation.py   (NEW — 3-tier scorers, TSV loader, run_eval, LLM judge,
                    MLflow logging, CLI)

notebooks/
├── 5.1_run_eval.py       (NEW — driver: widgets, live clients, tier loop,
│                          Phase 5 exit check)
└── eval/
    ├── tier1_deterministic.tsv   (25 Q × [question, reference_sql, expected_value])
    ├── tier2_grounded.tsv        (25 Q × [question, source_id, expected_claim])
    └── tier3_synthesis.tsv       (25 Q × [question, reference_answer])

resources/
└── eval_workflow.yml     (NEW — weekly cron Mon 20 UTC; tier/use_judge/
                          gold_delta_version parameters; 2h timeout)

tests/
├── test_tracing.py       (NEW — 7 tests: span types, nesting, tags,
│                          disabled-tracing behaviour)
└── test_evaluation.py    (NEW — 33 tests: scorers, loaders, run_question,
                          aggregate_metrics, TBD handling)

conftest.py                (NEW — session-autouse fixture disables tracing;
                           opt-in tracing_enabled fixture for the tracing suite)
```

Test count is now **133** (up from 93 in Phase 4). All pass Spark-free
and without talking to any Databricks workspace.

## Key design decisions

### Three tracing levels, not one

The agent has three distinct "verbs" worth separating in MLflow:

1. `NhtsaAgent.run_turn`  → span_type **AGENT** (one per user turn)
2. `NhtsaAgent._call_llm` → span_type **LLM** (one per model round-trip)
3. `execute_tool(name, …)` → span_type **RETRIEVER** or **TOOL** (one per tool call)

Flat tracing (everything inside one big AGENT span) is cheaper but
makes it impossible to answer "how much of my latency is LLM vs tool
vs retrieval?" — which is the single question ops cares about most.
Three nested levels give the dashboard natural breakdowns without any
post-hoc parsing.

`_call_llm` was extracted as its own method specifically so
`@mlflow.trace` could decorate the actual API call without also
wrapping the surrounding orchestration. If a tool call dominates a
turn, the LLM span sum is tiny and the tool span dominates — instantly
visible in the trace tree.

### Tool span kind depends on the tool's role

`_span_type_for_tool(name)` in `mcp.py` returns **RETRIEVER** for
`genie_recalls` / `vector_search_narrative` (they pull evidence) and
**TOOL** for `fetch_tsb` / `fetch_investigation` (they fetch a single
deterministic record by id). MLflow's UI groups RETRIEVER spans
separately in the Retrieved Chunks pane, so this classification feeds
directly into a better eval-trace view.

### `start_span` context manager on `execute_tool`, not `@mlflow.trace`

`@mlflow.trace` introspects function arguments and serialises them as
the span's input. `execute_tool` takes a `ToolContext` dataclass
containing live Databricks clients — those don't serialise, and
MLflow's auto-capture warns noisily when it encounters them.

The context-manager form (`with mlflow.start_span(...) as span: ...`)
lets us pick what goes in: just `{"tool": name, "args": args}`. The
output is a *preview* (first 3 rows, truncation marker for the rest)
rather than the full payload — full tool results get logged separately
in the agent turn for Lakebase storage; traces stay scannable.

### Session + user tags on the agent trace

`mlflow.update_current_trace(tags={"session_id": …, "user_id": …})`
tags the top-level trace so ops can filter all traces for a given
session. This is called *after* the session is created inside
`run_turn` (new sessions only get their id after
`session_store.create_session`), and it short-circuits when no active
trace exists — see `_tag_current_trace` — so disabled-tracing tests
don't produce MLflow warnings.

### `conftest.py` controls tracing globally in the test suite

Session-autouse fixture does two things:

1. Points `MLFLOW_TRACKING_URI` at a tmp dir so any incidental MLflow
   calls land in isolated storage (never the user's `~/.mlflow`).
2. Calls `mlflow.tracing.disable()` so the default test-run is
   trace-free — the 100+ unit tests that don't care about tracing
   don't pay MLflow's span-open/close cost on every agent call.

The opt-in `tracing_enabled` fixture re-enables tracing *and* sets an
experiment ("nhtsa-unit-tests") so `mlflow.get_trace(trace_id)` can
resolve the backing run. Without the experiment, MLflow's file-store
backend returns "Experiment 0 does not exist" and the trace lookup
fails — a subtle trap we hit and documented with a fixture.

### Three tiers, distinct scorers, one runner

Per `docs/05_evaluation_strategy.md`:

- **Tier 1 — deterministic**: questions with objectively verifiable
  answers. Scorer path depends on the shape of `expected_value`:
  - Pure numeric (`re.fullmatch(r"-?\d+(?:\.\d+)?", expected)`) →
    extract first number from the answer, compare as floats.
  - Pipe-separated list (`"Tesla|Ford|GM"`) → check substring
    membership of each token against the normalised answer. The
    coverage ratio is treated as Jaccard; pass iff ≥ 0.8.
  - Anything else (e.g. `"model year 2023"`) → lowercased substring
    match.

  **Why no tokenisation on the answer side**: multi-word list values
  like "General Motors" get shredded when you split the natural-language
  answer on commas. Substring containment per expected token is more
  robust and gives a meaningful "missing" set in the breakdown.

  **Why numeric purity matters**: if we took the numeric path whenever
  `expected` contained *any* digit, `"model year 2023"` would match on
  just the `2023` and skip the "model year" context check. The purity
  gate forces the full phrase to match.

- **Tier 2 — citation-grounded**: the agent must cite a specific
  source id *and* make a faithful claim about it.
  - Citation check: normalise both sides by stripping non-alphanumerics
    (`re.sub(r"[^a-z0-9]", "", …)`) so "TSB 10160095" matches
    "TSB-10160095" — the agent shouldn't fail just because it used
    a space instead of a hyphen.
  - Faithfulness check: LLM judge scores 1–5; pass iff ≥ 4.
  - Short-circuit: no citation → score 0, judge never called (saves
    tokens on the obvious failures).

- **Tier 3 — synthesis rubric**: LLM judge returns five sub-scores
  (faithfulness, coverage, calibration, citation, conciseness). We
  average them. Pass iff mean ≥ 3.5 (leaves headroom above a mediocre 3).
  - Fallback: if the judge returns a single `score` instead of
    sub-scores, use that directly — handles off-protocol judge
    responses gracefully rather than scoring 0.

All three share one `run_question(agent, question, judge)` entry
point, and aggregation is done by a single `aggregate_metrics(tier,
results)` function that emits a tier-specific headline key
(`tier1_exact_match_rate`, `tier2_grounded_pass_rate`,
`tier3_avg_judge_score`) alongside the common latency + tool-call
stats. One runner + one aggregator means the CLI / notebook / workflow
/ promotion gate all consume the same metric dict shape.

### `TBD:*` placeholder for pending ground truth

Tier-1 tier accepts `TBD:<short-name>` as an `expected_value` to mean
"ground truth not yet known — waiting on a gold refresh". `EvalQuestion.
is_pending` returns `True` for these, `run_question` short-circuits
to `score=0.0, passed=False, pending=True`, and `aggregate_metrics`
excludes pending rows from both the numerator *and* the denominator
of `pass_rate`. Effect: we can commit the eval TSV before the gold
table has been ingested, and the gate stays green as long as the
*resolved* rows pass.

Alternative (rejected): delete pending rows until truth is resolved.
That loses the question text — the eval set stops reflecting the
actual coverage we intend to hold the agent to. The TBD convention
gives us coverage-as-written and pass-rate-as-scored in one file.

### LLM judge is an injected protocol, not a hardcoded client

`LLMJudge` is a `Protocol` with one method: `score(prompt, *, context)
→ dict`. Tests inject a `_FakeJudge([{"score": 5}, …])` with canned
responses. Notebook 5.1 and the CLI inject `OpenAICompatJudge`, which
wraps `ws.serving_endpoints.get_open_ai_client()` and asks for
`response_format={"type": "json_object"}`. On judge failure (timeout,
non-JSON response) we return `{"score": 0, "notes": "…"}` rather than
raising — one flaky judge call should not nuke a 75-question eval run.

### TSV, not JSONL, for eval sets

The eval files live in `notebooks/eval/*.tsv`. TSV because:

1. Columns are fixed per tier and flat — no need for JSON nesting.
2. Comments: the loader strips lines starting with `#`, so each TSV
   doubles as documentation (header comment block + per-row notes).
3. Reviewability: diffs against a TSV in a PR are legible; diffs
   against a JSONL with escaped quotes are not.

Downside: TSV can't hold newlines inside cells. We enforce this by
ensuring reference answers are single-line strings — long-form
reference answers go into notes on Tier-3 synthesis rather than
embedded line breaks.

### Header validation at load time

Loader compares the first non-comment row against a tier-specific
expected tuple and raises with the offending line number. Hard failure
rather than best-effort parsing: a misaligned column silently
swapping `expected_value` with `reference_sql` would corrupt scores in
a way no downstream test would catch. Fail at load, not at score.

### Per-question artifacts vs. aggregate metrics

For each question, `run_eval` logs:

- `mlflow.log_metric(f"{tier}.score", score, step=idx)` — scalar,
  queryable across runs.
- `mlflow.log_metric(f"{tier}.latency_s", latency, step=idx)` — same.
- `mlflow.log_dict(result.to_dict(), f"per_question/{idx}.json")` —
  full row including tool trace, judge notes, metric breakdown.

And for the run:

- `mlflow.log_metric(f"agg.{k}", v)` for every scalar in
  `aggregate_metrics(tier, results)`.
- `mlflow.log_dict(metrics, "aggregate_metrics.json")`.

Why both: `agg.*` metrics drive the dashboard and promotion gate;
per-question JSONs are what a developer opens when a regression
appears. You need both — either alone leaves one audience blind.

### The notebook 5.1 exit check

The notebook asserts on tier-level issues before exiting:

- zero questions in any tier → assertion (misconfigured widget / path)
- `error_rate > 0.2` → assertion (agent crashing on ≥ 1-in-5 questions)
- tier-1 all-pending → print + skip pass-rate check (allowed to ship)

The exit check is notebook-side, not harness-side, because different
runs want different thresholds: an ad-hoc "single tier" run shouldn't
fail on `n=1`. The harness returns numbers; the driver decides.

## End-to-end trace shape

A single `run_turn` with one Genie call produces:

```
AGENT  NhtsaAgent.run_turn                 tags: session_id, user_id
├── LLM    NhtsaAgent._call_llm            (decision-making turn)
├── RETRIEVER tool.genie_recalls           inputs: {tool, args}
│                                          outputs: preview (≤ 3 rows)
└── LLM    NhtsaAgent._call_llm            (follow-up synthesis turn)
```

The LLM spans appear on either side of the RETRIEVER span because the
agent loop is: think → act → think. A turn with no tool calls has a
single LLM span; a turn that bails after one tool call has two LLM
spans with one RETRIEVER between them. The ops dashboard groups by
span_type, so a "slow turns" query drills straight to whichever
component was actually slow.

## How to run Phase 5

```bash
# Unit tests (133 total including Phase 5 additions)
uv run pytest

# Evaluation CLI — runs all three tiers against a live agent
uv run python -m nhtsa_curator.evaluation \
    --tier all --target dev \
    --eval-dir notebooks/eval \
    --experiment /Shared/nhtsa-curator-course-pg

# Or run notebook 5.1 interactively:
# - open notebooks/5.1_run_eval.py
# - widgets: env=dev, run_id=manual, tier=all, use_judge=true
# - run all cells
# - confirm "Phase 5 eval checkpoint: PASS"

# Scheduled weekly (Mondays 20 UTC) via the bundle resource
databricks bundle run eval_workflow --target dev
```

## What's intentionally NOT in Phase 5

- **Serving-endpoint deployment** — Phase 6 via `databricks-agents` +
  `register_agent_workflow.yml`. Phase 5 produces the eval numbers
  that gate the promotion decision.
- **Automated promotion gate** — the workflow fails loud on
  `error_rate > 0.2`, but *comparison against a baseline run* (is the
  current model better than what's in prod?) is Phase 6. Right now
  a human reads the metrics dict and decides.
- **Trace export to `gold_agent_traces`** — traces land in the MLflow
  experiment; the pipeline that materialises them into a Delta table
  for BI consumption is Phase 6.
- **Parallel per-question execution** — we run sequentially. Serving
  endpoints have token-per-minute ceilings, so parallelising gives
  you 429s, not speedups. Revisit when the eval set grows past ~200
  questions.
- **Streaming judge output** — judge calls are synchronous + blocking.
  Tier-3 runs add ~1s per question; for a 25-question tier that's 25s
  of judge latency per run. Fine at this scale.
- **Alternative judges / model cross-checking** — we use the same
  `cfg.llm_endpoint` for both the agent and the judge. A stronger
  judge (or a different provider for independent checking) is a
  Phase 6+ concern if judge variance becomes a blocker.

## Open questions to validate during first live run

1. **Tier-1 TBD row count** — how many of the 25 tier-1 questions
   still have `TBD:*` ground truth after the first real gold refresh?
   If `n_pending > n_scored`, the pass-rate denominator is too small
   to be meaningful. Remedy: either resolve more TBDs (by running the
   reference SQL against gold) or drop placeholder rows from the file
   until truth is known.
2. **Judge score calibration** — the 4/5 tier-2 threshold and
   3.5/5 tier-3 threshold are borrowed from `docs/05`. After the first
   full run we should look at the judge-score distribution: if 90% of
   rows score 4-5 regardless of answer quality, the judge prompt is
   too lenient and needs sharpening (probably more explicit grounding
   criteria). If 90% score 2-3, the opposite.
3. **Tool-span output-preview size ceilings** — we cap at 3 rows
   before the `_truncated` marker. For Genie queries returning wide
   rows (50+ columns), even 3 rows can be KBs. If the MLflow UI gets
   sluggish, tighten to a column-subset preview rather than row count.
4. **Judge JSON reliability** — Databricks LLM endpoints honour
   `response_format={"type": "json_object"}` but sometimes emit trailing
   prose. `OpenAICompatJudge` catches `JSONDecodeError` and falls back
   to a zero score. If the log shows more than ~5% judge fallbacks,
   move to a schema-enforced format (function-calling with a
   `submit_scores` tool) rather than free-form JSON.
5. **Eval run token budget** — 75 questions × (agent turn + judge
   turn) ≈ 150 LLM round-trips per full run. On a serving endpoint
   rate-limited to 60 req/min, that's ~2.5 min of wall-time just from
   rate limiting. If this blocks iteration, drop `use_judge=false` in
   dev or split the run across tiers.
6. **MLflow file-store deprecation** — pytest emits a `FutureWarning`
   that the filesystem tracking backend will be deprecated Feb 2026.
   No action needed now (our tests run against a tmp dir anyway), but
   worth noting before Phase 7's CI hardening.

## Test count progression

| Phase | New tests | Cumulative | Notes |
|-------|-----------|------------|-------|
| 0     | —         | 0          | scaffolding only |
| 1     | 17        | 17         | http + flat-files |
| 2     | 15        | 32         | chunking + taxonomy + pii |
| 3     | 15        | 47         | VS + Genie helpers |
| 4     | 46        | 93         | mcp + memory + agent routing |
| 5     | 40        | **133**    | tracing + evaluation harness |

Phase 5 test mix: 7 tracing (span types / nesting / tags / disabled
tracing) + 33 evaluation (loaders 9, scorers 15, end-to-end
run_question + aggregate 9). The tracing suite is deliberately small
because MLflow itself is heavily tested upstream — we only verify the
specific *shape* of the spans we emit, not tracing internals. The
evaluation suite is larger because every scorer branch is a separate
promotion-gate surface: a bug in `score_tier2`'s citation
normalisation could silently pass every tier-2 question and make the
eval number meaningless.
