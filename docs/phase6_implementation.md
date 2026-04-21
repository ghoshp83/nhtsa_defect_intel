# Phase 6 — Implementation Notes

This document captures the *what* and *why* of the Phase 6 build —
agent deployment as a Databricks serving endpoint, the trace-
propagation pipeline that feeds the ops dashboard, the NHTSA-specific
feedback scorers, the dashboard itself, and the demo driver notebook.

Phase 6 is the phase that turns "a locally-scored eval harness" into
"a published `/responses` endpoint with ops telemetry" — everything
between `mlflow.genai.evaluate` and a Grafana-style dashboard.

## What Phase 6 ships

```
nhtsa_agent_pg.py                              (NEW — MLflow entry point,
                                               model-as-code wrapper)

src/nhtsa_curator/
├── serving.py                                 (+ NhtsaResponsesAgent — pyfunc
│                                               ResponsesAgent wrapper; trace-
│                                               tag helpers; /responses envelope
│                                               helpers)
├── agent.py                                   (+ log_register_agent() —
│                                               mlflow.pyfunc.log_model with
│                                               enumerated UC resources, eval
│                                               metric filter, latest-model alias)
├── evaluation.py                              (+ cite_id_present, word_count_under,
│                                               mentions_oem scorers; Guidelines
│                                               judges; evaluate_agent() wrapper)
└── utils/
    ├── __init__.py                            (NEW)
    └── common.py                              (NEW — get_widget, set_mlflow_
                                                tracking_uri, get_delta_table_version)

resources/
├── register_deploy_agent.yml                  (NEW — 2-task job:
│                                               log_register → deploy)
├── update_traces_aggregated.yml               (NEW — hourly cron)
├── deployment_scripts/
│   ├── log_register_agent.py                  (NEW — evaluate + pyfunc.log_model
│   │                                           + UC register + alias)
│   ├── deploy_agent.py                        (NEW — databricks.agents.deploy
│   │                                           against latest-model alias)
│   └── update_traces_aggregated.py            (NEW — cheap scorers on all
│                                               traces + guidelines on 10%;
│                                               rebuilds aggregated view)
└── dashboard/
    ├── nhtsa_agent_monitoring_dashboard.yml   (NEW — bundle resource)
    └── nhtsa_agent_monitoring_dashboard       (NEW — KPIs + time-series +
        .lvdash.json                            tool-mix + judge-outcome
                                                + trace-drilldown widgets)

notebooks/
└── 6.1_propagate_traces.py                    (NEW — 30-question demo driver
                                                with session_id / request_id
                                                stamped via custom_inputs)

tests/
└── test_phase6.py                             (NEW — 40 tests: feedback
                                                scorers, ResponsesAgent shape,
                                                log_register_agent resources,
                                                trace-tag stamping, SQL/dashboard
                                                drift guards)

databricks.yml                                 (+ warehouse_id variable with
                                                per-env override; include glob
                                                expanded to resources/*/*.yml;
                                                nhtsa_agent_pg.py in sync block)
```

Test count is now **173** (up from 133 in Phase 5). All 40 new tests
run Spark-free and without a live Databricks workspace.

## Key design decisions

### Why a separate `NhtsaResponsesAgent`, not a refactor of `NhtsaAgentModel`

`NhtsaAgentModel` (Phase 4) conforms to MLflow's `PythonModel` — it
accepts `{"inputs": [...]}` dicts and returns plain strings. That's
fine for the evaluation harness, which drives it directly in-process.

But `databricks.agents.deploy` requires an agent that speaks the
**Responses API**: `/responses` request envelopes in, streamed
`response.output_item.done` events out. MLflow's `ResponsesAgent` base
class is the contract for that.

Rather than retrofit `NhtsaAgentModel` (which would churn Phase 4–5
tests and introduce a dual request-shape branch), we layered a thin
wrapper:

```
NhtsaResponsesAgent(ResponsesAgent)
  └── holds a NhtsaAgentModel instance
  └── parses /responses request envelope → extracts user text + custom_inputs
  └── calls inner .predict(...) → natural-language answer
  └── wraps answer in response.output_item.done event
  └── stamps session_id / request_id / deploy tags on the active trace
```

Two concrete wins from the wrapper approach:

1. **No regression surface**: Phase 4/5 tests still exercise
   `NhtsaAgentModel` unchanged. The 40 new Phase 6 tests live in
   `test_phase6.py` and target the wrapper.
2. **Deploy-time concerns stay isolated**: trace-tag stamping,
   `custom_inputs` parsing, and OpenAI envelope construction all live
   in one file (`serving.py`) rather than bleeding into `agent.py`.

Rejected alternative: make `NhtsaAgentModel` inherit from
`ResponsesAgent` directly. Forces every test and every eval-harness
call-site to manufacture `/responses` envelopes even when a plain
string would do. The extra indirection cost in tests outweighed the
"one fewer class" win.

### Eager `load_context` in the wrapper constructor

MLflow's `PythonModel.load_context` runs on first inference by
default. For a cold-start serving replica that means the first user
request pays:

- Databricks SDK auth (1–2s)
- Vector-search index handle creation (~1s)
- Warehouse / Genie client instantiation (~500ms)

…on top of the actual LLM call. `NhtsaResponsesAgent.__init__` calls
`self._inner.load_context(None)` eagerly so the first real request
hits a warm object. This trades a small increase in replica-boot time
for a consistent latency profile on the first post-boot request —
exactly what you want on a serving endpoint where scale-from-zero
already costs ~30s.

### `custom_inputs` is the session/request propagation channel

The `/responses` API doesn't give callers a standard knob for
"attach these tags to the resulting trace". Databricks agents solve
this with `custom_inputs`: a free-form dict that the request-sender
passes through `extra_body={"custom_inputs": {...}}`, and that the
agent can read inside `predict_stream`.

`_stamp_trace_deploy_tags(custom_inputs)` pulls three things out:

1. `session_id` → trace tag → joins per-session on the dashboard
2. `request_id` → `client_request_id` on the trace → traceable to
   the caller's correlation id across systems
3. `user_id` → trace metadata → filterable by who

And three from the serving environment:

4. `GIT_SHA` → trace tag → "which commit produced this trace"
5. `MODEL_VERSION` → trace tag → UC version at serve time
6. `MODEL_SERVING_ENDPOINT_NAME` → trace tag → the aggregated view's
   filter predicate

Those last three are baked into the deployment as env vars in
`deploy_agent.py`. The tracing dashboard is *designed* to filter on
`tags.endpoint_name = 'nhtsa-agent-endpoint-prd-pg'` — if that tag
were missing, traces from dev + prd would commingle and every chart
would be misleading.

### Why enumerate UC resources at `log_model` time

`mlflow.pyfunc.log_model(resources=[...])` is what tells Unity Catalog
"this deployed model will need on-behalf-of-user access to the
following endpoints / warehouses / tables / indexes / Genie spaces".
UC denies the request at *serve time* if any of those weren't declared
at *log time*.

`log_register_agent` enumerates **every** gold table the Genie tool
could query — not just the ones the current eval questions happen to
hit — because a missing table manifests as a UC 403 on a prod user
question, not at deploy time or in tests. The test
`test_log_register_agent_resources_contain_all_gold_tables` exists
specifically to catch regressions where someone adds a new Genie
table but forgets the resources list.

**Placeholder genie_space_id handling**: in dev, `cfg.genie_space_id`
is often `"PLACEHOLDER_..."` because Genie spaces are created
per-workspace and not all devs have one. The resources list
conditionally appends `DatabricksGenieSpace` only when the id is real
— otherwise UC rejects the registration with "invalid genie space
id".

### Filter evaluation_metrics before `mlflow.log_metrics`

`log_metrics(dict)` requires `float`-castable values. Our
`evaluate_agent` returns a mix: numeric per-scorer averages plus
string metadata like `judge_notes` or `tier` labels. Shipping the raw
dict means a `ValueError: could not convert string to float`.

The filter is one line:

```python
numeric = {k: float(v) for k, v in evaluation_metrics.items()
           if isinstance(v, (int, float))}
```

…but it matters because the failure mode is loud-and-late (the
pyfunc.log_model succeeds, the whole registration flow runs, then the
very last metric-log step explodes), which is the worst place to fail.

### `latest-model` alias, not `champion`, set at log time

Two registry aliases matter for this project:

- `latest-model` — moves with every successful registration. "The
  most recent build." This is what `deploy_agent.py` reads.
- `champion` — moves only when a human (or automated gate) promotes
  a version. "What's in prod." Set by a separate promotion step,
  never by `log_register_agent`.

If `log_register_agent` set `champion` automatically, a flaky
evaluation run could ship a broken model to prod. Keeping the two
aliases strictly separated — latest for continuous integration,
champion for human-gated promotion — preserves the ability to roll
back without re-running the whole pipeline.

### Three "cheap" scorers run on every trace; guidelines on 10%

`update_traces_aggregated.py` applies scorers in two tiers:

1. **Cheap, on 100% of traces**: `cite_id_present`, `word_count_under`,
   `mentions_oem`. Pure-Python regex + string ops. O(microseconds)
   per trace. Free, may as well.
2. **Expensive, on 10% sample**: `factual_defect`, `cite_every_claim`,
   `stays_in_scope` (MLflow `Guidelines` judges, seed=42 for
   reproducibility). Each costs a judge LLM round-trip — at high
   traffic, judging every trace would swamp the endpoint budget and
   push eval costs to 2× serving costs.

Picking the 10% is deterministic via seed so the same trace is
always in or always out — dashboard judge-outcome trend isn't
destabilised by sample churn on re-run. Increase the fraction for a
pre-promotion run; keep it at 10% for the hourly cron.

### NHTSA-specific span-name matching in the aggregated view

The aggregated view explodes `spans` and counts calls per tool:

```sql
SUM(CASE WHEN span.name = 'tool.genie_recalls'         THEN 1 ELSE 0 END) AS genie_call_count,
SUM(CASE WHEN span.name = 'tool.vector_search_narrative' THEN 1 ELSE 0 END) AS vs_call_count,
SUM(CASE WHEN span.name = 'tool.fetch_tsb'             THEN 1 ELSE 0 END) AS fetch_tsb_count,
SUM(CASE WHEN span.name = 'tool.fetch_investigation'   THEN 1 ELSE 0 END) AS fetch_investigation_count,
```

The dashboard consumes those column names directly. Silently renaming
a span in Phase 4's `mcp.py` would break the dashboard with no test
signal — so `test_update_traces_script_uses_nhtsa_tool_span_names`
reads the script file and greps for all four literal span strings.
Cheap, catches the exact drift that would otherwise surface only when
someone opens the dashboard and sees zeros.

The sibling test
`test_update_traces_script_exposes_nhtsa_columns` does the reverse —
asserts the column names the dashboard charts rely on are emitted by
the script. Two-way drift guard.

### Dashboard JSON integrity test — parse + key-widget grep

Dashboard JSON is opaque: one misplaced brace and the whole dashboard
fails to import with a generic parse error. Two tiny tests remove
that class of regression:

- `test_dashboard_json_parses_and_points_at_view`: JSON-parses the
  file and asserts the dataset query references the aggregated view.
- `test_dashboard_json_widgets_cover_key_kpis`: greps the raw JSON
  for the widget names the README promises (`kpi_total_traces`,
  `kpi_cite_rate`, `kpi_oem_rate`, `kpi_p95_latency`, etc.). If any
  widget is deleted, the test fails with the name of the missing
  widget.

Neither test validates correctness (a bar chart that should be a
pie chart still passes). They just ensure the file *exists* and
*has* the widgets the surrounding pipeline assumes.

### `/responses` streaming with one `output_item.done` event

MLflow's `ResponsesAgent.predict_stream` contract is: yield
`ResponsesAgentStreamEvent` objects. For a non-streaming agent (ours),
the simplest conforming shape is a single `response.output_item.done`
event containing the full answer:

```python
yield ResponsesAgentStreamEvent(
    type="response.output_item.done",
    item={
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": answer}],
    },
)
```

We don't stream tokens because the underlying agent is synchronous
(multi-turn tool loop + final LLM call) and fake-streaming a complete
response adds no user value. If / when we move the agent to token-
streaming, this is the integration point — the single `output_item.
done` becomes an `output_item.added` followed by `output_text.delta`
events followed by `output_item.done`.

### Request-shape tolerance: 4 input shapes into one normalised form

`_extract_responses_request` normalises four valid input shapes into
a single `(user_text, custom_inputs)` tuple:

1. **Plain dict**: `{"input": [...], "custom_inputs": {...}}` — the
   canonical `/responses` shape.
2. **Messages alias**: `{"messages": [...], "custom_inputs": {...}}`
   — some Databricks SDK calls still use OpenAI chat-completion
   naming.
3. **String probe**: `{"input": "hello"}` — MLflow's
   `log_model(input_example=...)` sometimes captures a plain string
   rather than a dict when the user passes a bare prompt. Handled
   defensively so `log_model` auto-validation doesn't fail.
4. **Pydantic object**: `ResponsesAgentRequest(...)` — what
   `databricks.agents.deploy` actually passes at serve time.

Parameterised tests in `test_phase6.py` cover all four. The specific
fragility: MLflow's signature inference runs the model against the
input example *during logging*, so if shape 3 isn't handled the
registration flow fails after 90% of the work is done.

## The deployment pipeline flow

```
 ┌──────────────────────────────────┐
 │ bundle deploy                    │
 │  → databricks.yml resources load │
 │  → whl built + synced            │
 └──────────────┬───────────────────┘
                │
                ▼
 ┌──────────────────────────────────────────────────────┐
 │ register_deploy_agent.yml  (manual trigger or CI)    │
 │                                                      │
 │  task 1: log_register_agent                          │
 │    - evaluate_agent(cfg, "eval_inputs.txt")  ← smoke │
 │    - pyfunc.log_model(                               │
 │        python_model="nhtsa_agent_pg.py",             │
 │        resources=[...every UC asset...],             │
 │        input_example={...custom_inputs...},          │
 │      )                                               │
 │    - register_model(...) → version N                 │
 │    - set_alias("latest-model", N)                    │
 │    - dbutils.jobs.taskValues.set("model_version", N) │
 │                                                      │
 │  task 2: deploy_agent                                │
 │    - read "latest-model" alias                       │
 │    - databricks.agents.deploy(                       │
 │        endpoint_name=f"nhtsa-agent-endpoint-{env}-pg"│
 │        scale_to_zero=(env != "prd"),                 │
 │        environment_vars={GIT_SHA, MODEL_VERSION,     │
 │                         MODEL_SERVING_ENDPOINT_NAME, │
 │                         secret refs}                 │
 │      )                                               │
 └──────────────────────────────────────────────────────┘

                [endpoint is now live]
                          │
                          ▼
 ┌──────────────────────────────────────────────────────┐
 │ Live traffic (notebook 6.1, users, eval)             │
 │   /responses → NhtsaResponsesAgent.predict_stream    │
 │     → stamps session_id / request_id / env tags      │
 │     → emits AGENT / LLM / RETRIEVER / TOOL spans     │
 └──────────────────┬───────────────────────────────────┘
                    │ traces land in MLflow
                    ▼
 ┌──────────────────────────────────────────────────────┐
 │ update_traces_aggregated.yml  (hourly: 0 0 * * * ?)  │
 │                                                      │
 │  - find unscored traces for this endpoint            │
 │  - cheap scorers: cite_id_present, word_count,       │
 │                   mentions_oem  (100% of traces)     │
 │  - guidelines judges: factual_defect, cite_every_    │
 │                       claim, stays_in_scope  (10%)   │
 │  - mlflow.log_feedback(trace_id, name, value)        │
 │  - rebuild `{catalog}.{schema}.nhtsa_traces_         │
 │    aggregated_pg` view:                              │
 │      cols: request_id, session_id, user_id,          │
 │            latency_ms, total_tokens,                 │
 │            genie_call_count, vs_call_count,          │
 │            fetch_tsb_count, fetch_investigation_count│
 │            cite_id_present, word_count_under,        │
 │            mentions_oem, factual_defect,             │
 │            cite_every_claim, stays_in_scope          │
 └──────────────────┬───────────────────────────────────┘
                    │
                    ▼
 ┌──────────────────────────────────────────────────────┐
 │ nhtsa-agent-monitoring-dashboard.lvdash.json         │
 │   KPIs + time-series + tool-mix + judge-outcomes     │
 │   + trace drilldown (filterable by session_id)       │
 └──────────────────────────────────────────────────────┘
```

## NHTSA-specific deviations from the reference project

The reference project (`arxiv-curator`) ships the generic LLMOps
skeleton. These are the Phase 6 pieces we customised for NHTSA:

1. **Cite-id regex**: reference project uses a generic `arxiv:*`
   pattern. We built a regex union for four NHTSA source systems —
   recall campaigns (`23V123456`), investigations (`PE22-001`,
   `EA23-004`, `DP24-002`, `RQ22-003`, `AQ21-005`), TSBs
   (`TSB-10160095`), ODI complaints (`ODI-11567823`). Each has a
   different ID convention; lumping them into one regex means the
   scorer is one pattern against a single response string.

2. **OEM keyword list**: 34-entry tuple covering traditional
   manufacturers (Ford, GM, Stellantis, Toyota, Honda, Hyundai, Kia,
   Volkswagen, BMW, Mercedes, Tesla, …), AV operators (Waymo, Cruise,
   Zoox, Aurora, Motional, Nuro, Gatik, …), and commercial-vehicle
   OEMs (Freightliner, Peterbilt, Paccar, …). Stays-in-scope check is
   "at least one of these must appear"; breadth matters because a
   Tier-3 question about "AV crashes" might legitimately respond with
   "Cruise and Waymo" without any traditional OEM.

3. **Tool-span names** (already referenced above): `tool.genie_
   recalls`, `tool.vector_search_narrative`, `tool.fetch_tsb`,
   `tool.fetch_investigation` — all NHTSA-specific. The aggregated
   view and the tool-mix chart on the dashboard name these
   explicitly.

4. **Demo question bank** (`notebooks/6.1_propagate_traces.py`): 30
   questions split across the three eval tiers but tuned for the
   NHTSA domain — Tier-1 counts by model year + OEM, Tier-2 cite-
   specific-source queries, Tier-3 synthesis ("ADAS-related
   investigations this year", "Tesla recalls vs Tesla complaints
   disagree about …"). Shuffled at runtime so the trace timeline
   shows realistic interleaved traffic.

5. **Dashboard KPIs with NHTSA framing**: `kpi_cite_rate` reads as
   "% of answers with a valid defect-id citation" (not generic
   citation). `kpi_oem_rate` tracks "% mentioning an OEM" — directly
   answers "is the agent staying on the NHTSA domain?". Reference
   project's equivalent is "% mentioning arXiv id" — different
   signal, same mechanic.

## How to run Phase 6

```bash
# Unit tests (173 total; 40 new in Phase 6)
uv run pytest

# Deploy the bundle (dev target, fresh whl)
databricks bundle deploy --target dev

# Trigger the register + deploy job
databricks bundle run register_deploy_agent --target dev

# Warm the endpoint + seed the aggregated view
databricks workspace run notebooks/6.1_propagate_traces.py \
    --target dev \
    --params env=dev,run_label=demo-2026-04-18,sleep_seconds=2

# Force an immediate aggregation (otherwise waits for next hour)
databricks bundle run update_traces_aggregated --target dev

# Open the dashboard
databricks workspace open \
    /Workspace/Users/.../[dev]-NHTSA-Agent-Monitoring-Dashboard
```

## What's intentionally NOT in Phase 6

- **Automated promotion gate** — `champion` alias is still set by
  hand. The "eval-driven promotion" flow (compare metric-deltas
  between `latest-model` and `champion`, bail if regression > X%)
  belongs in Phase 7.
- **Multi-endpoint canary** — single endpoint per env. A 95/5
  traffic split between old and new champion isn't worth the
  complexity at this scale; redeploy-and-roll-back is the current
  rollback strategy.
- **Dashboard alerting** — the dashboard is read-only. Paging on
  `cite_rate < 0.7` or `p95_latency > 10s` requires a Databricks
  SQL alert attached to the aggregated view. One `yml` resource
  away, but scoped to Phase 7 operations hardening.
- **Cost telemetry** — the aggregated view exposes `total_tokens`
  per trace but not `$/trace`. Token-to-dollar conversion depends
  on the endpoint's pricing tier, which varies by env and by
  renegotiation cadence with Anthropic — putting a stale multiplier
  in the pipeline is worse than omitting cost entirely.
- **Streaming `output_text.delta` events** — see design note above.
  Agent is synchronous; faking streaming adds complexity for no
  user-visible latency improvement.
- **Endpoint auto-scaling tuning** — `scale_to_zero=True` in dev /
  acc for cost, `False` in prd for latency floor. Concurrent-request
  tuning (`scale_to_zero_enabled`, `min_provisioned_throughput`,
  `max_provisioned_throughput`) is a prd-only concern once we have
  load data from Phase 7.

## Open questions to validate during first live deploy

1. **Cold-start latency of `NhtsaResponsesAgent`** — eager
   `load_context` helps but doesn't eliminate. What's the
   end-to-end first-request latency on a scaled-from-zero replica?
   If > 30s consistently, we need to pin replicas warm in prd or
   move the VS / Genie client instantiation to module-load time.
2. **`custom_inputs` round-trip fidelity** — the OpenAI Python SDK
   sends `extra_body` as part of the JSON body; Databricks serving
   forwards it verbatim, but we haven't confirmed that every field
   (including nested dicts) survives when the caller sets
   `stream=True`. First demo run with notebook 6.1 is the first
   real test.
3. **Hourly aggregation window adequacy** — the cron runs every
   hour. If one hour's worth of traces exceeds the per-job execution
   budget (90 min timeout), we get lapped. First week of
   production-like traffic will show whether we need 30-min or 15-min
   intervals.
4. **Guidelines judge cost at 10% sampling** — 10% of, say, 1000
   hourly traces = 100 judge calls × 3 rubrics = 300 LLM round-trips
   per hour. On the `databricks-claude-3-7-sonnet` endpoint that's
   likely acceptable; if someone cranks traffic to 100k traces/hr
   the 10% becomes 10k × 3 = 30k judge calls and we need either a
   cheaper judge model or a lower sample rate.
5. **UC resource drift** — we enumerate 10 resources today. If the
   agent gains a new tool (Phase 7+), the `log_register_agent`
   resource list has to be extended *or* the test we added as a
   drift guard will fail. That's by design, but worth calling out
   so "the test is red" is read as "the resource list is stale",
   not "the test is wrong".
6. **Dashboard refresh on zero-trace windows** — if a dev env goes
   8 hours without traffic, the aggregated view is empty and the
   KPI counters show "—". The dashboard handles this gracefully
   (empty-state text), but first-time viewers sometimes read it as
   "dashboard broken". Add an "as-of" timestamp widget in a later
   iteration.

## Test count progression

| Phase | New tests | Cumulative | Notes |
|-------|-----------|------------|-------|
| 0     | —         | 0          | scaffolding only |
| 1     | 17        | 17         | http + flat-files |
| 2     | 15        | 32         | chunking + taxonomy + pii |
| 3     | 15        | 47         | VS + Genie helpers |
| 4     | 46        | 93         | mcp + memory + agent routing |
| 5     | 40        | 133        | tracing + evaluation harness |
| 6     | 40        | **173**    | serving + scorers + dashboard |

Phase 6 test mix: 14 feedback scorers (cite-id regex branches + OEM
+ word-count), 8 ResponsesAgent request-shape parsing (4 shapes ×
edge cases), 6 log_register_agent resource enumeration + metric
filter, 4 trace-tag stamping (env-var forwarding + no-op when
tracing off), 4 update-traces SQL drift guards, 4 dashboard JSON
integrity. Nothing exercises the live serving endpoint or a real
Databricks workspace — those go in a separate `integration/` suite
(Phase 7).
