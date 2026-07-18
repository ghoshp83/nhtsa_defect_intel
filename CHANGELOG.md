# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-07-18

### Added

- **CI gate** (`.github/workflows/ci.yml`): ruff lint + format check
  (pinned to the same ruff version as `.pre-commit-config.yaml`) and the
  full offline pytest suite, on every push and pull request. The repo
  previously had a 173-test suite and pre-commit hooks but nothing
  enforcing them server-side.
- First test coverage for the trace-side cheap scorers
  (`cite_id_present`, `word_count_under`, `mentions_oem`) and a
  dispatcher test pinning the `_latency_ms` every-path float contract.
  Suite: 173 → 189 tests.

### Fixed

- **Tier-2 citation gate scored 0% for complaints** (roadmap #3):
  `_cite_match`'s alphanumeric squash turned the agent's "ODI ID
  11512345" into `odiid11512345`, which can never contain ground truth
  `odi11512345`. Added a numeric-core fallback, guarded to ids with
  ≥ 6 digits so short cores (e.g. `23085` from `23V-085`) stay
  prefix-anchored.
- **`cite_id_present` scored 0% on real traces** (roadmap #7, regex
  half): the pattern required exactly "ODI 11512345" and had no
  bare-number branch, while the agent (and README) cite "ODI ID
  11512345" and bare 8-digit ODI/TSB ids. The regex now matches the
  agent's real citation vocabulary; the bare branch is exactly 8 digits
  so years and row counts don't false-positive.
- **Tool latency rendered as "—" for every call** (roadmap #6):
  `mcp.execute_tool` stamps `_latency_ms` as a float on every path, but
  the App's trace expander only accepted `int`. The renderer now
  accepts any numeric. (The roadmap's original diagnosis — missing
  stamping in `mcp.py` — was wrong; stamping was already universal.)
- Lint drift: 4 ruff errors and 7 unformatted files that accumulated
  with no CI to catch them.

## [0.0.1] - 2026-05-26

Initial public release: five-stream NHTSA ingestion (recalls,
complaints, ODI investigations, TSBs, SGO AV crashes), medallion
pipeline on Databricks, Vector Search + Genie tool surface, MLflow
tool-calling agent with textual-tool-call recovery, three-tier eval
harness, trace aggregation + dashboard, and the Streamlit App.
