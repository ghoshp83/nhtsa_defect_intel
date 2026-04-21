# 01 · Architecture

## High-level diagram

```
                    ┌──────────────────────────────────────────────┐
                    │ NHTSA open data sources                      │
                    │  - Recalls API / bulk                        │
                    │  - Complaints API / bulk (VOQ)               │
                    │  - Investigations index + case PDFs          │
                    │  - SGO AV crash CSV + narratives             │
                    │  - TSB index + bulletin PDFs                 │
                    └──────────────────┬───────────────────────────┘
                                       │  scheduled batch
                                       ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Ingestion layer (notebooks/1.x_*.py + src/nhtsa_curator/io/)   │
   │  - Resilient HTTP client (httpx + tenacity)                    │
   │  - Bronze writers: append-only, raw JSON / PDF bytes           │
   └──────────────────┬─────────────────────────────────────────────┘
                      ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Parsing + transformation (notebooks/2.x_*.py)                  │
   │  - ai_parse_document on TSB + investigation PDFs               │
   │  - Component code normalisation (NHTSA component taxonomy)     │
   │  - Make/model normalisation (vPIC join)                        │
   │  - Silver: typed, deduplicated                                 │
   │  - Gold: star schema for Genie + narrative table for vector    │
   └──────────────────┬─────────────────────────────────────────────┘
                      ▼
   ┌──────────────────────────────────┬─────────────────────────────┐
   │ Vector Search index               │ Genie space                 │
   │  source: gold_narrative           │ tables: gold_recalls_fact   │
   │  embed:  databricks-gte-large-en  │         gold_complaints_fact│
   │  exposed via Managed MCP          │         gold_investig_fact  │
   │                                   │ exposed via Managed MCP     │
   └──────────────────┬────────────────┴────────────┬────────────────┘
                      ▼                              ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Agent (src/nhtsa_curator/agent.py)                             │
   │   tools: vector_search_narrative, genie_recalls,               │
   │          fetch_tsb, fetch_investigation                        │
   │   memory: Lakebase (per-session conversation + filters)        │
   │   model:  configurable LLM endpoint via AI Gateway             │
   │   tracing: mlflow.trace + OpenTelemetry tracing tables         │
   └──────────────────┬─────────────────────────────────────────────┘
                      ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Mosaic serving endpoint (logged + registered in UC via MLflow) │
   └──────────────────┬─────────────────────────────────────────────┘
                      ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ Tracing tables  ─►  Eval harness  ─►  SQL dashboard           │
   └────────────────────────────────────────────────────────────────┘
```

## Component responsibilities

### 1. Ingestion layer
- Idempotent. Re-running yesterday's job must produce the same bronze
  rows (use NHTSA's natural ids as merge keys, not row hashes).
- Writes raw payloads to bronze with an `ingested_at` timestamp.
- PDFs land in a UC volume (`/Volumes/<catalog>/<schema>/nhtsa_files/`)
  and bronze stores the path + source-system metadata.
- Failure handling: retry with backoff, then dead-letter to a
  `_quarantine` table for manual inspection.

### 2. Parsing + transformation
- `ai_parse_document` is invoked **only** for new PDFs (delta merge on
  document id). Avoid re-parsing on every run.
- Component codes are mapped to a canonical taxonomy table
  (`ref_component_taxonomy`) so cross-dataset joins work.
- Make / model are normalised via a small `ref_make_model` table seeded
  from NHTSA vPIC.
- Silver = typed + deduped. Gold = analytical schema (star).

### 3. Retrieval surfaces
- **Vector Search**: one index, source `gold_narrative_chunks`. Each
  row = one chunk + metadata (source dataset, source id, make, model,
  year, component, date). Filters are crucial for grounding.
- **Genie space**: a small star schema (3 fact tables + ~5 dimension
  tables) so Genie generates clean SQL. We keep the surface narrow on
  purpose — narrow Genie spaces produce far better SQL than wide ones.

### 4. Agent
- Implemented in `src/nhtsa_curator/agent.py` using the same
  `databricks-agents` SDK pattern as the reference project.
- Tool routing is **explicit** — the system prompt enumerates the four
  tools and what each is for, so the LLM does not invent tools.
- Lakebase persists `(session_id, turn_idx, role, content, tool_calls,
  filters)` so the next turn can pick up state.

### 5. Observability
- Every agent call is wrapped with `mlflow.trace` decorators on the
  tool boundary.
- A Lakehouse monitoring job joins the OTel tracing table with the
  evaluation results table to compute weekly quality dashboards.

### 6. Deployment
- Asset bundle (`databricks.yml` + `resources/*.yml`) deploys all
  notebooks + jobs + the registered model + the serving endpoint.
- Promotion model: `dev` -> `acc` (staging) -> `prd`. `acc` runs the
  eval harness; promotion to `prd` is gated on no regression.

## Cross-cutting concerns

- **Cost guardrails**: vector index uses `databricks-gte-large-en`
  (small dim, cheap). Agent uses a smaller model in `dev` (Maverick),
  larger only in `prd`.
- **PII**: NHTSA strips PII from the public dump, but consumer
  complaints can still contain names/contact details — we run a regex
  scrubber in silver before vector indexing.
- **Provenance**: every gold row carries `source_dataset`, `source_id`,
  `ingested_at`, `parsed_at` so the agent can cite originals.
- **Reproducibility**: each MLflow run logs a snapshot of
  `project_config.yml` + the gold table version (delta version id).
