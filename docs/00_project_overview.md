# 00 · Project Overview

## Problem statement

A safety analyst, OEM quality engineer, journalist, or investor who wants
to understand vehicle defect trends today must:

1. Search the **NHTSA Recalls** portal by VIN / make / model / year.
2. Cross-reference the **Complaints** database for early warning signals.
3. Hunt for any open **Investigations** (PE / EA / RQ) on the components
   in question.
4. Pull **Technical Service Bulletins (TSBs)** that OEMs have issued to
   dealers — these are PDF, indexed only loosely.
5. For Level-2+ ADAS / AV concerns, also consult the
   **Standing General Order (SGO)** crash reports.

Each of these lives in a separate UI, with different schemas and
different free-text conventions. Cross-corpus questions
("emerging EV battery thermal events in 2025, by OEM, with related
investigations and TSBs") are effectively impossible without a custom
data pipeline.

## Solution

Build an agentic assistant on Databricks that:

- **Ingests** all five NHTSA datasets to delta tables on a daily schedule.
- **Parses** TSB and investigation PDFs with `ai_parse_document` so the
  narrative content becomes queryable.
- **Indexes** narratives + TSBs in a Databricks Vector Search index.
- **Curates** a small star schema for Genie so structured questions
  (counts, OEM rankings, time series) get exact answers.
- **Routes** user questions through a custom agent that picks the right
  tool(s), composes an answer, and cites source ids.
- **Persists** session state in Lakebase so multi-turn investigations
  ("now exclude Tesla", "narrow to model year >= 2023") work naturally.
- **Observes** every call via MLflow tracing + OpenTelemetry tracing
  tables, with an eval harness that runs against a curated gold set.
- **Deploys** the agent as a Mosaic serving endpoint behind the AI
  Gateway, governed by the same usage policy as the reference project.
- **Surfaces** emerging themes + agent ops metrics through a Databricks
  SQL dashboard.

## Who would use this

- **NHTSA / regulators** — early-warning surveillance.
- **OEM quality / safety teams** — cross-supplier, cross-platform
  emergence detection.
- **Journalists** — investigative leads ("which OEMs have unusual fire
  complaint clusters this quarter?").
- **Insurance / re-insurance underwriters** — model-year risk scoring.
- **Plaintiffs' / defense counsel** — case-relevant evidence gathering.

## Out of scope (initially)

- Telematics / connected-car streaming data (not open).
- VIN-level forecasting (would need proprietary fleet data).
- Non-US recall regimes (Transport Canada, JNCAP, KBA etc.) — easy
  follow-on, but excluded from MVP.
- Image / video analysis of crash photos.

## Success criteria for the MVP

| # | Criterion                                                              | Target |
|---|------------------------------------------------------------------------|--------|
| 1 | All five datasets land in bronze daily without manual intervention     | 100% over 7 days |
| 2 | TSB PDFs are parsed and chunked into vector index                      | >= 95% parse success |
| 3 | Agent passes deterministic eval set (count / lookup questions)         | >= 90% exact match |
| 4 | Agent passes judged eval set (narrative summarisation)                 | >= 4.0 / 5.0 LLM-judge |
| 5 | All agent calls produce MLflow traces with full tool-call lineage      | 100% |
| 6 | Dashboard renders top-10 emerging themes per week                      | 1-click refresh |

## Glossary

| Term       | Meaning                                                          |
|------------|------------------------------------------------------------------|
| Recall     | Mandatory safety fix issued under 49 CFR Part 573 / Part 577    |
| Campaign # | NHTSA-assigned recall id, e.g. `24V-001`                         |
| VOQ        | Vehicle Owner Questionnaire — the consumer complaint record      |
| PE / EA / RQ | Preliminary Evaluation / Engineering Analysis / Recall Query   |
| TSB        | Technical Service Bulletin — OEM-to-dealer service guidance      |
| SGO        | Standing General Order — mandatory AV / ADAS incident reporting  |
| DTC        | Diagnostic Trouble Code (OBD-II)                                 |
| MFR        | Manufacturer (NHTSA short form)                                  |
