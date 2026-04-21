# Phase 3 — Implementation Notes

This document captures the *what* and *why* of the Phase 3 build —
Vector Search endpoint + index, Genie space curation, and the
retrieval smoke harness.

## What Phase 3 ships

```
src/nhtsa_curator/
├── vector_search.py      (endpoint + delta-sync index + query helpers)
└── genie.py              (exposed-table contract, comment DDL, verification,
                           trusted-query seeds)

notebooks/
├── 3.1_vector_index_setup.py   (idempotent endpoint + index bootstrap)
├── 3.2_genie_space_setup.py    (comments + verification + UI recipe)
└── 3.3_smoke_retrieval.py      (10 hand-picked VS queries, p50/p95 latency)

resources/
└── refresh_vector_index_job.yml  (cron at 16 UTC: ensure + smoke)

docs/reference/
└── genie_space.yml               (declarative space config snapshot)

tests/
├── test_vector_search.py  (6 tests — fake-client, spec + search flatten)
└── test_genie.py          (9 tests — surface area, DDL, verification)
```

Test count is now **47** (up from 32 in Phase 2). All pass Spark-free.

## Key design decisions

### Managed embeddings over self-managed

The index is a **delta-sync index with managed embeddings**. We hand
Databricks a source Delta table name and an embedding-endpoint name;
the service tails Change Data Feed on the source and calls the
embedding endpoint itself.

Alternative (rejected): self-managed — we'd compute embeddings in a
Spark job and upsert vectors via the SDK. It's strictly more code
(batch-embed job + upsert + failure handling) for no win at NHTSA
scale. We only revisit this when we need a custom embedding model
not exposed as a Databricks endpoint.

### The source table's schema is the index contract

`gold_narrative_chunks` was designed in Phase 2 specifically to be
the index source:

- `chunk_id` (sha256) → index primary key. Stable across rebuilds.
- `content` → `embedding_source_column`. This is what gets embedded.
- `make_norm`, `model_year`, `component_group`, `source_dataset`,
  `event_date`, `oem_group` → filterable / returned metadata.

`delta.enableChangeDataFeed = true` is set on the source table (in
`gold.py`), which is what makes delta-sync possible. Notebook 3.1
asserts the CDF flag before creating the index — if it's off, the
index would build but not auto-refresh.

### `TRIGGERED` pipeline, not `CONTINUOUS`

NHTSA data refreshes daily. `CONTINUOUS` would keep a sync process
running 24/7 for what's effectively a one-pulse-per-day workload.
`TRIGGERED` lets the gold → refresh job cron be the pacemaker:
silver 11 UTC → gold 14 UTC → refresh 16 UTC.

### `ensure_*` helpers are idempotent, not re-create

`ensure_endpoint` and `ensure_index` **do not delete and re-create**
if the target exists — they reuse it. Changing the embedding model,
primary key, or source-column shape is a manual
`client.delete_index(...)` first, followed by a re-run. This is
deliberate: silent re-creates would cost embedding money and cause
a retrieval outage during the rebuild.

### Genie spaces have no public DDL — YAML + notebook is the contract

There's no `CREATE GENIE SPACE` API. What we can own programmatically:

1. The **list of tables** (`GENIE_TABLES` in `genie.py`) — exactly
   the 4 dims + 3 facts, enforced by a unit test.
2. **Table + column comments** — Genie reads these at SQL-generation
   time. This is the single biggest text-to-SQL quality lever. The
   comments curate grain ("one row per…") and units ("4-digit year"),
   both of which Genie hallucinates on otherwise.
3. **Verification** — `verify_genie_space(spark, cfg)` confirms every
   table in the contract exists and that `genie_space_id` has been
   set (not a placeholder).
4. **Trusted-query seeds** — 5 (question, SQL) pairs. Notebook 3.2
   prints them for paste-into-UI.

The space-id itself is stored in `project_config.yml` under
`<env>.genie_space_id`, set manually after first UI creation, then
committed. The same file is what wires the agent (Phase 4) to the
space.

### Small Genie surface area is a design principle

Only 4 dims + 3 facts are exposed. Every extra table widens the prompt
Genie builds and pushes the model toward join hallucinations. If an
analyst needs SGO or bronze, they go via SQL Warehouse directly —
not Genie. This is the same reasoning applied in Phase 2 when we
kept SGO out of the star schema.

### Smoke harness: 10 VS + 5 Genie (manual)

Phase 3's exit gate (per `docs/07_build_roadmap.md`):
- Vector Search: ≥ 8 of 10 hand-picked queries return a sensible
  top hit. Automated via `3.3_smoke_retrieval.py`.
- Genie: 5 of 5 trusted-query questions regenerate the seed SQL.
  Manual — paste each into the space UI, compare.

The VS smoke is automated because retrieval failures are easy to
assert on metadata (e.g. "query mentions Tesla → top hit should
have `make_norm == 'Tesla'`"). Genie's SQL output is harder to
string-compare reliably, so 5 is the tractable manual set.

### Latency is recorded, not gated

p50 + p95 retrieval latency is logged by the smoke notebook but
doesn't fail the run. At dev scale the index is small and latency
is ~50-200ms — the gate would be noise. We'll re-introduce a latency
SLO in Phase 5 alongside the eval harness once we have a baseline.

## Running it

### Locally (unit tests)

```
uv sync --extra ci
uv run --extra ci pytest tests/
```

47 tests, all Spark-free. Phase 3 added `test_vector_search.py` (6)
and `test_genie.py` (9) on top of Phase 2's 32.

### On Databricks

```
databricks bundle deploy -t dev

# Phase 1 → bronze (cron 06-10 UTC)
# Phase 2 → silver (11 UTC) + gold (14 UTC)

# Phase 3 → Vector Search + Genie
databricks bundle run -t dev refresh_vector_index_job   # 3.1 + 3.3

# Genie (one-time manual):
#  - open notebook 3.2, run cells 1 + 2 to apply comments + verify
#  - follow the UI recipe in cell 4 to create the space
#  - paste the space-id into project_config.yml under <env>.genie_space_id
#  - rerun 3.2 and confirm verify_genie_space().ok == True
```

Once both surfaces are live, Phase 4 (agent + MCP tools + Lakebase
memory) can begin.

## What's intentionally NOT in Phase 3

- Agent tool wrappers — Phase 4 (`mcp.py` wraps VS + Genie as MCP
  tools).
- Lakebase session memory — Phase 4.
- MLflow tracing on retrieval calls — Phase 5 (we decorate at the
  agent layer, not the VS helper, so traces are per-turn).
- Programmatic Genie space creation — not supported by the public
  SDK at the time of writing. Revisit when Databricks ships it.
- Incremental tuning of the trusted-query set — we seed with 5 and
  grow the set in Phase 5 based on eval-harness failures.

## Open questions to validate during first run

1. **Embedding-endpoint throughput** — initial sync of
   `gold_narrative_chunks` runs every row through
   `cfg.embedding_endpoint`. Watch the endpoint QPS; if it caps out,
   either move to a provisioned-throughput endpoint or downsample
   the complaint corpus in dev.
2. **Filter syntax** — confirm the `similarity_search` filter dict
   format matches what the VS SDK currently accepts
   (`{"make_norm": "Tesla"}` vs `{"make_norm =": "Tesla"}` — the
   SDK has historically accepted both). Smoke notebook 3.3 exercises
   both equality and no-filter cases.
3. **Genie `COMMENT ON` persistence** — some workspaces strip table
   comments when `CREATE OR REPLACE` overwrites the table. Phase 2's
   silver + gold both use overwrite mode, so comments may need to
   be re-applied after every silver/gold run. If so, move
   `apply_comments` into the gold job (`2.7`) or the refresh job.
4. **Trusted-query regression** — after the first Genie run, note
   which of the 5 seed questions Genie answers correctly without
   paste-in. If any fail, tighten the column comments on the tables
   involved (not the trusted query itself).
5. **Index size budget** — complaints alone are ~10M rows; even at
   800-char chunks with one chunk per complaint that's ~10M vectors
   at 1024 dims ≈ 40 GB of embeddings. Confirm the endpoint tier
   sizes correctly before prod.
