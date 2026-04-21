# 07 · Build Roadmap

Phased, incremental delivery. Each phase produces a runnable artifact
and ends with a checkpoint we can demo before moving to the next phase.

## Phase 0 — Scaffolding + design docs *(in progress)*

**Goal**: project skeleton ready for code, with all design decisions
captured in writing.

Deliverables:
- [x] Directory layout (`src/`, `notebooks/`, `resources/`, `tests/`,
      `docs/`)
- [x] `pyproject.toml` mirroring the reference project's pinned deps
- [x] `databricks.yml` with `dev` / `acc` / `prd` targets
- [x] `project_config.yml` with system prompt + per-env config + NHTSA
      source URLs
- [x] `version.txt`, `eval_inputs.txt`
- [x] `README.md`
- [x] Eight design docs (`docs/00` through `docs/07`)

**Checkpoint**: a teammate can read the docs and understand the system
without reading any code.

---

## Phase 1 — Data ingestion (bronze)

**Goal**: NHTSA data lands in delta + UC volume, idempotently, every
day.

Deliverables:
- `src/nhtsa_curator/io/http.py` — resilient httpx client with
  tenacity retry + 5 RPS throttle.
- `src/nhtsa_curator/io/recalls.py` — recalls API + bulk CSV reader.
- `src/nhtsa_curator/io/complaints.py` — complaints API + bulk CSV
  reader.
- `src/nhtsa_curator/io/investigations.py` — investigations index +
  case-PDF downloader (writes to UC volume).
- `src/nhtsa_curator/io/tsbs.py` — TSB index + PDF downloader.
- `src/nhtsa_curator/io/sgo.py` — SGO AV crash CSV reader.
- `src/nhtsa_curator/bronze.py` — bronze writers (one per dataset)
  with `_ingested_at`, `_source_url`, `_raw` columns.
- Notebooks `1.1_recalls_ingestion.py` ... `1.5_sgo_ingestion.py` —
  idempotent ingestion entry points.
- `resources/ingestion_*_job.yml` for each dataset.
- Unit tests for HTTP retry + bronze schema.

**Checkpoint**: `databricks bundle run -t dev ingestion_recalls_job`
populates `bronze_recalls` end to end. A second run is a no-op.

---

## Phase 2 — Parsing + silver/gold modelling

**Goal**: PDFs become queryable, gold tables ready for both Genie and
Vector Search.

Deliverables:
- `src/nhtsa_curator/parsing.py` — `ai_parse_document` wrapper +
  chunker.
- `src/nhtsa_curator/silver.py` — typed silver writers, PII scrub,
  dedupe.
- `src/nhtsa_curator/gold.py` — fact-table + dim-table builders,
  narrative-chunks builder.
- `src/nhtsa_curator/normalise.py` — make/model + component taxonomy
  joins.
- Reference YAML: `src/nhtsa_curator/ref/oem_groups.yml`.
- Notebooks `2.1_parse_tsbs.py`, `2.2_parse_investigations.py`,
  `2.3_build_silver.py`, `2.4_build_gold.py`,
  `2.5_chunk_narratives.py`.
- `resources/parse_documents_job.yml`,
  `resources/build_gold_job.yml`.

**Checkpoint**: `gold_recalls_fact`, `gold_complaints_fact`,
`gold_investig_fact`, `dim_*`, and `gold_narrative_chunks` are all
populated and joinable. A simple SQL question against gold runs in
< 2 seconds at dev scale.

---

## Phase 3 — Vector Search + Genie space

**Goal**: both retrieval surfaces are live and tested individually.

Deliverables:
- `src/nhtsa_curator/vector_search.py` — index create + sync + query
  helpers (mirrors the reference project's pattern).
- Notebook `3.1_vector_index_setup.py` — creates the index from
  `gold_narrative_chunks`.
- Notebook `3.2_genie_space_setup.py` — instructions + DDL to create
  the Genie space (manual step + scripted verification).
- `resources/refresh_vector_index_job.yml` for incremental sync.
- A small smoke notebook `3.3_smoke_retrieval.py` that runs 10 hand-
  picked queries against each surface and records p50 latency + recall
  on a known answer set.

**Checkpoint**: vector search returns sensible results for at least 8
of 10 hand-picked queries; Genie answers at least 5 of 5 hand-picked
SQL questions correctly.

---

## Phase 4 — Custom agent + MCP tools + Lakebase memory

**Goal**: end-to-end agent runs locally and on a serving endpoint.

Deliverables:
- `src/nhtsa_curator/mcp.py` — Managed MCP wrappers for vector search
  + Genie (matches reference project's `mcp.py`).
- `src/nhtsa_curator/memory.py` — Lakebase session store
  (`session_id`, `turn_idx`, conversation, `accumulated_filters`).
- `src/nhtsa_curator/agent.py` — agent class implementing the four
  tools, the system prompt loader, and the tool-call loop.
- `src/nhtsa_curator/serving.py` — model-as-code wrapper for MLflow
  logging.
- Notebook `4.1_agent_local.py` — drive the agent in a notebook for
  iterative debugging.
- Notebook `4.2_lakebase_setup.py` — create Lakebase tables + indices.
- `tests/test_agent_routing.py` — unit tests for tool-routing
  decisions on a fixed set of inputs.

**Checkpoint**: 5 hand-picked end-to-end questions answered with
correct citations in a notebook session, with multi-turn context
maintained.

---

## Phase 5 — MLflow tracing + evaluation

**Goal**: every call is observable and the eval harness produces the
metrics we care about.

Deliverables:
- `mlflow.trace` decorators on every tool function.
- OpenTelemetry exporter configuration → `gold_agent_traces` delta
  table.
- `notebooks/eval/tier1_deterministic.tsv`,
  `tier2_grounded.tsv`, `tier3_synthesis.tsv` — initial eval sets
  (~25 questions per tier; expand in Phase 6).
- `src/nhtsa_curator/evaluation.py` — eval harness runnable from CLI
  and from a notebook; logs per-question + aggregate metrics to
  MLflow.
- Notebook `5.1_run_eval.py`.
- `resources/eval_workflow.yml`.

**Checkpoint**: `uv run python -m nhtsa_curator.evaluation --tier all
--target dev` produces a complete MLflow run with all aggregate
metrics + a per-question delta table.

---

## Phase 6 — Asset-bundle deployment + dashboard

**Goal**: hands-off deployment + a dashboard worth showing.

Deliverables:
- `resources/register_agent_workflow.yml` and
  `resources/deploy_endpoint_workflow.yml`.
- Champion / Challenger alias workflow.
- `notebooks/dashboard/01_emerging_themes.sql`,
  `02_oem_quality_metrics.sql`, `03_agent_ops_metrics.sql`.
- `resources/dashboard.yml` (Databricks SQL dashboard).
- Slack alert webhook integration.
- Eval set expanded to ~150 questions (50 per tier).
- README updated with screenshots + demo script.

**Checkpoint**: `databricks bundle deploy -t prd` deploys everything,
the endpoint serves the agent, the dashboard renders, the eval gate
runs on PRs, and rollback is one alias swap away.

---

## Time estimate (rough, per phase)

| Phase | Engineering days |
|-------|------------------|
| 0     | 1 (this turn)    |
| 1     | 3                |
| 2     | 4                |
| 3     | 2                |
| 4     | 4                |
| 5     | 3                |
| 6     | 3                |
| Total | ~20 days         |

These assume one person working in focused sessions and reusing
patterns from the reference arxiv project (which dramatically shortens
phases 1, 4, and 6).

## Suggested course-deliverable mapping

For the LLMOps course's weekly deliverables, this project can be cut
along the same notebook numbering as the reference project:

| Course week | Notebooks                  | Phase |
|-------------|----------------------------|-------|
| 1 (foundation models) | `1.x` ingestion notebooks | 1 |
| 2 (parsing + chunking) | `2.x` parsing + gold     | 2 |
| 3 (vector + genie + MCP) | `3.x` retrieval        | 3 |
| 4 (agents + tracing + eval) | `4.x` + `5.x`         | 4–5 |
| 5 (deployment) | `6.x` deployment + dashboard  | 6 |
