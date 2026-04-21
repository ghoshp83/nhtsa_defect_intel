# Phase 4 — Implementation Notes

This document captures the *what* and *why* of the Phase 4 build —
the four-tool agent, the Lakebase session store, the MLflow
model-as-code wrapper, and the agent-routing test harness.

## What Phase 4 ships

```
src/nhtsa_curator/
├── mcp.py        (4 tool specs + dispatcher + merge_filters + MCP URLs)
├── memory.py     (SessionStore protocol, Postgres + in-memory impls, DDL)
├── agent.py      (NhtsaAgent tool-call loop + multi-turn memory wiring)
└── serving.py    (MLflow pyfunc ChatModel wrapper; model-as-code entry)

notebooks/
├── 4.1_agent_local.py      (live driver: 5 canned questions, routing check)
└── 4.2_lakebase_setup.py   (one-time DDL + smoke round-trip)

resources/
├── lakebase_setup_job.yml  (no schedule; manual re-run on DDL change)
└── agent_smoke_job.yml     (cron at 18 UTC: run notebook 4.1 in-memory)

tests/
├── test_mcp.py            (21 tests — tool specs, dispatcher, merge)
├── test_memory.py         (14 tests — InMemorySessionStore + DDL shape)
└── test_agent_routing.py  (11 tests — ScriptedLLM tool-loop behaviours)
```

Test count is now **93** (up from 47 in Phase 3). All pass Spark-free
and without talking to any Databricks workspace.

## Key design decisions

### Four tools, no more

Per `docs/04_agent_design.md`: exactly `genie_recalls`,
`vector_search_narrative`, `fetch_tsb`, `fetch_investigation`.

The LLM's tool-routing accuracy drops measurably once the tool list
grows past ~5 — every extra tool widens the prompt and pushes the
model toward low-value "chained" calls. Anything that *could* be a
tool but isn't urgent (e.g. recall unit-count by campaign) is either
expressible as a Genie question or best left off the surface.

### Tool dispatch is a single switch

`mcp.py::execute_tool(name, args, ctx)` is the only place that knows
how to turn a tool name into a real call. Everything else (agent loop,
MLflow wrapper, tests) goes through this one function. Adding a fifth
tool means: add the spec, add an `elif` branch, add the implementation
— no changes elsewhere.

### `ToolContext` dependency bundle

Every tool needs some subset of: `ProjectConfig`, `VectorSearchClient`,
Genie API client, a SQL executor. We bundle these into a single
`ToolContext` dataclass carried through the agent loop.

Alternative (rejected): module-level globals set at startup. That's
what most notebook-first projects do, and it makes tests impossible —
you can't build a `ToolContext` with fakes without also unwinding the
globals. Explicit context objects are the clean path.

### In-memory + Postgres session stores behind one Protocol

`memory.py` defines a `SessionStore` `Protocol` and two concrete
implementations:

1. `PostgresSessionStore` — real Lakebase, via a `conn_factory` that
   fetches a fresh credential on each call (handles token rotation
   transparently).
2. `InMemorySessionStore` — thread-safe Python-dict shim, used by
   tests and by the notebook's `use_lakebase=false` path.

Shared contract tested in `test_memory.py`: duplicate `turn_idx`
raises, invalid role raises, `get_history` returns ascending order,
`get_history(max_turns=k)` returns the last *k*. If either impl
drifts from the contract, the tests catch it.

### DDL lives in `memory.py`, not a migrations dir

`DDL_STATEMENTS` is a tuple of three idempotent `CREATE TABLE IF NOT
EXISTS` statements. `PostgresSessionStore.init_schema()` executes
them; notebook `4.2_lakebase_setup.py` does the same and then runs a
round-trip smoke test.

Why not Alembic: the schema is 2 tables + 1 index, and we've shipped
it once. A migrations framework is pure overhead at this size.
Revisit when we add the 3rd or 4th schema change.

### `PostgresSessionStore` takes a factory, not a connection

```python
PostgresSessionStore(conn_factory=lambda: psycopg.connect(...))
```

We open a short-lived connection per operation. Reasons:

- Lakebase credentials rotate; a long-lived connection dies.
- Agent turns are seconds apart but unpredictable — connection pooling
  doesn't buy much at this traffic.
- Tests can pass a trivial factory without running a real server.

If latency ever becomes a concern, wrap the factory in `psycopg_pool`
— the store's public surface doesn't change.

### Agent loop is sequential, synchronous

The tool call loop dispatches tools one LLM response at a time. When
the LLM returns *multiple* tool calls in one response, we run them in
order within that step (not in parallel).

Parallelising fan-out would shave ~200-500 ms off combined questions
like "top 3 OEMs by fires *with* example narratives", but:
- Genie + VS both have ~100 ms variance already; small wins.
- Parallel dispatch complicates the filter-merge ordering.
- Phase 5 adds MLflow tracing, which will quantify the cost —
  optimise after measurement, not before.

### `max_tool_steps = 6` is a safety rail

The longest legitimate trajectory we've designed for is:

    genie → vs → fetch_tsb → fetch_investigation → (one retry) → answer

That's 5 tool steps + 1 answer. We allow 6 before forcing termination
with `stopped_reason="max_steps"`. A model that spins past this is
usually confused about why its tool calls aren't producing what it
expects; retrying more wastes tokens.

### Filter accumulation uses a dedicated `merge_filters`

When the user says "now filter to 2023", the agent calls
`vector_search_narrative` with `filters={"model_year": 2023}`. We
merge that into `accumulated_filters` in the session store so *next*
turn's `_build_messages` injects the bag back into the LLM context.

Rules (from `mcp.py::merge_filters`):
- **Scalar overwrites** — the last user turn is authoritative.
- **Lists union, preserving order** — `["Tesla"] + ["Ford"]` →
  `["Tesla", "Ford"]`.
- **Explicit `None` clears** — enables "drop the make filter" via LLM.

### Only `vector_search_narrative` filters flow into session state

Genie filters are embedded in the NL question, not a structured bag —
nothing sensible to accumulate. `fetch_tsb` / `fetch_investigation`
are point lookups; their "filter" is the id itself, which doesn't
generalise. So: only the VS tool's filters merge into session state.

### Tool turns are persisted inside the assistant turn, not as
standalone rows

A single user turn triggers N tool calls + 1 assistant reply. We
store:
- 1 row: `role="user"`, `content=user_message`
- 1 row: `role="assistant"`, `content=answer`,
  `tool_calls=tool_trace`

The design doc mentions `role="tool"` as a valid role — we keep the
enum (so a future change can split tool turns out) but the current
writer coalesces them. Reason: the LLM never re-reads prior tool turns
(tool-call IDs don't match across requests), so storing them
separately would be write-amplification for no read-side benefit.

### `serving.py` is model-as-code, not a container

MLflow 3's `set_model(...)` pattern lets us log the source file as the
model artifact. No Dockerfile, no base-image rebuild — deployment is
`mlflow.pyfunc.log_model(python_model=...)` from the notebook, then
`databricks-agents deploy` in Phase 6.

We guard heavyweight imports (psycopg, databricks.sdk, VectorSearchClient)
behind `load_context` so MLflow's validation step — which imports the
file on a lean worker — doesn't have to satisfy every runtime dep.

### ScriptedLLM + InMemorySessionStore = tests without a workspace

`test_agent_routing.py` uses a `ScriptedLLM` that returns a fixed
sequence of `chat.completions.create` responses. This pins down:
- tool selection (we assert the call sequence),
- max-step termination,
- malformed-args resilience,
- filter accumulation across turns,
- system-prompt + accumulated-filters message ordering.

No Databricks SDK, no network, no mocks of internal classes — just
dataclass fakes that match the `openai.OpenAI` response shape we
actually depend on.

### Fetch tools hit silver, not gold

`fetch_tsb` reads `silver_tsbs_parsed`; `fetch_investigation` reads
`silver_investigations_parsed`. Gold carries only the summarised fact
columns; silver has the full body + parsed structure. The agent needs
the body for citations, so silver it is.

If the user ever asks "show me the gold row for TSB X" we'd add a
third fetch tool that hits gold — but no prompt in the eval set
requires it.

### SQL interpolation for id lookups — not parameterised, but safe

`_sql_quote` single-quote-escapes + length-caps (≤ 64 chars) the NHTSA
id before building the literal SQL. Reasons:

- Both real SQL execution paths (`spark.sql`, statement-execution) take
  a literal string — no driver-level parameter binding available.
- NHTSA ids are short ASCII tokens (`14V123000`, `PE22-017`).
- The id comes from the LLM, which got it from the user. We still
  validate it at the boundary rather than trust.

If we ever add a third SQL-based tool that takes free-text input, we
revisit — parameterised execution is doable via prepared-statement
APIs and is worth the ceremony for narrative text.

## Running it

### Locally (unit tests)

```
uv sync --extra ci
uv run --extra ci pytest tests/
```

93 tests, all Spark-free, no Databricks workspace required.

### On Databricks

```
databricks bundle deploy -t dev

# Phase 4 one-time:
databricks bundle run -t dev lakebase_setup_job   # runs 4.2 once

# Phase 4 scheduled smoke:
databricks bundle run -t dev agent_smoke_job      # runs 4.1 in-memory

# Or run notebook 4.1 interactively:
# - open notebooks/4.1_agent_local.py
# - widgets: env=dev, run_id=manual, use_lakebase=false|true
# - run all cells
# - confirm "Phase 4 local checkpoint: PASS"
```

## What's intentionally NOT in Phase 4

- **MLflow tracing** — Phase 5 decorates tool entry points with
  `@mlflow.trace` and exports to `gold_agent_traces`. We instrument
  at the agent layer (per-turn) rather than at the tool layer, so
  each trace reflects one user interaction.
- **Eval harness** — Phase 5 builds the 3-tier eval set
  (deterministic / grounded / synthesis) and the runner.
- **Serving-endpoint deployment** — Phase 6 via `databricks-agents` +
  `register_agent_workflow.yml`.
- **Parallel tool fan-out** — measured, then optimised, in Phase 5.
- **Multi-tenant auth on Lakebase** — current scheme is one service
  principal; row-level security is a Phase 6 concern when the agent
  serves external users.
- **Connection pooling on `PostgresSessionStore`** — no latency
  pressure yet.

## Open questions to validate during first run

1. **Databricks LLM endpoint tool-calling fidelity** — Llama 4
   Maverick (`cfg.llm_endpoint` in dev) supports the OpenAI tools
   schema, but edge cases around parallel tool calls differ from
   GPT-class models. If notebook 4.1 routes fewer than 4/5 questions
   correctly, the first remedy is sharpening the tool `description`
   fields in `mcp.py::tool_specs` — not tweaking the system prompt.
2. **Genie conversation SDK shape** — `WorkspaceClient().genie` is
   new; `start_conversation_and_wait` sometimes returns a dict, sometimes
   an object depending on SDK version. `_run_genie` handles both but
   we should nail this down on first live run and remove whichever
   branch isn't hit.
3. **Lakebase credential TTL** — `generate_database_credential`
   returns a token with a finite TTL (typically 1h). Long-running
   notebooks may see auth failures mid-session. If that happens,
   wrap `_conn_factory` with a credential cache that refreshes on
   401 rather than every call.
4. **System prompt → accumulated_filters coupling** — the prompt says
   "use conversation history to maintain continuity", but we *also*
   inject an explicit system message with the filter bag every turn.
   If the LLM starts stacking filters unexpectedly (e.g. keeping
   `make_norm=Tesla` after the user clearly moved to Ford), tighten
   the wording in `_build_messages` so the injected message is
   advisory, not authoritative.
5. **Tool-trace payload size in Lakebase** — we store the full
   tool_trace in the `agent_turns.tool_calls` JSONB. A turn with 3
   tools each returning 50 VS hits can be ~100 KB. JSONB handles
   this but queries over the column get slow past a few MB. If the
   ops dashboard starts lagging, start truncating `result_preview`
   server-side or move full results to a separate table keyed by
   `(session_id, turn_idx, tool_idx)`.

## Test count progression

| Phase | New tests | Cumulative | Notes |
|-------|-----------|------------|-------|
| 0     | —         | 0          | scaffolding only |
| 1     | 17        | 17         | http + flat-files |
| 2     | 15        | 32         | chunking + taxonomy + pii |
| 3     | 15        | 47         | VS + Genie helpers |
| 4     | 46        | **93**     | mcp + memory + agent routing |

Phase 4 is disproportionately heavy on tests because the agent loop
has more branches per LOC than any other module — tool dispatch,
max-steps, malformed-args, multi-turn filter accumulation, history
windowing, role enforcement. Every one of those branches is a place a
live agent can misbehave silently, so we pin them down in unit tests
before they can surface on a Databricks workspace.
