# 05 · Evaluation Strategy

The eval set has **three tiers**, each scored differently. Together they
prevent the two classic failure modes — "agent gets the right vibe but
the wrong number" and "agent gets the right number but ignores the
narrative".

## Tier 1 — Deterministic (exact-match)

~100 questions whose answer is a single number, list, or boolean
derivable from the gold fact tables. Built by writing the SQL query and
recording its result at a frozen delta version.

Examples:
- "How many recalls were issued for the component group 'Power Train'
  in 2024?"
- "List all OEMs that had open Engineering Analyses on EV battery
  thermal events as of 2025-12-31."
- "What is the total number of vehicles affected by Tesla recalls
  related to the Autopilot system since 2020?"

**Scoring**: exact-match against the recorded ground-truth value.
Partial credit only for list questions (Jaccard >= 0.8 = pass).

## Tier 2 — Citation-grounded (extraction)

~50 questions whose answer is a short factual statement that must be
backed by a specific source id.

Examples:
- "What was the remedy described in campaign 23V-085?"
- "Summarise the consequence section for ODI complaint 11543210."

**Scoring**:
- (a) Did the answer contain the correct source id? (binary)
- (b) Does the answer's claim match the cited source content? (LLM
  judge with rubric)
- Composite = (a) AND (b >= 4/5)

## Tier 3 — Judged synthesis (themes / narratives)

~50 open-ended questions that require synthesis across many narratives.

Examples:
- "What are the most common reported failure modes for L2 ADAS
  disengagements involving emergency braking on highways in 2025?"
- "Compare consumer complaints about transmission shifting between
  Honda and Toyota in model years 2022–2024."

**Scoring**: an LLM-judge with a 5-point rubric:
1. Faithfulness (claims supported by retrieved content)
2. Coverage (relevant themes captured)
3. Calibration (uncertainty correctly flagged)
4. Citation (every claim backed by an id)
5. Conciseness

Reference answers are written by us once; the judge compares the
agent's answer against the reference plus the retrieved chunks.

## Eval harness

- One MLflow experiment: `${env.experiment_name}`.
- Each eval run logs:
  - Dataset version (delta version id of `gold_*` tables).
  - System prompt hash.
  - Model + endpoint identifier.
  - Per-question: question, ground truth, agent answer, retrieved
    context, tool-call trace, score per metric.
- Aggregate metrics surfaced:
  - `tier1_exact_match_rate`
  - `tier2_grounded_pass_rate`
  - `tier3_avg_judge_score`
  - `mean_tool_calls_per_question`
  - `mean_latency_seconds`
  - `mean_token_cost_usd`

## Data files

Stored in `notebooks/eval/` as TSVs for reviewability:

```
notebooks/eval/
├── tier1_deterministic.tsv     # question \t reference_sql \t expected_value
├── tier2_grounded.tsv          # question \t source_id \t expected_claim
└── tier3_synthesis.tsv         # question \t reference_answer
```

A small CLI in `src/nhtsa_curator/evaluation.py`:

```
uv run python -m nhtsa_curator.evaluation \
    --tier all \
    --target dev \
    --experiment /Shared/nhtsa-curator-pg
```

## Promotion gate

Move from `acc` to `prd` only if **all** of the following hold versus
the current `prd` champion:

| Metric                      | Rule                |
|-----------------------------|---------------------|
| `tier1_exact_match_rate`    | >= champion         |
| `tier2_grounded_pass_rate`  | >= champion - 0.02  |
| `tier3_avg_judge_score`     | >= champion - 0.10  |
| `mean_latency_seconds`      | <= champion * 1.20  |

Failures of the gate produce a comment on the deployment PR with a
side-by-side diff of failing examples.

## Continuous monitoring

A scheduled job runs a 25-question **smoke eval** subset hourly against
the live `prd` endpoint and writes results to
`gold_agent_ops_metrics`. The dashboard shows the rolling 7-day score
and pages a Slack channel if it drops below threshold.
