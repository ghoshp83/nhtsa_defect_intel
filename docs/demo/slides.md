# NHTSA Defect Intelligence Agent — Presentation Content

Three-slide deck. Copy each `## Slide X` section into its own PPT page.
Render [architecture.mmd](architecture.mmd) at https://mermaid.live and paste the image into Slide 2.

---

## Slide 1 — What this agent does

**Title**: NHTSA Defect Intelligence Agent
**Subtitle**: An agentic assistant over 10 M+ U.S. vehicle-defect records

**One-liner**
A single chat surface that joins structured NHTSA fact tables (recalls, complaints, investigations, SGO AV crashes, TSBs) with the free-text narratives behind them, so analysts can ask cross-corpus questions in English and get cited, traceable answers.

**Who uses it**
- OEM quality teams — "how do our recall patterns compare to peers?"
- Regulatory analysts — "what themes are emerging in 2024 ADAS complaints?"
- Safety journalists / researchers — "what did NHTSA do about phantom braking?"
- Ops / reliability engineers — component-level trend spotting.

**Example interaction (multi-turn)**
> *User*: Describe the common complaint patterns for ADAS lane-keep assist nuisance activations across OEMs.
> *Agent*: [cites 3 patterns across Honda / Toyota / Subaru / GM with 6 ODI IDs]
> *User*: Narrow that to Honda and Toyota only.
> *Agent*: [same frame, only the two OEMs, reusing the prior lane-keep context from Lakebase session memory]

**Four takeaways for the audience**
1. **Cross-corpus** — a single question can hit SQL *and* semantic search in parallel.
2. **Always cited** — every claim carries a campaign ID / ODI ID / TSB ID inline.
3. **Multi-turn stateful** — Lakebase (Postgres Projects API) holds session + accumulated filters.
4. **Production-graded** — Mosaic AI serving, MLflow auto-trace on every call, hourly LLM-judge scoring, Lakehouse monitoring dashboard.

---

## Slide 2 — Architecture

*(Insert the rendered `architecture.mmd` image here, full-width.)*

**Caption / talk track (2 sentences, goes under the diagram)**
Medallion ingestion (Bronze → Silver → Gold) feeds two retrieval surfaces — a narrow Genie space over a star schema, and a Vector Search index over narrative chunks — plus two UC functions for document lookup. A `ResponsesAgent` on Mosaic AI Agent Serving routes among those four tools, persists turns to a Lakebase Postgres project for session memory, and emits an MLflow trace per turn that an hourly evaluation job scores with three cheap heuristics plus three LLM-judge guidelines, surfaced on a Lakehouse dashboard.

**Numbers to cite on the slide (1 line each)**
- Data: 2.2 M recalls / 154 K investigations / 239 K complaints / 5.6 M TSBs / 1 970 SGO crashes in Silver.
- Retrieval: 350 K narrative chunks, `databricks-gte-large-en`, cosine top-8.
- Model: `databricks-llama-4-maverick` (dev), temp 0.2, 2 000 max tokens.
- Runtime: p95 latency 52.9 s, avg tool calls / trace > 1, judge pass rate 1.00 / 1.00 / 1.00.

---

## Slide 3 — Your Dataset, Your Agent, What You Did Differently

### Dataset & use case

**Domain / problem**: U.S. vehicle-defect intelligence. NHTSA is the only open, authoritative source for recalls, consumer complaints, open investigations, SGO autonomous-vehicle crash reports, and manufacturer TSBs — five feeds that together describe every safety signal on U.S. roads but are never queried jointly by any official tool.

**Why it's interesting for an AI agent**
- Five datasets, different schemas, different update cadences — a natural router problem.
- Every question is a *mix*: "how many" (structured) + "what kinds of" (narrative) + "what did the agency do" (document lookup). Single-tool RAG or text-to-SQL cannot answer these.
- Every answer needs a **citation** (NHTSA campaign #, ODI #, EA/PE #, TSB #) — that gives a crisp, testable evaluation target, not a vibe check.
- Public, large (~10 M rows in Silver), messy (free-text complaints, PDF TSBs and investigation case files).

**Unique data challenges**
- TSBs and investigation case files are **PDFs** — require `ai_parse_document` in Silver, chunking that preserves procedural context (800 tokens + 100 overlap vs 500/50 for abstracts).
- NHTSA split TSBs into 5-year ZIPs after the May 2024 MfrComms rewrite — ingestion has to walk a list, not a single URL.
- SGO publishes two cumulative CSVs (ADS and ADAS) at direct `static.nhtsa.gov` paths — the directory listing returns 403 to scripts.
- Complaint narratives still contain PII despite NHTSA's scrubber — we run a regex pass in Silver before indexing.
- Component codes and make/model strings are non-canonical — normalised via an in-repo taxonomy + vPIC-seeded dimension.

### Agent logic

**Tools (exactly four — narrow surface is intentional)**
1. `genie_recalls` — Managed MCP to a Genie space over the gold star schema (recalls / complaints / investigations facts + dim_vehicle / dim_component / dim_oem_group / dim_date). Used for counts, trends, rankings.
2. `vector_search_narrative` — Managed MCP to a Vector Search index over `gold_narrative_chunks` (complaints, TSBs, investigation documents, SGO narratives). Accepts metadata filters on `source_dataset`, `make_norm`, `model_year`, `component_group`, `oem_group`, date range.
3. `fetch_tsb` — UC function pulling one TSB's parsed body + applicable vehicles from an NHTSA item number.
4. `fetch_investigation` — UC function returning one investigation case file (type, status, documents, related recalls) from an action number.

**Workflow**
- System prompt enumerates the four tools and the intent→tool mapping explicitly — no tool invention.
- Agent is an `mlflow.pyfunc.ResponsesAgent` subclass; MLflow auto-traces every turn, tool call, and LLM span.
- Multi-turn memory in Lakebase (Postgres Projects API): `agent_sessions` + `agent_turns`, keyed by UUID `session_id`. Each turn merges new refinement filters into an `accumulated_filters` JSONB and passes them into subsequent tool calls.
- Parallel tool calls enabled — the system prompt actively encourages parallelisation for independent sub-questions.
- Output is a `ResponsesAgent` stream with citations inline; message items get a `msg_<uuid>` id that MLflow requires.

**Live demo (2-3 example queries — use the Review App on `nhtsa-agent-endpoint-dev-pg`)**
- *Q2* "What role have OTA 'phantom braking' fixes played in Tesla recalls 2022-2025?" — two parallel tool calls (Genie + VS); show the trace.
- *Q4* "Describe the common complaint patterns for ADAS lane-keep assist nuisance activations across OEMs." — pure synthesis across 4 OEMs.
- *Q5* (follow-up in the same chat) "Narrow that to Honda and Toyota only." — proves Lakebase session memory.

### What you did differently (vs. the reference arxiv agent)

**Data and domain**
- arxiv = one uniform corpus of abstracts; NHTSA = five heterogeneous feeds + PDFs. Required `ai_parse_document` + a normalisation taxonomy + a narrative unioner across four source datasets.
- Chunking tuned for dense procedural text: `chunk_size=800 / overlap=100` (reference was 500/50 — TSB steps lose meaning if split).
- Lowered temperature to **0.2** (arxiv: 0.7). Defect answers must be factual; no creative paraphrasing of remedies.
- Top-K raised to **8** (arxiv: 5) — defect themes usually need more evidence to triangulate.

**Agent and evaluation**
- **Three-tier eval set** (not one flat list): Tier-1 deterministic (exact scalar match, 25 Qs), Tier-2 citation-grounded (the agent must surface a specific NHTSA ID, 25 Qs), Tier-3 synthesis (5-point rubric: faithfulness / coverage / calibration / citation / conciseness, 25 Qs). Pass threshold 3.5 on Tier 3.
- **Custom cheap scorers** `cite_id_present`, `mentions_oem`, `word_count_under` run on every trace; **three Guidelines LLM judges** (`factual_defect`, `cite_every_claim`, `stays_in_scope`) run on a 10 % sample to control cost. Cheap scorers catch regressions fast; judges catch style drift.
- **Hourly rebuild** of `nhtsa_traces_aggregated_pg` via `update_and_evaluate_traces` job: reads new traces, runs scorers, `log_feedback`s each assessment back onto the trace, and republishes the view. Dashboard reads the view, never the raw table.

**Infrastructure**
- **Lakebase on the Projects/PostgresAPI** — the newer namespace, not the Database-Instance API. This meant MLflow's `DatabricksLakebase` resource class does not auto-grant; we use `pg_api.create_role` + SQL `GRANT`s on `dev_SPN` once, then inject `LAKEBASE_SP_*` env vars at deploy time. (Same pattern the reference `arxiv` project landed on — Projects is simply the way forward.)
- **Config ship path**: `project_config.yml` is shipped inside the model artifact via `code_paths` + a `__file__`-based fallback resolver, so the serving container finds it even with CWD = `/`.

**Challenges solved (top 3)**
1. *Serving-pod auth to Lakebase.* The built-in system SP has no Postgres role. Fix: reuse the existing `dev_SPN`, grant it USAGE + CRUD on the two session tables once, inject credentials via `agents.deploy(environment_vars={...})` using `{{secrets/dev_SPN/...}}` refs — no new SPN, no account-admin needed.
2. *Trace response column is VARIANT, not STRING.* `get_json_object(response, '$.output')` silently returned NULL for every trace and the Guidelines scorer hard-errored with "requires outputs". Fix: `CAST(response AS STRING)` before `get_json_object`, plus a pandas non-null filter before handing traces to `mlflow.genai.evaluate`.
3. *Judge values serialized differently than cheap scorers.* Cheap booleans store as plain `true`/`false`; Guidelines scorers store as JSON-quoted `"yes"`/`"no"` (5-char literal). The view CASE statements had to match `'"yes"'` / `'"no"'`, not `'Pass'` / `'Fail'` or `'true'`/`'false'`. Lesson: always `SELECT DISTINCT a.name, a.feedback.value, typeof(a.feedback.value)` before writing assessment SQL.

---

## Appendix — demo questions (for the speaker, not the slide)

1. *Opener* — How many recall campaigns were issued for Ford in 2024?
2. What role have OTA 'phantom braking' fixes played in Tesla recalls 2022-2025?
3. What are the dominant themes in SGO reports tagged 'Level 2' ADAS 2024?
4. Describe the common complaint patterns for ADAS lane-keep assist nuisance activations across OEMs.
5. *(same chat)* Narrow that to Honda and Toyota only.
6. What defect patterns appear in school-bus recalls since 2020?
7. Summarise the current state of NHTSA engagement with rear cross-traffic alert defects.
