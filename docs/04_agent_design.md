# 04 · Agent Design

## Goals

1. Answer cross-corpus questions about vehicle defects accurately.
2. **Always cite** source ids (campaign #, ODI #, action #, TSB item #).
3. Distinguish **hard facts** (counts, dates, OEMs) from **soft signals**
   (themes inferred from narrative text).
4. Support multi-turn refinement via Lakebase session memory.
5. Refuse / clarify on out-of-scope questions (e.g., legal advice,
   diagnostics for a specific VIN).

## Tool inventory

The agent is exposed exactly **four** tools — narrow on purpose, since
narrow tool surfaces produce far better LLM tool-routing decisions.

### `genie_recalls`
- **Type**: Managed MCP -> Genie space
- **Purpose**: structured SQL questions over the gold fact tables
- **Underlying tables**: `gold_recalls_fact`, `gold_complaints_fact`,
  `gold_investig_fact`, `dim_vehicle`, `dim_component`, `dim_oem_group`,
  `dim_date`
- **Input**: natural-language question
- **Output**: tabular result + the SQL Genie generated (we surface the
  SQL to the user as a citation)

### `vector_search_narrative`
- **Type**: Managed MCP -> Vector Search index
- **Purpose**: semantic retrieval over complaints + TSBs +
  investigation narratives + SGO crash narratives
- **Index source**: `gold_narrative_chunks`
- **Input**:
  - `query: str`
  - optional metadata filters: `source_dataset`, `make_norm`,
    `model_year`, `component_group`, `event_date_from`,
    `event_date_to`, `oem_group`
- **Output**: top-K chunks with content + metadata + score

### `fetch_tsb`
- **Type**: UC function
- **Purpose**: retrieve a single TSB's parsed content + metadata
- **Input**: `nhtsa_item_number: str`
- **Output**: `{summary, body, components, applicable_vehicles, pdf_url}`

### `fetch_investigation`
- **Type**: UC function
- **Purpose**: retrieve a single investigation case file
- **Input**: `nhtsa_action_number: str`
- **Output**: `{type, status, dates, components, summary,
  document_list, related_recalls}`

## System prompt (canonical)

Source of truth: `project_config.yml -> system_prompt`.

Key rules baked into the prompt:

- Always plan tool calls **before** answering.
- Prefer `genie_recalls` for any question that mentions counts, time
  ranges, OEM rankings, "how many", "top N".
- Prefer `vector_search_narrative` for "what kinds of issues",
  "themes", "examples", "find similar".
- Use `fetch_tsb` / `fetch_investigation` only when the user references
  a specific id or asks for full detail on one item.
- Cite every claim with the relevant id.
- If a tool returns zero rows, say so — never fabricate.

## Conversation + memory

State stored in **Lakebase** keyed by `session_id`:

| column           | type      |
|------------------|-----------|
| session_id       | UUID      |
| turn_idx         | INT       |
| role             | ENUM(user, assistant, tool) |
| content          | TEXT      |
| tool_calls       | JSONB     |
| accumulated_filters | JSONB  |
| created_at       | TIMESTAMP |

`accumulated_filters` is the agent-managed running state of refinements
(e.g. `{make: [Tesla, Rivian], model_year_from: 2023}`). Each turn the
agent merges new constraints into this object and passes the merged
filters to its tool calls.

## Decision flow

```
                     ┌────────────────┐
   User question ──▶ │ Agent (LLM)    │
                     └──────┬─────────┘
                            │ classify intent
            ┌───────────────┼─────────────────────────┐
            ▼               ▼                         ▼
     "structured?"     "exploratory?"        "specific id?"
            │               │                         │
            ▼               ▼                         ▼
     genie_recalls   vector_search_narrative    fetch_tsb /
                                                fetch_investigation
            └───────────────┴───────────┬─────────────┘
                                        ▼
                              ┌────────────────────┐
                              │ Synthesize answer  │
                              │ with citations     │
                              └────────┬───────────┘
                                       ▼
                              ┌────────────────────┐
                              │ Persist turn to    │
                              │ Lakebase           │
                              └────────┬───────────┘
                                       ▼
                              ┌────────────────────┐
                              │ Emit MLflow trace  │
                              └────────────────────┘
```

For complex questions (e.g., the worked example in the project pitch),
the agent runs **multiple tool calls in parallel** and then composes.
The `databricks-agents` SDK supports parallel tool calls; the system
prompt explicitly encourages this for independent sub-questions.

## Failure modes + handling

| Failure                                    | Handling |
|--------------------------------------------|----------|
| Genie SQL fails to compile                 | Retry with one re-prompt; on second failure, fall back to vector search and tell the user |
| Vector search returns zero hits            | Widen filters once; if still empty, report "no relevant narratives" |
| Fetch_tsb id not found                     | Return "not in our index" — do not invent content |
| LLM returns answer without tool calls      | Reject in post-processor and force a re-attempt |
| Tool latency > 30s                         | Cancel + return partial answer with a "incomplete due to timeout" flag |

## Out-of-scope handling

The system prompt instructs the agent to refuse:
- Legal advice ("am I entitled to a refund?")
- Per-VIN diagnostics ("why is my 2019 Civic stalling?") — instead,
  surface generally relevant TSBs/recalls and tell the user to consult
  a dealer.
- Future predictions ("will this car be recalled?") — only retrospective
  analysis is supported.
