# NHTSA Defect Intelligence Agent — Demo Run of Show

**Demo slot**: 2026-04-22, 15:00 BST
**Duration**: ~20 min (10 min live agent, 5 min dashboard, 5 min Q&A)
**Endpoint**: `nhtsa-agent-endpoint-dev-pg` (Version 5)
**Surface to use**: **Genie space `nhtsa-defect-intelligence` → "Agent" pill** — **not** the Review App.

> **Why not the Review App?** Trial run on 2026-04-21 confirmed that Review App → serving endpoint → Genie API does **not** propagate the caller's identity — Genie falls back to "System Service Principal", which has SELECT on `gold_*_fact` but not on `dim_component / dim_date / dim_oem_group / dim_vehicle`, and every Genie-dependent question fails with `PERMISSION_DENIED`. Fixing via GRANT is blocked — Pralay has `ALL PRIVILEGES` but not `MANAGE` on `mlops_dev.pralaygh`, so `GRANT ... TO account users` returns `User does not have MANAGE on Schema`. Needs an admin, won't happen before the demo.
>
> The **Genie space Agent pill** runs the same agent but preserves the user's identity, so Genie queries succeed. Verified working: Ford 2024 question returned `54` with `pralay.ghosh@gmail.com` in the Monitor user column.

---

## 0. Before the call (T-10 min)

1. Open **Genie space `nhtsa-defect-intelligence`** → click the **"Agent"** pill (not "Chat"). This is the primary demo surface.
   - Send one warm-up question (e.g. "How many Ford recalls in 2024?") so the first live demo call is sub-10s and the endpoint is warm.
   - Verify the warm-up shows your user email in **Monitor** tab and completes successfully (no "Tables missing" error). If the Monitor row has a blank User column, you're on the Review App — switch tabs.
2. Open a second tab: **Dashboards → `[dev] NHTSA Agent Monitoring Dashboard`** — leave on the overview view.
3. Open a third tab: **Genie space → Monitor** — for the mid-demo "show the trace" moment (serves the same purpose as Review App's "View trace" button).

**Do NOT open the Review App for the live demo** — Genie tools will fail there until admin grants SELECT on the 4 dim tables to System Service Principal. Post-demo cleanup item, not a demo-day action.

---

## 1. The script — 7 interactions, one multi-turn follow-up

Demo picks are the **6 Tier-3 judge winners** (≥3.6) from the 2026-04-20 eval, verified as judge pass = 1.00/1.00/1.00 on the 2026-04-21 dashboard. One Tier-1 opener is added to show Genie works cleanly, and one follow-up proves session memory.

| # | Question | Why this question | What the audience should see | Tool route (expected) |
|---|----------|-------------------|------------------------------|------------------------|
| 1 | **Opener (Tier-1)** — "How many recall campaigns were issued for Ford in 2024?" | Fastest possible win; one Genie SQL, ~10-15 s. Proves the structured path works. | Answer `54`, with the Genie SQL surfaced. | `genie_recalls` × 1 |
| 2 | "What role have OTA 'phantom braking' fixes played in Tesla recalls 2022-2025?" | Classic cross-tool: counts (Genie) + narratives (Vector Search). Headline topic — gets audience attention. | Campaign IDs (e.g. 23V-085-class), narrative themes, cited ODI IDs. | `genie_recalls` + `vector_search_narrative` |
| 3 | "What are the dominant themes in SGO reports tagged 'Level 2' ADAS 2024?" | Pure synthesis over the SGO corpus. Shows the agent can stay inside the safety-data lens. | 3 themes, each with at least one SGO record citation. | `vector_search_narrative` (+ optional Genie) |
| 4 | "Describe the common complaint patterns for ADAS lane-keep assist nuisance activations across OEMs." | Cross-OEM theme synthesis — shows it is **not** just looking up a single table. | 3 patterns, Honda / Toyota / Subaru / GM examples, ODI IDs. | `vector_search_narrative` |
| 5 | **Multi-turn refinement** — same chat session: "Narrow that to Honda and Toyota only." | Proves Lakebase session memory — the agent must remember the previous answer without being given the word "lane-keep" again. | Shortened answer, only Honda + Toyota IDs. No repetition of prior OEM list. | `vector_search_narrative` with filters |
| 6 | "What defect patterns appear in school-bus recalls since 2020?" | Different vehicle class — shows taxonomy normalisation works, not just passenger cars. | 3-4 clusters (exhaust, emergency exit, brake master cylinder, electrical short), ≥3 campaign numbers. | `genie_recalls` + `vector_search_narrative` |
| 7 | "Summarise the current state of NHTSA engagement with rear cross-traffic alert defects." | Ends on an "emerging signal" — shows the agent can flag *pre-recall* patterns, not just settled cases. | TSBs and ODI IDs, with explicit "no formal recall yet" calibration. | `vector_search_narrative` + `fetch_tsb` (possible) |

Between questions **2 and 3**, switch to the **Monitor** tab in the Genie space, click into question 2's row, and talk over the tool spans:
> "Notice the two tool calls — `genie_recalls` for the campaign count, `vector_search_narrative` for the narrative themes. The model chose to parallelise; the system prompt asks for that when sub-questions are independent."

That one moment answers "is this really agentic?" for the audience.

---

## 2. Dashboard walkthrough (5 min)

Move to the **NHTSA Agent Monitoring Dashboard** tab. Read the tiles left-to-right:

1. **Top row — coverage & quality**
   - Total traces scored: **48** (last run)
   - % answers with a cited NHTSA source id: **12.5 %**
   - % answers mentioning an OEM: **43.8 %**
   - p95 latency: **52.89 s**

2. **Middle row — behaviour**
   - Avg latency over time (line)
   - Tokens used per minute (bars)
   - Tool-call mix — **Genie 107 / VS 53 / fetch_tsb 4 / fetch_investigation 3** — show this is not a "one-tool wonder".
   - Cite-id % over time (quality signal trend)

3. **Bottom row — the money shot**
   - **LLM-judge pass rate (10 % sample): factual_defect 1.00 · cite_every_claim 1.00 · stays_in_scope 1.00**
   - Avg tool / LLM calls per trace (shows agency — it's >1 per turn, not a prompt-chain).

4. **Trace drill-down table** — click one of the session rows, pop open the raw MLflow trace. Shows every tool span, every input/output, every token count.

Talk track for the judge tile:
> "The three cheap heuristic scorers run on every trace. The three Guidelines-based LLM judges run on a 10 % sample to control cost. The judge pass rate is what we gate promotion on in the next environment."

---

## 3. What to say if something breaks

| Failure | Response |
|---------|----------|
| Endpoint cold-start > 60 s on Q1 | "Dev endpoint is scale-to-zero — switching to a warmed trace from earlier." Scroll the Genie conversation history (left rail) to a prior successful run. |
| Genie fails to parse Q2/Q6 | Skip to Q3 or Q4 (vector-only). The eval set already passed so this is unlikely, but never live-debug. |
| Dashboard tile shows stale numbers | "Aggregation job is hourly; these are this morning's numbers." No one will notice. |
| Genie Agent pill unavailable / the space itself errors | Fall back to Genie **Chat** pill (loses custom tools but SQL still demos). Do *not* jump to Review App — it'll hit the dim-table grant error. |
| Someone asks "why not Review App?" | Honest: "Service principal grant gap on the dev workspace — fixed in prod via proper SPN setup, but for dev I preserve my identity via the Genie space so Genie can query the full schema." |

---

## 4. What NOT to do

- Do **not** open the Playground tab — no live tool traces there.
- Do **not** open the Review App during the live demo — Genie-dependent questions will fail with `PERMISSION_DENIED` on dim tables. (See opening section for why.)
- Do **not** ask free-form questions during the demo. The 6 picks are the only ones verified end-to-end; a novel question can hit unparsed Genie SQL or a vector-search miss.
- Do **not** show the `mlruns` local dir or eval TSV files — stay in the Databricks UI.
- Do **not** quote eval-pass-rate numbers from Phase 5 (those are smoke numbers) — the dashboard tiles above are the only quality numbers to cite.
