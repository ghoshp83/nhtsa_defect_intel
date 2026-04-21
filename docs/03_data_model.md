# 03 · Data Model

We follow a **bronze / silver / gold** medallion pattern in Unity Catalog.

```
catalog: ${env.catalog}
schema:  ${env.schema}      (e.g. pralaygh_nhtsa)
volume:  ${env.volume}      (e.g. nhtsa_files — for raw PDFs)
```

## Bronze (raw, append-only)

| Table                          | Grain                           | Notes |
|--------------------------------|---------------------------------|-------|
| `bronze_recalls`               | one row per campaign x vehicle  | Raw API JSON kept in `_raw` column |
| `bronze_complaints`            | one row per ODINumber           | |
| `bronze_investigations`        | one row per NHTSAActionNumber   | Case-level metadata only |
| `bronze_investigation_documents` | one row per PDF in case      | `pdf_path` -> UC volume |
| `bronze_sgo_crashes`           | one row per Report ID           | |
| `bronze_tsb_index`             | one row per NHTSAItemNumber     | |
| `bronze_tsb_documents`         | one row per PDF                 | `pdf_path`, `pdf_sha256` |

Common columns on every bronze table:
- `_ingested_at TIMESTAMP`
- `_source_url STRING`
- `_raw STRING` (verbatim payload for debugging)

## Silver (typed, deduped, scrubbed)

| Table                          | Notes |
|--------------------------------|-------|
| `silver_recalls`               | Typed columns + canonical component_id |
| `silver_complaints`            | PII-scrubbed `summary_clean` + extracted booleans |
| `silver_investigations`        | + computed `days_open` |
| `silver_sgo_crashes`           | + canonical SAE level + reporting_entity_norm |
| `silver_tsb_parsed`            | output of `ai_parse_document` (one row per page or per logical section) |
| `silver_investigation_parsed`  | output of `ai_parse_document` (one row per logical section per PDF) |

Common columns:
- `make_norm`, `model_norm`, `model_year`
- `component_id` (FK to `ref_component_taxonomy`)
- `_source_id`, `_source_dataset`
- `_silver_at TIMESTAMP`

## Gold (analytical)

### Star schema for Genie

```
                   ┌────────────────────┐
                   │ dim_vehicle        │
                   │ (make, model, year │
                   │  vehicle_key)      │
                   └─────────┬──────────┘
                             │
   ┌─────────────────────────┼─────────────────────────┐
   ▼                         ▼                         ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐
│ gold_recalls_fact│ │ gold_complaints_  │ │ gold_investig_fact   │
│ (campaign, date, │ │ fact              │ │ (action_no, type,    │
│  units, comp_id, │ │ (odi, date, comp, │ │  status, comp_id,    │
│  vehicle_key)    │ │  crash, fire,     │ │  vehicle_key,        │
│                  │ │  inj, deaths,     │ │  date_open,          │
│                  │ │  vehicle_key)     │ │  date_close)         │
└──────────────────┘ └──────────────────┘ └──────────────────────┘
                             │
                             ▼
                   ┌────────────────────┐
                   │ dim_component      │
                   │ (component_id,     │
                   │  group, code, name)│
                   └────────────────────┘
                   ┌────────────────────┐
                   │ dim_oem_group      │
                   │ (oem_group_id,     │
                   │  parent_company)   │
                   └────────────────────┘
                   ┌────────────────────┐
                   │ dim_date           │
                   │ (date_key, year,   │
                   │  quarter, month)   │
                   └────────────────────┘
```

These five fact + dim tables are the **only** ones exposed to the Genie
space. Keeping the surface narrow is the single biggest lever on Genie
SQL quality.

### Narrative table (for Vector Search)

```
gold_narrative_chunks
─────────────────────
chunk_id            STRING  (PK; sha256 of source_id + chunk_idx)
source_dataset      STRING  ('complaints' | 'tsb' | 'investigation' | 'sgo')
source_id           STRING  (e.g. ODINumber, NHTSAItemNumber)
parent_doc_id       STRING  (PDF id where applicable)
chunk_idx           INT
content             STRING  (the chunk text)
make_norm           STRING
model_norm          STRING
model_year          INT
component_id        BIGINT  (nullable)
component_group     STRING
event_date          DATE
oem_group           STRING
embedding_model     STRING  (frozen at ingest time)
_chunked_at         TIMESTAMP
```

This is the **only** table the Vector Search index reads. Filters in
the index are the metadata columns above — make_norm, model_year,
component_group, event_date, source_dataset.

### Aggregates for the dashboard

```
gold_weekly_themes        -- top emerging clusters per week
gold_oem_quality_metrics  -- complaints, recalls, investigations per 100k vehicles
gold_agent_ops_metrics    -- agent latency, cost, eval score (joined from tracing)
```

## Reference tables

### `ref_component_taxonomy`

NHTSA component codes are hierarchical (e.g.,
`Engine and Engine Cooling : Engine : Engine Block`). We flatten:

| column          | example                              |
|-----------------|--------------------------------------|
| component_id    | 12345                                |
| component_code  | "ENG.ENG.BLK"                        |
| component_name  | "Engine Block"                       |
| component_group | "Engine and Engine Cooling"          |

### `ref_make_model`

Sourced from NHTSA vPIC. Used to normalise spellings
("CHEVY" -> "Chevrolet", "VW" -> "Volkswagen").

### `ref_oem_group`

Hand-curated YAML committed to the repo:

```yaml
- oem_group: "Volkswagen Group"
  makes: [Volkswagen, Audi, Porsche, Bentley, Lamborghini]
- oem_group: "Stellantis"
  makes: [Chrysler, Dodge, Jeep, Ram, Fiat, Alfa Romeo]
- oem_group: "Tesla"
  makes: [Tesla]
# ...
```

## Delta optimisation

| Table                       | Partition by      | Z-order on              |
|-----------------------------|-------------------|-------------------------|
| `bronze_*`                  | `_ingested_at` (date) | -                   |
| `silver_complaints`         | `event_date` (year)   | `make_norm, model_year` |
| `silver_tsb_parsed`         | `bulletin_year`       | `nhtsa_item_number`     |
| `gold_*_fact`               | `event_date` (year)   | `vehicle_key, component_id` |
| `gold_narrative_chunks`     | `event_date` (year)   | `source_dataset, component_id` |

VACUUM retention: 30 days on bronze, 90 days on silver, 180 days on gold.
