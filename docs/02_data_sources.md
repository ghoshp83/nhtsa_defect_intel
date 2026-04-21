# 02 · Data Sources

All sources are **open** and require no credentials. NHTSA publishes
both REST APIs and bulk dumps; we use the API for incremental and the
bulk dump for the initial backfill.

## Source inventory

| # | Dataset                    | Format        | Volume / day | Notes |
|---|----------------------------|---------------|--------------|-------|
| 1 | Recalls                    | JSON + CSV bulk | ~10s of new rows | Has campaign_number primary key |
| 2 | Complaints (VOQ)           | JSON + TSV bulk | ~1k new rows    | Long free-text narrative field |
| 3 | Investigations (PE/EA/RQ)  | HTML index + PDF case files | ~1-5 new cases / week | Each case = many PDFs |
| 4 | SGO AV crash reports       | CSV + narrative | weekly batches | Mandatory under 49 CFR 579 |
| 5 | Technical Service Bulletins | PDF (one per bulletin) | ~50-500 / day | OEM-issued, NHTSA-indexed |

Below: where each dataset lives, what fields matter, and how we land it.

---

### 1. Recalls

- **API**: `https://api.nhtsa.gov/recalls/recallsByVehicle`
  (and friends — `recallsByCampaignNumber`, `recallsByCriteria`).
- **Bulk**: monthly flat-file dump on the NHTSA datasets page.
- **Primary key**: `NHTSACampaignNumber` (e.g., `24V-001`).
- **Key fields**:
  - `Manufacturer`, `Make`, `Model`, `ModelYear`
  - `Component` (raw text + numeric `ComponentID`)
  - `Summary` (free-text defect description)
  - `Consequence` (free-text)
  - `Remedy` (free-text)
  - `ReportReceivedDate`, `RecallTypeCode`
  - `PotentialNumberofUnitsAffected`
- **Refresh strategy**: daily incremental by `ReportReceivedDate`.
- **Bronze table**: `bronze_recalls` (one row per campaign x make x
  model x year — recalls can fan out across multiple vehicles).

### 2. Complaints (VOQ)

- **API**: `https://api.nhtsa.gov/complaints/complaintsByVehicle`.
- **Primary key**: `ODINumber` (Office of Defects Investigation #).
- **Key fields**:
  - `Make`, `Model`, `ModelYear`
  - `Components` (multi-valued)
  - `Summary` (long free-text — the actionable narrative)
  - `Crash`, `Fire`, `NumberOfInjuries`, `NumberOfDeaths` (booleans/ints)
  - `DateOfIncident`, `DateComplaintFiled`
  - `VehicleSpeed`, `MilesAtFailure`
- **Refresh strategy**: daily incremental by `DateComplaintFiled`.
- **Bronze table**: `bronze_complaints`.
- **PII risk**: narratives sometimes contain phone numbers / names;
  we regex-scrub in silver.

### 3. Investigations (PE / EA / RQ)

- **Index**: NHTSA "Defect Investigations" portal exposes a downloadable
  CSV/HTML list of all open + closed investigations.
- **Case files**: each investigation has zero-to-many PDF documents
  (initial filing, MFR responses, closing report).
- **Primary key**: `NHTSAActionNumber` (e.g., `EA22-002`).
- **Key fields**:
  - `InvestigationType` (PE / EA / RQ)
  - `Subject`, `Component`, `Make`, `Model`, `ModelYear`
  - `DateOpen`, `DateClose`, `Status`
  - `SummarySource` (URL to the underlying PDF set)
- **Refresh strategy**: weekly index pull + on-demand PDF fetch when a
  new action number appears.
- **Bronze tables**: `bronze_investigations` (case metadata),
  `bronze_investigation_documents` (per-PDF metadata + path in volume).

### 4. SGO AV crash reports

- **Index**: NHTSA Standing General Order page, monthly CSV dumps.
- **Primary key**: `Report ID` (synthetic per submission).
- **Key fields**:
  - Reporting entity (Tesla, Waymo, Cruise, etc.)
  - SAE level (L2, L4)
  - Operating area, time, weather
  - Crash narrative (free-text, often redacted)
  - Vehicle make / model / model year / VIN suffix
- **Refresh strategy**: monthly batch on the 5th.
- **Bronze table**: `bronze_sgo_crashes`.

### 5. TSBs (Technical Service Bulletins)

- **Index**: NHTSA "Manufacturer Communications" portal (CSV index of
  all TSBs filed with NHTSA).
- **Bulletin PDFs**: each row in the index links to a PDF.
- **Primary key**: `NHTSAItemNumber` + OEM-supplied `BulletinNumber`.
- **Key fields**:
  - `Manufacturer`, `Make`, `Model`, `ModelYear`
  - `BulletinDate`, `Component`
  - `Summary` (free-text, often a long narrative + procedural steps)
- **Refresh strategy**: daily index pull + parallel PDF download for new
  rows. Bulletins can be large (10–100 pages).
- **Bronze tables**: `bronze_tsb_index`, `bronze_tsb_documents` (path +
  bytes hash), and after parsing, `silver_tsb_parsed`.

---

## Reference / dimension data

| Table                  | Source                           | Purpose |
|------------------------|----------------------------------|---------|
| `ref_component_taxonomy` | NHTSA component code reference | Canonical join key across datasets |
| `ref_make_model`         | NHTSA vPIC API                 | Normalise Make / Model spelling |
| `ref_oem_group`          | hand-curated YAML              | Map Make -> OEM group (e.g., Audi -> VW Group) |

## Rate limits + politeness

- NHTSA APIs are public and ungated, but we throttle to **5 req/s** with
  jitter to be polite.
- PDFs are downloaded with `If-Modified-Since` headers where supported,
  and stored by content hash to avoid re-downloads.
- All HTTP calls are wrapped in `tenacity` with exponential backoff and
  a 5-attempt cap.

## Legal / licensing

NHTSA data is U.S. Government Work and not subject to copyright in the
United States (per 17 USC §105). TSBs are filed by manufacturers under
49 CFR §579.5 — the manufacturer retains copyright on the bulletin
content, but the NHTSA-filed copy is publicly accessible. We use them
strictly for analytical / informational purposes and do not redistribute
the raw PDFs from this project.
