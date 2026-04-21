# 06 · Deployment Plan

## Targets

Three Databricks Asset Bundle targets, mirroring the reference project:

| Target | Workspace path                                 | Schedules | Catalog       | Purpose |
|--------|------------------------------------------------|-----------|---------------|---------|
| `dev`  | `/Workspace/Users/<user>/.bundle/dev/...`      | PAUSED    | `mlops_dev`   | Engineer's sandbox |
| `acc`  | `/Shared/.bundle/acc/...`                      | PAUSED    | `mlops_dev`   | Eval gates + integration |
| `prd`  | `/Shared/.bundle/prd/...`                      | UNPAUSED  | `llmops_dev`  | Production |

Promotion path: `dev` → `acc` → `prd`. Promotion is gated by the eval
metrics in [05 · Evaluation Strategy](05_evaluation_strategy.md).

## Asset bundle inventory

Each YAML in `resources/` declares one logical unit:

| File | Declares |
|---|---|
| `resources/ingestion_recalls_job.yml` | Daily job: pull recalls API → bronze → silver |
| `resources/ingestion_complaints_job.yml` | Daily job: pull complaints API → bronze → silver |
| `resources/ingestion_investigations_job.yml` | Daily job: pull investigations + download case PDFs |
| `resources/ingestion_tsbs_job.yml` | Daily job: pull TSB index + download bulletin PDFs |
| `resources/ingestion_sgo_job.yml` | Monthly job: pull SGO AV crash CSV |
| `resources/parse_documents_job.yml` | After-ingestion job: `ai_parse_document` on new PDFs |
| `resources/build_gold_job.yml` | After-parse job: build gold facts + chunk table |
| `resources/refresh_vector_index_job.yml` | After-gold job: incremental sync of vector index |
| `resources/refresh_genie_space_job.yml` | After-gold job: refresh Genie space metadata |
| `resources/register_agent_workflow.yml` | Workflow: log + register agent on PR merge |
| `resources/deploy_endpoint_workflow.yml` | Workflow: deploy / update Mosaic serving endpoint |
| `resources/eval_workflow.yml` | Workflow: run the eval harness against `acc` |
| `resources/dashboard.yml` | Databricks SQL dashboard definition |

## CI/CD flow

```
   Developer PR
       │
       ▼
   ┌──────────────────────────────────────────────────────┐
   │ GitHub Actions (or Databricks Workflows on PR)       │
   │  1. uv sync --extra ci                               │
   │  2. ruff lint + format check                         │
   │  3. pytest tests/                                    │
   │  4. databricks bundle validate -t dev                │
   │  5. databricks bundle deploy -t dev                  │
   │  6. Run smoke notebook on dev                        │
   └──────────────────────────────────────────────────────┘
       │
       ▼
   PR review + merge to main
       │
       ▼
   ┌──────────────────────────────────────────────────────┐
   │ On merge:                                            │
   │  1. databricks bundle deploy -t acc                  │
   │  2. Run eval workflow on acc                         │
   │  3. Compare metrics to current prd champion          │
   │  4. If passes gate → databricks bundle deploy -t prd │
   │  5. databricks bundle run register_agent_workflow    │
   │     on prd                                           │
   │  6. databricks bundle run deploy_endpoint_workflow   │
   │     on prd                                           │
   └──────────────────────────────────────────────────────┘
```

## Serving endpoint

- **Type**: Mosaic AI Agent serving endpoint (registered model behind
  AI Gateway).
- **Routing**: AI Gateway applies the same usage policy as the
  reference project (`usage_policy_id` in `project_config.yml`), giving
  us consistent rate limit / cost ceilings / content moderation.
- **Scaling**: scale-to-zero in `dev` and `acc`; min 1 instance in
  `prd` to avoid cold-start penalty during the smoke eval.
- **Versioning**: every promotion writes a new model version; previous
  Champion is retained for instant rollback (`mlflow alias` swap).

## Champion / Challenger

- New deployments register as **Challenger**.
- A 24-hour shadow window: the eval workflow runs against both
  Champion and Challenger on the smoke set hourly.
- If Challenger passes the gate at the end of 24h, alias `champion` is
  swapped to it.

## Rollback

Single command:

```
databricks alias models set <model_name> champion <previous_version>
```

The serving endpoint reads the `champion` alias on each model load, so
the rollback takes effect on next request without a redeploy.

## Cost guardrails

| Lever                      | dev                | acc                | prd                |
|----------------------------|--------------------|--------------------|--------------------|
| LLM endpoint               | maverick (small)   | maverick           | llama-3.1-70b      |
| Vector index size          | 90-day window      | 1-year window      | full corpus        |
| TSB PDF parse window       | 90 days            | 1 year             | full corpus        |
| Daily job runtime cap      | 30 min             | 60 min             | 120 min            |
| Endpoint min instances     | 0                  | 0                  | 1                  |
| AI Gateway monthly budget  | $50                | $200               | $1500              |

## Secrets

All secrets live in a Databricks secret scope `nhtsa-curator`:

- `service_principal_client_id`
- `service_principal_client_secret`
- `lakebase_password`
- `slack_alert_webhook_url`

Never commit secrets. Rotate on a 90-day cadence.

## Observability hooks

- MLflow autolog enabled at the agent class level.
- OpenTelemetry exporter configured to write to the
  `gold_agent_traces` delta table (sink path in `project_config.yml`).
- Slack webhook fired when:
  - Eval smoke score drops > 5 % week-over-week
  - Endpoint p95 latency > 12s for 15 minutes
  - Daily ingestion job fails twice in a row
