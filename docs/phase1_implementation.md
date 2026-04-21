# Phase 1 — Implementation Notes

This document captures the *what* and *why* of the Phase 1 build so we
don't have to re-derive decisions later.

## What Phase 1 ships

```
src/nhtsa_curator/
├── __init__.py            (version)
├── _typing.py             (Json type alias)
├── config.py              (ProjectConfig, NhtsaSourcesConfig, load_config, get_env)
├── bronze.py              (5 dataset writers, MERGE-on-key, provenance cols)
└── io/
    ├── __init__.py
    ├── http.py            (NhtsaHttpClient + TokenBucket + tenacity retries)
    ├── _flat_files.py     (NHTSA pipe-delimited column lists + parser)
    ├── recalls.py
    ├── complaints.py
    ├── investigations.py
    ├── tsbs.py
    └── sgo.py

notebooks/
├── 1.1_recalls_ingestion.py
├── 1.2_complaints_ingestion.py
├── 1.3_investigations_ingestion.py
├── 1.4_tsbs_ingestion.py
└── 1.5_sgo_ingestion.py

resources/
├── ingestion_recalls_job.yml
├── ingestion_complaints_job.yml
├── ingestion_investigations_job.yml
├── ingestion_tsbs_job.yml
└── ingestion_sgo_job.yml

tests/
├── test_basic.py
├── test_flat_files.py
└── test_http.py
```

## Key design decisions

### Bulk over API as the primary path

NHTSA exposes both REST APIs (per-make/model/year) and bulk flat-file
dumps (one ZIP per dataset, refreshed nightly). We use **bulk** for
ingestion because:

1. The bulk dump is the canonical, complete dataset; the API is best
   for spot lookups.
2. One HTTP request vs. tens of thousands — far less load on NHTSA.
3. Schema is stable (column order documented in the companion .txt).

The REST API helpers are still exposed (`fetch_recalls_by_vehicle`,
`fetch_complaints_by_vehicle`) for future use cases like "backfill
this newly-released make".

### Bronze keeps the raw payload

Every bronze row carries an `_raw` column with `json.dumps(row)` of
the parsed dict. This lets us:
- Reprocess silver from bronze without re-downloading.
- Investigate parsing bugs after the fact.
- Recover from a schema drift if NHTSA adds a new column.

### Idempotency via MERGE on natural keys

| Dataset        | Merge key                  |
|----------------|----------------------------|
| recalls        | `record_id`                |
| complaints     | `cmplid`                   |
| investigations | `nhtsa_action_number`      |
| tsb_index      | `tsb_id`                   |
| sgo_crashes    | configurable header (default `Report ID`) |

Re-running yesterday's job is a no-op (zero new inserts). NHTSA may
update existing rows; for now we **keep the first version** in bronze
and let silver track changes from the `_raw` column. This is a
deliberate choice — change tracking is a Phase-2 problem.

### TSB PDF download cadence

The TSB notebook downloads up to `max_pdfs_per_run` (default 500) new
PDFs per run. At 5 RPS that's ~100s of network time + however long
the actual PDF transfers take. We cap to keep the job's runtime
predictable; the index continues to grow but the PDF backfill is
amortised across many runs.

A tracking table `bronze_tsb_documents` records what's been downloaded
so we never re-fetch the same PDF.

### PII handling deferred to silver

Complaint narratives may contain VINs, phone numbers, names. Bronze
keeps them verbatim — they're useful for debugging and we control
access via UC table grants. Silver runs the regex scrubber that
populates `description_clean` for downstream consumption.

### HTTP client design

- Token-bucket throttle (default 5 RPS) shared by all calls in a
  process.
- Tenacity exponential backoff with jitter on 408 / 425 / 429 / 5xx.
- Custom `User-Agent` identifying the project + a contact email — NHTSA
  reaches out before they block, but only if they can identify us.
- Streaming downloads for large PDFs (no full-buffer in memory).

## Running it

### Locally (unit tests)

```
uv sync --extra dev
uv run pytest tests/
```

The unit tests cover:
- Flat-file parser tolerance (short rows, overflow, empty lines).
- HTTP token-bucket throttle math.
- HTTP retry-then-succeed and give-up-after-N behaviours.

No Spark / Databricks needed.

### On Databricks (single dataset)

```
databricks bundle deploy -t dev
databricks bundle run -t dev ingestion_recalls_job
```

### On Databricks (full Phase 1)

Run the five jobs in any order — they're independent. Once all five
are populated, Phase 2 can begin.

## What's intentionally NOT in Phase 1

- `ai_parse_document` calls — that's Phase 2.
- Silver / gold tables — Phase 2.
- Vector index sync — Phase 3.
- Genie space DDL — Phase 3.
- Per-investigation PDF scraping — deferred to Phase 2 alongside parsing.

## Open questions to validate during first run

1. Are NHTSA's bulk URLs in `project_config.yml` still current? They
   change infrequently but verify after the first failed run.
2. Encoding: we assume Latin-1 for flat files based on observed
   behaviour. If we see mojibake, switch to `cp1252`.
3. SGO column header for the merge key — confirm the exact spelling
   against the most recent monthly snapshot and update the
   `key_column` parameter in `1.5_sgo_ingestion.py` if needed.
