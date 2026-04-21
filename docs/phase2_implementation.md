# Phase 2 — Implementation Notes

This document captures the *what* and *why* of the Phase 2 build —
silver typing + PII scrubbing, `ai_parse_document` over PDFs, and
the gold star-schema + narrative-chunk tables.

## What Phase 2 ships

```
src/nhtsa_curator/
├── pii.py                 (regex scrubber for VINs/phones/emails/digits)
├── chunking.py            (char-based sliding-window chunker)
├── taxonomy.py            (make → OEM-group + component normalisation)
├── silver.py              (5 silver writers, typed + scrubbed)
├── parsing.py             (ai_parse_document wrapper + tracker tables)
└── gold.py                (4 dims, 3 facts, gold_narrative_chunks)

resources/
├── oem_groups.yml         (OEM taxonomy + make-spelling aliases)
├── silver_job.yml         (6 parallel silver tasks, 11 UTC)
└── gold_job.yml           (dims+facts → narrative chunks, 14 UTC)

notebooks/
├── 2.1_silver_recalls.py
├── 2.2_silver_complaints.py            (PII scrub spot-check)
├── 2.3_silver_investigations.py
├── 2.4_silver_tsbs_parse.py            (silver index + ai_parse_document)
├── 2.5_silver_sgo.py
├── 2.6_silver_investigations_parse.py  (ai_parse_document on PDFs)
├── 2.7_gold_dimensions_facts.py
└── 2.8_gold_narrative_chunks.py        (Vector Search source table)

tests/
├── test_pii.py            (9 tests — categories + leave-alone cases)
├── test_chunking.py       (7 tests — bounds, overlap, paragraphs)
└── test_taxonomy.py       (7 tests — aliases, unknowns, components)
```

Test count is now 32 (up from 9 in Phase 1). All pass without Spark.

## Key design decisions

### Silver is CREATE OR REPLACE, not MERGE

Bronze is the source of truth and is ingested with MERGE-on-key —
nothing is ever lost. Silver is fully reproducible from bronze, so
silver writers do `mode("overwrite") + overwriteSchema=true` rather
than maintaining incremental delta state. This keeps the silver code
trivial: no partition pruning, no tombstone handling, no schema
migration ceremony when we add a new derived column.

The cost of the rewrite is small at NHTSA scale (~10M complaints,
~100k recalls), and it lets us iterate on the schema without
operational drama.

### PII scrubbing is regex-based and stable

The scrubber lives in pure Python (`pii.py`) so it can be unit-tested
without Spark. Replacement tokens (`[VIN]`, `[PHONE]`, `[EMAIL]`,
`[NUM]`) are stable strings so:

- Downstream readers can spot scrubbed fields (e.g. exclude them from
  fuzzy matching).
- Re-running silver yields byte-identical narratives — useful for
  consumer cache validation.

What we deliberately don't scrub: street addresses (too noisy for
regex without false positives), names (same — and the complaint
narrator often refers to themselves as "I"). If we ever need to ship
narratives publicly we'll add an LLM-based name detector in Phase 5.

### `ai_parse_document` is gated by a tracker table

`ai_parse_document(content)` runs on a billed inference endpoint.
We track parsed `doc_id`s in `silver_<dataset>_parsed_tracker` and
only invoke the function for unseen docs — re-running the parse
notebook is therefore a cheap no-op once the backlog is drained.

Per-run cap (`max_docs_per_run`, default 200 for TSBs / 100 for
investigations) keeps job runtime predictable. Backlog drains across
consecutive runs.

If `ai_parse_document` raises mid-batch the whole call fails; the
tracker isn't updated, so the next run picks up from where we left
off. We don't try to distinguish per-row failures inside the function
— the function either succeeds for the whole batch or the whole
batch retries.

### Gold star schema is deliberately tiny

Only 4 dims + 3 facts are exposed to Genie:

| Layer  | Tables                                                                |
|--------|-----------------------------------------------------------------------|
| dim    | `dim_vehicle`, `dim_component`, `dim_oem_group`, `dim_date`           |
| fact   | `gold_recalls_fact`, `gold_complaints_fact`, `gold_investigations_fact` |

Genie's text-to-SQL quality is inversely correlated with surface
area. By keeping joinable narrow tables behind `vehicle_key`,
`component_id`, `oem_group_id`, and `date_key` (all xxhash64
surrogates), we make the join graph obvious to the model.

SGO is intentionally NOT in the star schema — its column set is too
volatile across releases to model dimensionally. Analysts query it
directly via a flat silver table.

### Surrogate keys are deterministic xxhash64 digests

`vehicle_key = xxhash64(make_norm, model_norm, model_year)` (and so
on for component / oem_group). This means:

- A full rebuild produces the same keys as the prior run, so any
  external consumer of `vehicle_key` keeps working.
- Two facts can be joined to a dim by computing the key locally —
  no need to materialise the dim first to "issue" surrogate ids.
- We avoid auto-increment columns (which require Identity columns +
  ordered writes to be stable across rebuilds).

### Narrative chunks: one table, three sources

`gold_narrative_chunks` is the *only* table the Vector Search index
reads. Sources:

| `source_dataset` | Body field                                       |
|------------------|--------------------------------------------------|
| `complaints`     | `silver_complaints.narrative_clean` (PII-scrubbed) |
| `tsb`            | `silver_tsb_parsed.full_text` (joined to silver_tsbs) |
| `investigation`  | `silver_investigation_parsed.full_text` (joined to silver_investigations) |

Each chunk carries the metadata columns the agent will use as
filters: `make_norm`, `model_year`, `component_group`, `event_date`,
`source_dataset`, `oem_group`. The Vector Search index is filtered
on these in Phase 3.

`chunk_id = sha256(source_dataset || source_id || chunk_idx)` is
stable, so the index can be incrementally synced via CDF (which is
why we enable `delta.enableChangeDataFeed = true` on this table).

### Char-based chunking, not token-based

NHTSA narratives are short and bursty; token estimates are within
±25 % of char counts at this scale, which is good enough for
retrieval. Char-based chunking lets us avoid instantiating a
tokenizer per Spark task. Defaults: 800 chars / 100 overlap /
`\n\n` separator (configurable via `project_config.yml`).

The chunker prefers the *last* `\n\n` within each window so
paragraphs stay intact. Fall-back is a hard cut.

### Make normalisation: alias → canonical → OEM group

`taxonomy.py` does a two-step lookup:

1. **Alias resolution** — submitter shortcuts ("CHEVY" → "Chevrolet",
   "VW" → "Volkswagen") via `make_aliases` in `oem_groups.yml`.
2. **Group lookup** — canonical make → parent corporate group.

Unknown makes pass through to `make_norm` (title-cased) but receive
`oem_group = NULL`, so analysts can audit the gap via:

```sql
SELECT DISTINCT make_norm
FROM silver_complaints
WHERE oem_group IS NULL
ORDER BY 1;
```

OEM-group YAML is hand-curated — add new entries when NHTSA data
shows a make we don't recognise (typically EV startups or grey-market
imports).

## Running it

### Locally (unit tests)

```
uv sync --extra ci
uv run pytest tests/
```

32 tests, all Spark-free. Phase 2 added `test_pii.py` (9),
`test_chunking.py` (7), `test_taxonomy.py` (7) on top of Phase 1's 9.

### On Databricks

```
databricks bundle deploy -t dev

# Phase 1 → bronze (run these first; cron triggers them at 06–10 UTC)
databricks bundle run -t dev ingestion_recalls_job
databricks bundle run -t dev ingestion_complaints_job
databricks bundle run -t dev ingestion_investigations_job
databricks bundle run -t dev ingestion_tsbs_job
databricks bundle run -t dev ingestion_sgo_job

# Phase 2 → silver (cron 11 UTC) and gold (cron 14 UTC)
databricks bundle run -t dev silver_job
databricks bundle run -t dev gold_job
```

Once `gold_narrative_chunks` is populated, Phase 3 (Vector Search
index + Genie space) can begin.

## What's intentionally NOT in Phase 2

- Vector Search index sync — Phase 3.
- Genie space DDL + curation — Phase 3.
- Investigation per-case PDF scraper — separate Phase-2 follow-up;
  the parse notebook (2.6) gracefully no-ops until the bronze
  table exists.
- Aggregate gold tables (`gold_weekly_themes`,
  `gold_oem_quality_metrics`, `gold_agent_ops_metrics`) — Phase 6
  alongside the dashboard.
- `silver_tsb_parsed` schema migration tooling — when
  `ai_parse_document` returns a new struct shape, we'll
  `DROP + REBUILD` rather than maintain a column-add migration.

## Open questions to validate during first run

1. **`ai_parse_document` return schema** — we assume `parsed.text`,
   `parsed.pages`, `parsed.metadata`. If Databricks renames these,
   the parsed-table schema in `parsing.py` is the only thing to
   touch.
2. **SGO column names** — `silver_sgo` defaults assume snake_cased
   headers (`report_id`, `make`, `sae_automation_level`,
   `incident_date`). Confirm against the actual SGO file the bronze
   job ingested and update notebook 2.5's widgets if needed.
3. **OEM taxonomy coverage** — after the first silver run, query
   `SELECT DISTINCT make_norm WHERE oem_group IS NULL` and add any
   surfaced makes to `src/nhtsa_curator/ref/oem_groups.yml`. Common gaps will
   be EV startups (Lordstown, Fisker, etc.) and ultra-luxury
   marques (Pagani, Koenigsegg).
4. **PII scrubber recall** — sample 100 narratives via the
   spot-check cell in notebook 2.2 and confirm no obvious VIN /
   phone slips through. If it does, tighten `_VIN_RE` /
   `_PHONE_RE` in `pii.py`.
5. **Chunk-size sanity** — for a sample of TSBs, count chunks per
   doc (`SELECT source_id, count(*) FROM gold_narrative_chunks
   WHERE source_dataset='tsb' GROUP BY source_id ORDER BY 2 DESC
   LIMIT 20`). Outliers >100 chunks suggest a TSB whose
   `ai_parse_document` output wasn't broken into pages — worth a
   manual look.
