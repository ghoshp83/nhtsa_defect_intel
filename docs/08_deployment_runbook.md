# 08 — Deployment Runbook (End-to-End)

A step-by-step guide to standing the **NHTSA Defect Intelligence Agent**
up on a fresh Databricks workspace — from zero to a live serving
endpoint with the monitoring dashboard populated.

> **Audience**: anyone deploying this bundle for the first time, or
> re-running the full pipeline after a project_config.yml change.
> Expected wall-clock: ~3–4 hours for a dev deploy, most of which is
> waiting on schedules / index builds / gold table backfill.

## 0. Prerequisites

### Local tools

```bash
# UV (package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Databricks CLI v0.2+  (bundles require ≥ 0.220.0)
pip install --upgrade databricks-cli
databricks --version   # expect ≥ 0.220.0
```

### Workspace prerequisites

The target Databricks workspace must have, **before** you deploy:

| Asset                       | Why                                                  | How to get it |
|-----------------------------|------------------------------------------------------|---------------|
| Unity Catalog enabled       | Every table lives in UC                              | Workspace admin |
| Catalog + schema            | Default dev: `mlops_dev.pralaygh_nhtsa`              | `CREATE CATALOG` / `CREATE SCHEMA` if missing |
| UC volume                   | Landing zone for NHTSA flat-file ZIPs                | `CREATE VOLUME {catalog}.{schema}.nhtsa_files` |
| SQL Warehouse               | Serves Genie + dashboard                             | Create a Serverless warehouse; note its id |
| Vector Search endpoint      | Hosts the narrative index                            | UI → Compute → Vector Search → Create; name it `llmops_course_vs_endpoint` (or update cfg) |
| Model-serving LLM endpoint  | The agent's LLM + the Guidelines judge               | Default: `databricks-llama-4-maverick` (ships with Databricks Foundation Models) |
| Model-serving embedding ep  | Builds vector-search embeddings                      | Default: `databricks-gte-large-en` (ships with Foundation Models) |
| Lakebase project name       | Multi-turn session memory store                      | **Auto-provisioned** on first run of `lakebase_setup_job` — just pick a name (e.g. `nhtsa-agent-lakebase-pg`). No admin needed. |
| Usage-policy id (optional)  | Attaches spend guardrails to the serving endpoint    | UI → Settings → Usage Policies → Create; copy id |

### Secrets (OPTIONAL — only for production hardening)

The baseline deploy needs **no secret scope**. The serving container
uses its on-behalf-of-user credentials to talk to Lakebase.

If and when you want service-principal auth for production (e.g.
the endpoint is serving requests outside a single user's session
context), create a scope and add three env vars to
`deploy_agent.py`:

```bash
databricks secrets create-scope nhtsa-curator
databricks secrets put-secret nhtsa-curator client-id
databricks secrets put-secret nhtsa-curator client-secret
# then in deploy_agent.py environment_vars add:
#   "LAKEBASE_SP_CLIENT_ID": "{{secrets/nhtsa-curator/client-id}}",
#   "LAKEBASE_SP_CLIENT_SECRET": "{{secrets/nhtsa-curator/client-secret}}",
#   "LAKEBASE_SP_HOST": "<workspace host>",
```

`_build_store` branches on those env vars — if they're set it uses
SPN auth; otherwise it falls back to the container's default auth.

### Populate `project_config.yml`

Open [project_config.yml](../project_config.yml) and replace the
following placeholders for every env you intend to deploy:

```yaml
dev:
  catalog:                 <your uc catalog>
  schema:                  <your uc schema>
  volume:                  <your uc volume>
  warehouse_id:            <sql warehouse id>
  vector_search_endpoint:  <vs endpoint name>
  genie_space_id:          PLACEHOLDER_DEV_GENIE_SPACE_ID   ← fill after step 4
  usage_policy_id:         <usage policy id or null>
  lakebase_project_id:     <lakebase project id>
  experiment_name:         /Shared/nhtsa-curator-<your initials>
```

> Genie space id stays `PLACEHOLDER_*` until step 4 — it's created
> interactively in the UI. `log_register_agent` skips the Genie
> resource entry while the placeholder is in place so dev deploys
> don't 403.

### Authenticate

```bash
# Profile-based auth (recommended)
databricks configure --profile nhtsa-dev
#   Host: https://dbc-xxxxxxxx.cloud.databricks.com/
#   PAT:  <your personal access token>

export DATABRICKS_CONFIG_PROFILE=nhtsa-dev
databricks current-user me    # sanity check
```

---

## 1. Deploy the bundle skeleton

```bash
cd nhtsa_defect_intel

# Install dev + ci deps (ci has pytest) + run the test suite
uv sync --all-extras
uv run pytest -o "addopts=" -q
# expect: 173 passed

# Build the wheel that all jobs depend on, then deploy
databricks bundle validate --target dev
databricks bundle deploy --target dev
```

What `bundle deploy` actually does:

1. Runs `uv build` (per the `artifacts.default` block in
   [databricks.yml](../databricks.yml)), producing `dist/*.whl`.
2. Uploads the wheel + every file under `notebooks/` +
   `resources/deployment_scripts/` to
   `/Workspace/Users/<you>/.bundle/dev/nhtsa-defect-intel/`.
3. Creates / updates every job, dashboard, and resource declared in
   `resources/*.yml` against the workspace. Schedules are created in
   **PAUSED** state in dev (per `schedule_pause_status: PAUSED`).
4. Syncs the files listed in `sync.include` — `eval_inputs.txt`,
   `project_config.yml`, `nhtsa_agent_pg.py` — so serving / eval
   jobs can read them as real files.

Sanity check in the workspace UI:

- **Workflows → Jobs** should list 11 jobs prefixed `nhtsa_*` +
  `nhtsa-*`.
- **Dashboards** should list `[dev] NHTSA Agent Monitoring Dashboard`
  (empty — will populate after step 7).
- **Workspace → Users → you → .bundle → dev → nhtsa-defect-intel**
  should contain `notebooks/`, `resources/`, `dist/*.whl`,
  `project_config.yml`.

---

## 2. Phase 1 — Ingest bronze flat files

Run the 5 ingestion jobs. They download the public NHTSA ZIPs,
land them in the volume, and write bronze Delta tables.

```bash
# Either trigger them individually (quick tests):
databricks bundle run ingestion_recalls_job --target dev
databricks bundle run ingestion_complaints_job --target dev
databricks bundle run ingestion_investigations_job --target dev
databricks bundle run ingestion_tsbs_job --target dev
databricks bundle run ingestion_sgo_job --target dev

# …or run them in parallel from the UI (Workflows → Run now).
```

Expected output per job:

| Source           | Bronze table                             | Rows (first run)    | Notes |
|------------------|------------------------------------------|---------------------|-------|
| Recalls          | `{cat}.{sch}.bronze_recalls`             | ~1.8M               | `FLAT_RCL_POST_2010.zip` (NHTSA renamed from `FLAT_RCL.zip`; pre-2010 campaigns dropped from the bulk dump) |
| Complaints       | `{cat}.{sch}.bronze_complaints`          | ~3.2M               | `FLAT_CMPL.zip` — largest |
| Investigations   | `{cat}.{sch}.bronze_investigations`      | ~100k               | `FLAT_INV.zip`; 11-col schema (NHTSA slimmed it post-2024) |
| TSBs             | `{cat}.{sch}.bronze_tsb_index`           | ~1M+                | 7 × `TSBS_RECEIVED_YYYY-YYYY.zip` chunks (NHTSA replaced the single `FLAT_TSBS.zip` with 5-year partitions in May 2024). `summary` field carries up to 4000 chars inline — no per-TSB PDF download phase anymore. |
| SGO AV crashes   | `{cat}.{sch}.bronze_sgo_av_crashes`      | ~2k–10k             | quarterly CSV |

Verify:

```sql
SELECT table_name, row_count
FROM system.information_schema.tables
WHERE table_schema = 'pralaygh_nhtsa'
  AND table_name LIKE 'bronze_%';
```

Troubleshooting:
- **HTTP 429 from static.nhtsa.gov** — the job retries once, then
  fails. Re-run after 10 minutes.
- **Volume not found** — create it: `CREATE VOLUME
  {catalog}.{schema}.nhtsa_files`.

---

## 3. Phase 2 — Silver + gold curation

### 3a. Silver (normalise + parse)

```bash
databricks bundle run silver_job --target dev
```

Six tasks run in parallel:

- `silver_recalls` — dedupe campaigns, canonical date cols
- `silver_complaints` — PII redaction (see `docs/03_data_model.md`)
- `silver_investigations` — normalised open/close dates
- `silver_tsbs` — index typing over `bronze_tsb_index` (MfrComms
  schema; NHTSA inlined bulletin text in `summary` so no PDF-parse
  stage exists anymore — gold narrative chunks reads `summary`
  directly)
- `silver_sgo` — SAE automation level normalisation
- `silver_investigations_parse` — `ai_parse_document` on investigation
  PDFs (max 100/run)

**Longest task is `silver_investigations_parse`** (≈ 20–30 min for
100 investigation PDFs; re-run until the pending queue drains).

### 3b. Gold (dimensions + facts + narrative chunks)

```bash
databricks bundle run gold_job --target dev
```

Two sequential tasks:

1. `gold_dimensions_facts` — builds `dim_vehicle`, `dim_component`,
   `dim_oem_group`, `dim_date`, `gold_recalls_fact`, `gold_complaints_fact`,
   `gold_investigations_fact`, `gold_tsb_meta`, `gold_sgo_av_crashes`.
2. `gold_narrative_chunks` — 800-token overlap-100 chunks across
   complaint narratives + TSB bodies + investigation docs into
   `gold_narrative_chunks`.

Verify:

```sql
SELECT COUNT(*) FROM {cat}.{sch}.gold_recalls_fact;          -- expect ~1.8M
SELECT COUNT(*) FROM {cat}.{sch}.gold_complaints_fact;       -- expect ~3.2M
SELECT COUNT(*) FROM {cat}.{sch}.gold_narrative_chunks;      -- expect ~4–8M
```

---

## 4. Phase 3 — Vector Search + Genie space

### 4a. Vector Search index

```bash
databricks bundle run refresh_vector_index_job --target dev
```

Two tasks:

1. `ensure_index` — `3.1_vector_index_setup.py`: creates the
   delta-sync index `{cat}.{sch}.nhtsa_narrative_index` against
   `gold_narrative_chunks` using the embedding endpoint from config.
   First-ever build takes ~20–40 min to reach `ONLINE` for a ~5M-row
   gold table.
2. `smoke_retrieval` — `3.3_smoke_retrieval.py`: runs 5 canned
   similarity queries and asserts each returns ≥ 1 hit.

While waiting, check index status:

```bash
databricks vector-search indexes get \
  --name {cat}.{sch}.nhtsa_narrative_index
# status.detailed_state == ONLINE_NO_PENDING_UPDATE when ready
```

### 4b. Genie space (manual, one-time)

Genie spaces **have no public DDL** — create them interactively:

1. Open the notebook [notebooks/3.2_genie_space_setup.py](../notebooks/3.2_genie_space_setup.py)
   and run it. It prints:
   - The exact table list to add.
   - The general-instructions text to paste.
   - The trusted-query seeds to add.
2. In the Databricks UI → **Genie** → **Create Space**:
   - Name: `NHTSA Defect Intelligence (dev)`
   - Warehouse: your SQL warehouse id
   - Paste the tables / instructions / trusted queries from the
     notebook output.
3. Copy the space id from the URL (`/genie/rooms/<space_id>`) and
   update `project_config.yml`:
   ```yaml
   dev:
     genie_space_id: "01efabc123..."
   ```
4. Re-run `databricks bundle deploy --target dev` so the updated
   config is synced to `/Workspace/.../project_config.yml`.

> **Reference**: [docs/reference/genie_space.yml](reference/genie_space.yml)
> is a human-readable snapshot of what the space should contain. Keep
> it updated when the space changes.

---

## 5. Phase 4 — Lakebase memory

```bash
databricks bundle run lakebase_setup_job --target dev
```

One task: `4.2_lakebase_setup.py`. Three things, all idempotent:

1. **Auto-provisions** the Lakebase Postgres project (`get_project`
   → fall back to `create_project`) — needs no admin rights; the
   notebook user owns the project. Autoscale 1–4 CU, suspend after
   300s idle.
2. Creates `agent_sessions` + `agent_turns` tables via
   `PostgresSessionStore.init_schema()`.
3. Smoke-tests a round-trip (write a throwaway session, read it
   back, delete).

First run takes ~3–5 minutes (project creation). Subsequent runs
finish in seconds because the `get_project` path short-circuits.

Verify in the Databricks UI → **Postgres** → your project name →
Branches → default → Databases → `databricks_postgres` → Tables:
should list `agent_sessions` + `agent_turns`.

> This job is deliberately **un-scheduled** (one-time bootstrap per
> env). Re-run manually if `DDL_STATEMENTS` in
> [src/nhtsa_curator/memory.py](../src/nhtsa_curator/memory.py) changes.

---

## 6. Phase 5 — Evaluation (optional pre-deploy smoke)

Run the eval harness against a live agent-in-notebook **before**
deploying the serving endpoint. Catches Genie / VS misconfiguration
cheaply.

```bash
databricks bundle run eval_workflow --target dev \
  --params tier=all,use_judge=true
```

One task: `5.1_run_eval.py`. Runs all 3 tiers × 25 questions each
(75 questions total) against `NhtsaAgentModel` and logs metrics to
the MLflow experiment `/Shared/nhtsa-curator-<initials>`.

Expected outcomes (first full dev run):

| Tier | Metric                         | Target |
|------|--------------------------------|--------|
| 1    | `tier1_exact_match_rate`       | ≥ 0.7  |
| 2    | `tier2_grounded_pass_rate`     | ≥ 0.6  |
| 3    | `tier3_avg_judge_score`        | ≥ 3.5  |

> First runs usually fall short on Tier 2 — the citation regex
> sometimes misses a legitimate answer. Review per-question JSON
> under the MLflow run artifacts.

---

## 7. Phase 6 — Register, deploy, observe

### 7a. Register + deploy the agent

```bash
databricks bundle run register_deploy_agent --target dev
```

Two sequential tasks:

1. **`log_register_agent`** (`deployment_scripts/log_register_agent.py`)
   - Runs a quick `evaluate_agent` smoke (< 5 questions) to confirm
     Genie + VS are reachable.
   - Calls `mlflow.pyfunc.log_model` with the `nhtsa_agent_pg.py`
     entry point + enumerated UC resources (serving endpoints,
     warehouse, 6 gold tables, VS index, Genie space if configured).
   - `mlflow.register_model(f"{cat}.{sch}.nhtsa_agent_pg")` → new
     version (e.g. `7`).
   - Sets alias `latest-model` → version 7.
   - Passes the version through to the next task via
     `dbutils.jobs.taskValues`.

2. **`deploy_agent`** (`deployment_scripts/deploy_agent.py`)
   - Reads the `latest-model` alias.
   - Calls `databricks.agents.deploy(model_name=..., model_version=...,
     endpoint_name="nhtsa-agent-endpoint-dev-pg", scale_to_zero=True)`.
   - Injects env vars: `GIT_SHA`, `MODEL_VERSION`,
     `MODEL_SERVING_ENDPOINT_NAME`, plus the three secret refs.
   - Serving endpoint goes through `NOT_READY → READY` (~8–15 min on
     first deploy, ~2–5 min on re-deploys).

Verify:

```bash
databricks serving-endpoints get nhtsa-agent-endpoint-dev-pg
# state.ready == READY
```

### 7b. Seed traces (populate dashboard)

```bash
databricks workspace run \
  /Workspace/Users/<you>/.bundle/dev/nhtsa-defect-intel/files/notebooks/6.1_propagate_traces.py \
  --target dev \
  --params env=dev,run_label=first-demo-2026-04-18,sleep_seconds=2
```

Or trigger from the notebook UI — it has widgets for `env`,
`run_label`, `sleep_seconds`.

The notebook fires 30 shuffled NHTSA questions at the endpoint via
the `/responses` API. Each request stamps a unique `session_id` +
`request_id` into `custom_inputs`, which `NhtsaResponsesAgent`
propagates onto the MLflow trace. Takes ~2 minutes at
`sleep_seconds=2`.

### 7c. Aggregate + score traces

```bash
databricks bundle run update_traces_aggregated --target dev
```

One task: `update_traces_aggregated.py`. Does three things:

1. Finds traces for endpoint `nhtsa-agent-endpoint-dev-pg` that
   aren't yet scored.
2. Runs cheap scorers (`cite_id_present`, `word_count_under`,
   `mentions_oem`) on **all** of them.
3. Runs Guidelines judges (`factual_defect`, `cite_every_claim`,
   `stays_in_scope`) on a deterministic 10% sample (seed=42).
4. Rebuilds the view `{cat}.{sch}.nhtsa_traces_aggregated_pg` with
   per-trace metrics + NHTSA-specific tool span counts
   (`genie_call_count`, `vs_call_count`, `fetch_tsb_count`,
   `fetch_investigation_count`).

> This cron runs **hourly** once schedules are unpaused. Trigger it
> manually here so the dashboard has data immediately after the
> first demo run.

### 7d. Open the dashboard

```
Workspace → Dashboards → [dev] NHTSA Agent Monitoring Dashboard
```

Verify KPIs populate:

- `kpi_total_traces` — should show the ~30 questions from step 7b.
- `kpi_cite_rate` — `% of answers with a valid NHTSA defect id`.
- `kpi_oem_rate` — `% mentioning an OEM`.
- `kpi_p95_latency` — 95th percentile latency in milliseconds.

Time-series + tool-mix + judge-outcomes + trace-drilldown widgets
should all render (even if sparsely).

---

## 8. Turn on the schedules (when you want continuous operation)

All jobs deploy **paused** in dev. Unpause the ones you want to run
automatically:

```bash
# Bulk unpause per-env by flipping the variable
databricks bundle deploy --target dev \
  --var="schedule_pause_status=UNPAUSED"

# …or unpause just one job in the UI (Workflows → <job> → Triggers)
```

The default daily schedule (all UTC):

| Time  | Job                              | Depends on        |
|-------|----------------------------------|-------------------|
| 06:00 | `ingestion_recalls_job`          | —                 |
| 06:00 | `ingestion_complaints_job`       | —                 |
| 06:00 | `ingestion_investigations_job`   | —                 |
| 06:00 | `ingestion_tsbs_job`             | —                 |
| 06:00 | `ingestion_sgo_job`              | —                 |
| 11:00 | `silver_job`                     | ingestion (~5h drain buffer) |
| 14:00 | `gold_job`                       | silver (~3h drain buffer) |
| 16:00 | `refresh_vector_index_job`       | gold (~2h drain buffer) |
| 18:00 | `agent_smoke_job`                | VS (~2h buffer)   |
| 20:00 | `eval_workflow` **(Mondays only)** | agent smoke     |
| hourly| `update_traces_aggregated`       | live serving      |

Manually triggered (no schedule):
- `lakebase_setup_job` — bootstrap only
- `register_deploy_agent` — on demand

---

## 9. Promote dev → acc → prd

Three-target flow. **Promotion is gated on eval metrics**, not
calendar.

```bash
# 1. Promote config (Genie space id per env, warehouse id, etc.)
#    Edit project_config.yml under the target env block.

# 2. Deploy the bundle
databricks bundle deploy --target acc

# 3. Run all phases against acc to verify
databricks bundle run ingestion_recalls_job --target acc
# …etc for silver_job, gold_job, refresh_vector_index_job, lakebase_setup_job

# 4. Run eval — this is the gate
databricks bundle run eval_workflow --target acc \
  --params tier=all,use_judge=true
# Review MLflow metrics; abort promotion if regressions

# 5. Register + deploy
databricks bundle run register_deploy_agent --target acc

# 6. Manually set champion alias (human gate)
databricks registered-models set-alias \
  --full-name {cat}.{sch}.nhtsa_agent_pg \
  --alias champion \
  --version-num 7

# Repeat 1–6 for --target prd once acc is soaked for 48h+
```

> `log_register_agent` only ever sets `latest-model`. The `champion`
> alias is set by a human (or a future Phase 7 automated gate) after
> the eval delta is reviewed. Keeping those two aliases strictly
> separated preserves the ability to roll back without re-running
> the whole pipeline — see `docs/phase6_implementation.md`.

---

## 10. Rollback procedure

```bash
# 1. Figure out the previous good version
databricks registered-models alias \
  get --full-name {cat}.{sch}.nhtsa_agent_pg --alias champion
# returns version 6

# 2. Re-point latest-model at the last-known-good
databricks registered-models set-alias \
  --full-name {cat}.{sch}.nhtsa_agent_pg \
  --alias latest-model \
  --version-num 6

# 3. Re-deploy (deploy_agent.py reads latest-model)
databricks bundle run register_deploy_agent --target prd
#   NB: this will re-log_model first. If you want to skip logging,
#   run the deploy_agent.py notebook directly with
#   env=prd and it will pick up the pinned alias.

# 4. Revoke the bad version's aliases (optional hygiene)
databricks registered-models delete-alias \
  --full-name {cat}.{sch}.nhtsa_agent_pg \
  --alias latest-model \
  --version-num 7
```

---

## 11. Smoke-test the live endpoint from the CLI

```python
import os
from databricks.sdk import WorkspaceClient
from openai import OpenAI

ws = WorkspaceClient()
token = ws.tokens.create(lifetime_seconds=600).token_value

client = OpenAI(
    api_key=token,
    base_url=f"{ws.config.host}/serving-endpoints",
)

resp = client.responses.create(
    model="nhtsa-agent-endpoint-dev-pg",
    input=[{"role": "user", "content": "Summarise recall 23V123."}],
    extra_body={
        "custom_inputs": {
            "session_id": "smoke-001",
            "request_id": "req-smoke-001",
            "user_id": "cli-smoke",
        }
    },
)
print(resp.output_text)
```

Trace for this call should appear on the dashboard within one cron
tick (≤ 1h) or after a manual
`databricks bundle run update_traces_aggregated`.

---

## 12. Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `bundle deploy` fails with `whl not found` | `uv build` didn't produce `dist/*.whl` | Run `uv build` locally first; verify `dist/` |
| Silver job retries forever on `ai_parse_document` | Rate limit / quota on foundation-model endpoint | Lower `max_docs_per_run`; run multiple times |
| Gold narrative chunks table is empty | Silver investigations-parse / TSBs-parse pending queue never drained | Re-run those two silver tasks until queue size = 0 |
| VS index stuck in `PROVISIONING` | First build on large gold table; normal at > 20 min | Wait; index builds are long on first full rebuild |
| Eval harness returns all zeros on Tier 2 | Genie space id still `PLACEHOLDER_*` | Create Genie space (step 4b); update config; re-deploy |
| `agents.deploy` 403 on `DatabricksTable` | Missing resource on `log_model(resources=[...])` | `test_log_register_agent_resources_contain_all_gold_tables` catches this — re-run tests; add missing table to `agent.py::log_register_agent` |
| Endpoint READY but `/responses` returns 500 | Usually `NhtsaResponsesAgent` init failure — check serving logs | Endpoint Logs tab → last 200 lines; common: Lakebase password secret missing |
| Dashboard shows "—" for all KPIs | Aggregated view is empty | Run `update_traces_aggregated` manually; verify traces landed in MLflow first |
| `kpi_cite_rate` stays at 0 even with visible cited answers | Cite-ID regex missing a pattern | Check `evaluation.py::_CITE_ID_RE`; add source-id variant; re-run `update_traces_aggregated` |

---

## 13. Full deployment checklist (copy into your PR description)

```
Prereqs
  [ ] UC catalog + schema + volume created
  [ ] SQL warehouse id noted
  [ ] VS endpoint created (or existing)
  [ ] Lakebase project NAME chosen (auto-provisioned on first run)
  [ ] project_config.yml updated for target env

Deploy + data
  [ ] uv run pytest                              → 173 passed
  [ ] databricks bundle deploy --target <env>
  [ ] ingestion_recalls_job                      → bronze_recalls populated
  [ ] ingestion_complaints_job                   → bronze_complaints populated
  [ ] ingestion_investigations_job               → bronze_investigations populated
  [ ] ingestion_tsbs_job                         → bronze_tsb_index populated (7 chunks)
  [ ] ingestion_sgo_job                          → bronze_sgo_av_crashes populated
  [ ] silver_job                                 → all 6 silver tables populated
  [ ] gold_job                                   → facts + chunks populated
  [ ] refresh_vector_index_job                   → index ONLINE + smoke OK

Genie + memory
  [ ] Genie space created in UI
  [ ] project_config.yml.genie_space_id updated + re-deploy
  [ ] lakebase_setup_job                         → agent_sessions / agent_messages exist

Eval + deploy
  [ ] eval_workflow                              → tier1 ≥ 0.7, tier2 ≥ 0.6, tier3 ≥ 3.5
  [ ] register_deploy_agent                      → endpoint READY
  [ ] 6.1_propagate_traces                       → 30/30 questions succeed
  [ ] update_traces_aggregated                   → view rebuilt

Observability
  [ ] Dashboard KPIs populated (non-zero)
  [ ] Trace drilldown filterable by session_id
  [ ] Guidelines judge outcomes visible

Schedules (when ready)
  [ ] bundle deploy --var schedule_pause_status=UNPAUSED
  [ ] 24h soak — confirm hourly update_traces_aggregated runs clean

Promotion (acc/prd only)
  [ ] Repeat pipeline against acc; eval gate passes
  [ ] champion alias set manually
  [ ] Repeat against prd after 48h acc soak
```

---

## Appendix A — file / job cross-reference

| What you want to do | Where it lives |
|---------------------|----------------|
| Change the system prompt | [project_config.yml](../project_config.yml) → `system_prompt` |
| Change LLM temperature / max tokens | [project_config.yml](../project_config.yml) → `model_config` |
| Change chunk size / overlap | [project_config.yml](../project_config.yml) → `chunking` |
| Add a Genie trusted query | [notebooks/3.2_genie_space_setup.py](../notebooks/3.2_genie_space_setup.py) + [docs/reference/genie_space.yml](reference/genie_space.yml) + Genie UI |
| Add a new tool to the agent | [src/nhtsa_curator/mcp.py](../src/nhtsa_curator/mcp.py) + [agent.py::TOOLS](../src/nhtsa_curator/agent.py) + update `log_register_agent` resources + update `test_phase6.py` drift guards |
| Add a cheap scorer to hourly aggregation | [src/nhtsa_curator/evaluation.py](../src/nhtsa_curator/evaluation.py) + [resources/deployment_scripts/update_traces_aggregated.py](../resources/deployment_scripts/update_traces_aggregated.py) |
| Change the eval threshold | [notebooks/5.1_run_eval.py](../notebooks/5.1_run_eval.py) exit-check block |
| Change the dashboard | [resources/dashboard/nhtsa_agent_monitoring_dashboard.lvdash.json](../resources/dashboard/nhtsa_agent_monitoring_dashboard.lvdash.json) |
| Change a cron schedule | `resources/<job>.yml` → `schedule.quartz_cron_expression` |
| Change the serving endpoint name pattern | [resources/deployment_scripts/deploy_agent.py](../resources/deployment_scripts/deploy_agent.py) |

## Appendix B — environments at a glance

| Env | Catalog.schema               | Warehouse         | LLM endpoint                              | Scale-to-zero | Schedules |
|-----|------------------------------|-------------------|-------------------------------------------|---------------|-----------|
| dev | `mlops_dev.pralaygh_nhtsa`   | 96e26e80fcd91931  | `databricks-llama-4-maverick`             | yes           | PAUSED    |
| acc | `mlops_dev.pralaygh_nhtsa`   | 96e26e80fcd91931  | `databricks-llama-4-maverick`             | yes           | PAUSED    |
| prd | `llmops_dev.nhtsa`           | 7077f4cb0e616c62  | `databricks-meta-llama-3-1-70b-instruct`  | **no**        | UNPAUSED  |

---

Proceed to the [Phase 6 implementation notes](phase6_implementation.md)
for deeper design-decision context on the serving / observability
layer, or jump back to the [build roadmap](07_build_roadmap.md) for
the phase-by-phase plan.
