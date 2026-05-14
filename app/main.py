"""Streamlit dashboard + chat for the NHTSA Defect Intelligence agent.

Single-page layout for OEM safety / quality / compliance analysts:

1. Banner — what this app is and who it's for.
2. KPI strip — recall campaigns, vehicles affected, complaints (90d),
   open ODI investigations.
3. Recall activity by OEM (current year) — top-10 horizontal bar.
4. Trending complaint themes (last 90 days) — top-10 horizontal bar.
5. Recall volume — last 24 months — line chart.
6. Active investigations — sortable table.
7. Coming soon — TSB and SGO data (silver layer ready, gold pending).
8. Ask the analyst — chat against the agent serving endpoint.

Auth: every Databricks-side call (warehouse SQL + serving endpoint) goes
out under Pralay's PAT, injected as `DATABRICKS_TOKEN` via the App
resource binding in `resources/nhtsa_chat_app.yml`. The endpoint then
runs Genie / SQL as Pralay too (PAT injected via deploy_agent.py env
vars). See `memory/nhtsa_databricks_app_phase1.md` for the full
auth-rationale chain.

We deliberately do NOT use
`WorkspaceClient.serving_endpoints.get_open_ai_client()`: that helper
lives on `ServingEndpointsExt` in databricks-sdk 0.85.0, but the
Databricks Apps runtime preloads an older SDK whose `serving_endpoints`
is the bare `ServingEndpointsAPI` (no helper). Constructing
`OpenAI(...)` directly sidesteps the version skew.
"""

from __future__ import annotations

import os
import uuid

import altair as alt
import pandas as pd
import streamlit as st
from databricks.sdk import WorkspaceClient
from openai import OpenAI

ENDPOINT_NAME = os.environ.get("AGENT_ENDPOINT_NAME", "nhtsa-agent-endpoint-dev-pg")
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "f871f96e97724dca")
CATALOG_SCHEMA = os.environ.get("NHTSA_CATALOG_SCHEMA", "mlops_dev.pralaygh")


# ---------------------------------------------------------------------------
# Endpoint / chat helpers
# ---------------------------------------------------------------------------


def _build_openai_client() -> OpenAI:
    """Return an OpenAI client pointed at Databricks Model Serving."""
    ws = WorkspaceClient()
    auth_headers = ws.config.authenticate()
    bearer = auth_headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return OpenAI(
        base_url=f"{ws.config.host.rstrip('/')}/serving-endpoints",
        api_key=bearer,
    )


def _response_dump(response: object) -> dict:
    """Best-effort dict view of a Responses API result (for trace extraction)."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            return dump() or {}
        except Exception:
            pass
    if isinstance(response, dict):
        return response
    return {}


def _extract_custom_outputs(response: object) -> dict:
    """Walk the response dump for the message item's ``custom_outputs``.

    The agent emits trace metadata (tool_trace, n_llm_calls, etc.) on the
    message-item dict — see ``src/nhtsa_curator/serving.py``. The OpenAI
    SDK's pydantic models pass unknown fields through via ``model_extra``,
    so ``model_dump()`` preserves them.
    """
    raw = _response_dump(response)
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        co = item.get("custom_outputs")
        if isinstance(co, dict):
            return co
    # Fall back to top-level custom_outputs if the SDK collapsed extras
    # on individual items (older openai-python releases).
    top = raw.get("custom_outputs")
    return top if isinstance(top, dict) else {}


def _extract_answer(response: object) -> tuple[str, dict | None]:
    """Pull the assistant text out of a Responses API result.

    Returns ``(answer, raw_dump)``. ``raw_dump`` is non-None only when
    we couldn't find any text — the caller renders it for debugging.
    """
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip(), None

    parts: list[str] = []
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    for item in output or []:
        item_type = getattr(item, "type", None) or (
            item.get("type") if isinstance(item, dict) else None
        )
        if item_type != "message":
            continue
        content = getattr(item, "content", None) or (
            item.get("content") if isinstance(item, dict) else []
        )
        for part in content or []:
            part_type = getattr(part, "type", None) or (
                part.get("type") if isinstance(part, dict) else None
            )
            if part_type == "output_text":
                ptext = getattr(part, "text", None) or (
                    part.get("text") if isinstance(part, dict) else ""
                )
                if ptext:
                    parts.append(ptext)
    answer = "\n".join(parts).strip()
    if answer:
        return answer, None

    raw = None
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            raw = dump()
        except Exception:
            raw = None
    if raw is None and isinstance(response, dict):
        raw = response
    return "(no response)", raw


def _render_trace(custom_outputs: dict) -> None:
    """Render an inline expander showing the agent's reasoning trace.

    Each tool_trace entry comes from ``NhtsaAgent.run_turn`` and carries
    ``step``, ``name``, ``args``, ``result_preview``, ``latency_ms``,
    ``error``. We render them as a markdown list so users see how the
    agent decomposed the question into Genie SQL / vector search /
    fact lookups.
    """
    trace = custom_outputs.get("tool_trace") or []
    n_llm = custom_outputs.get("n_llm_calls")
    stopped = custom_outputs.get("stopped_reason")
    filters = custom_outputs.get("accumulated_filters") or {}

    summary = []
    if isinstance(n_llm, int):
        summary.append(f"{n_llm} LLM call{'s' if n_llm != 1 else ''}")
    if trace:
        summary.append(f"{len(trace)} tool call{'s' if len(trace) != 1 else ''}")
    if stopped and stopped != "ok":
        summary.append(f"stopped: `{stopped}`")
    label = "🔎 How I got this — " + (" · ".join(summary) if summary else "trace")

    with st.expander(label, expanded=False):
        if not trace:
            st.caption("No tools were called — the LLM answered directly.")
        for step in trace:
            name = step.get("name", "?")
            latency = step.get("latency_ms")
            latency_str = f"{latency} ms" if isinstance(latency, int) else "—"
            err = step.get("error")
            badge = " ⚠️" if err else ""
            st.markdown(f"**Step {step.get('step', '?')} · `{name}` · {latency_str}{badge}**")
            args = step.get("args") or {}
            if args:
                st.json(args, expanded=False)
            preview = step.get("result_preview") or ""
            if preview:
                st.code(str(preview)[:1500], language="text")
        if filters:
            st.markdown("**Session filters accumulated so far:**")
            st.json(filters, expanded=False)


def _identity_debug() -> dict:
    """SDK identity + auth_type for the sidebar debug panel."""
    info: dict = {}
    try:
        ws = WorkspaceClient()
        info["auth_type"] = ws.config.auth_type or "(unset)"
        try:
            info["user_name"] = ws.current_user.me().user_name
        except Exception as exc:
            info["user_name_error"] = str(exc)
        bearer = ws.config.authenticate().get("Authorization", "").removeprefix("Bearer ")
        if bearer:
            info["token_prefix"] = bearer[:6] + "…" + bearer[-4:]
        info["host"] = ws.config.host
        info["env_DATABRICKS_TOKEN_set"] = bool(os.environ.get("DATABRICKS_TOKEN"))
        info["env_DATABRICKS_AUTH_TYPE"] = os.environ.get(
            "DATABRICKS_AUTH_TYPE", "(unset)"
        )
        info["env_DATABRICKS_CLIENT_ID_set"] = bool(
            os.environ.get("DATABRICKS_CLIENT_ID")
        )
    except Exception as exc:
        info["error"] = str(exc)
    return info


# ---------------------------------------------------------------------------
# Warehouse SQL
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def _ws() -> WorkspaceClient:
    return WorkspaceClient()


@st.cache_data(ttl=300, show_spinner=False)
def _sql_df(sql: str) -> pd.DataFrame:
    """Execute a SQL statement against the configured warehouse.

    Cached for 5 minutes — the panel data is aggregate and doesn't need
    real-time refresh on every page interaction. Raises a ``RuntimeError``
    with the SQL state's error message if the statement failed or never
    completed within ``wait_timeout``, so the caller's try/except surfaces
    the real cause instead of a silent empty DataFrame.
    """
    ws = _ws()
    res = ws.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",
    )
    sr = getattr(res, "statement_response", None) or res

    state = getattr(getattr(sr, "status", None), "state", None)
    state_str = str(state) if state is not None else "UNKNOWN"
    if state_str.endswith("FAILED") or state_str.endswith("CANCELED"):
        err = getattr(getattr(sr, "status", None), "error", None)
        msg = getattr(err, "message", None) or "no error message"
        raise RuntimeError(f"warehouse statement {state_str}: {msg}")
    if not state_str.endswith("SUCCEEDED"):
        raise RuntimeError(
            f"warehouse statement did not complete in wait_timeout (state={state_str})"
        )

    manifest = getattr(sr, "manifest", None)
    cols = [c.name for c in manifest.schema.columns] if manifest else []
    data = getattr(getattr(sr, "result", None), "data_array", None) or []
    return pd.DataFrame(data, columns=cols)


def _sql_scalar(sql: str, default: object = 0) -> object:
    """Return the first cell of a single-row, single-column query."""
    df = _sql_df(sql)
    if df.empty or df.shape[1] == 0:
        return default
    val = df.iloc[0, 0]
    return default if val is None else val


# ---------------------------------------------------------------------------
# Page setup + banner
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="NHTSA Defect Intelligence",
    page_icon="🚗",
    layout="wide",
)

st.markdown(
    """
    # 🚗 NHTSA Defect Intelligence
    **Surface emerging vehicle defects across the U.S. fleet** — recalls,
    consumer complaints, and ODI investigations from NHTSA's open
    datasets, with an LLM analyst on call.

    *Built for OEM safety, quality, and compliance teams to spot trends,
    benchmark against peers, and respond to risk before it escalates.*
    """
)
st.divider()


# ---------------------------------------------------------------------------
# KPI strip
# ---------------------------------------------------------------------------

st.subheader("At a glance — last 12 months")

# Four small scalar queries instead of one nested-subquery SELECT —
# Databricks SQL result manifests for a no-FROM scalar-subquery query
# sometimes come back with an empty column list, leaving _sql_df with a
# silently-empty DataFrame. Per-metric queries also let a single failed
# table not blank-out the whole strip.
try:
    recalls = int(
        _sql_scalar(
            f"SELECT COUNT(DISTINCT campaign_number) "
            f"FROM {CATALOG_SCHEMA}.gold_recalls_fact "
            f"WHERE event_date >= DATE_SUB(CURRENT_DATE(), 365)"
        )
        or 0
    )
    units = float(
        _sql_scalar(
            f"SELECT COALESCE(SUM(units_affected), 0) "
            f"FROM {CATALOG_SCHEMA}.gold_recalls_fact "
            f"WHERE event_date >= DATE_SUB(CURRENT_DATE(), 365)"
        )
        or 0
    )
    complaints = int(
        _sql_scalar(
            f"SELECT COUNT(*) "
            f"FROM {CATALOG_SCHEMA}.gold_complaints_fact "
            f"WHERE event_date >= DATE_SUB(CURRENT_DATE(), 90)"
        )
        or 0
    )
    investigations = int(
        _sql_scalar(
            f"SELECT COUNT(*) "
            f"FROM {CATALOG_SCHEMA}.gold_investigations_fact "
            f"WHERE close_date IS NULL OR UPPER(status) = 'OPEN'"
        )
        or 0
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Recall campaigns", f"{recalls:,}")
    units_label = (
        f"{units / 1_000_000:.1f}M" if units >= 1_000_000 else f"{int(units):,}"
    )
    c2.metric("Vehicles affected", units_label)
    c3.metric("Complaints (90d)", f"{complaints:,}")
    c4.metric("Open investigations", f"{investigations:,}")
except Exception as exc:
    st.warning(f"KPI query failed: `{exc}`")

st.divider()


# ---------------------------------------------------------------------------
# Two-column: OEM bar + complaint themes bar
# ---------------------------------------------------------------------------

left, right = st.columns(2)

with left:
    st.subheader("Recall activity by OEM (current year)")
    try:
        df = _sql_df(
            f"""
            SELECT o.oem_group AS oem,
                   COUNT(DISTINCT r.campaign_number) AS recalls
            FROM {CATALOG_SCHEMA}.gold_recalls_fact r
            LEFT JOIN {CATALOG_SCHEMA}.dim_oem_group o
              ON r.oem_group_id = o.oem_group_id
            WHERE YEAR(r.event_date) = YEAR(CURRENT_DATE())
              AND o.oem_group IS NOT NULL
            GROUP BY o.oem_group
            ORDER BY recalls DESC
            LIMIT 10
            """
        )
        if not df.empty:
            df["recalls"] = (
                pd.to_numeric(df["recalls"], errors="coerce").fillna(0).astype(int)
            )
            chart = (
                alt.Chart(df)
                .mark_bar(color="#1f77b4")
                .encode(
                    x=alt.X("recalls:Q", title="Recall campaigns"),
                    y=alt.Y("oem:N", sort="-x", title=None),
                    tooltip=["oem", "recalls"],
                )
                .properties(height=320)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("No recalls recorded for the current year yet.")
    except Exception as exc:
        st.warning(f"OEM query failed: `{exc}`")

with right:
    st.subheader("Trending complaint themes (last 90 days)")
    try:
        df = _sql_df(
            f"""
            SELECT c.component_group AS component,
                   COUNT(*) AS complaints
            FROM {CATALOG_SCHEMA}.gold_complaints_fact co
            LEFT JOIN {CATALOG_SCHEMA}.dim_component c
              ON co.component_id = c.component_id
            WHERE co.event_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND c.component_group IS NOT NULL
            GROUP BY c.component_group
            ORDER BY complaints DESC
            LIMIT 10
            """
        )
        if not df.empty:
            df["complaints"] = (
                pd.to_numeric(df["complaints"], errors="coerce").fillna(0).astype(int)
            )
            chart = (
                alt.Chart(df)
                .mark_bar(color="#ff7f0e")
                .encode(
                    x=alt.X("complaints:Q", title="Complaints filed"),
                    y=alt.Y("component:N", sort="-x", title=None),
                    tooltip=["component", "complaints"],
                )
                .properties(height=320)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("No complaints recorded in the last 90 days.")
    except Exception as exc:
        st.warning(f"Complaints query failed: `{exc}`")

st.divider()


# ---------------------------------------------------------------------------
# Recall trend (line)
# ---------------------------------------------------------------------------

st.subheader("Recall volume — last 24 months")
try:
    df = _sql_df(
        f"""
        SELECT DATE_TRUNC('month', event_date) AS month,
               COUNT(DISTINCT campaign_number) AS recalls
        FROM {CATALOG_SCHEMA}.gold_recalls_fact
        WHERE event_date >= ADD_MONTHS(CURRENT_DATE(), -24)
        GROUP BY DATE_TRUNC('month', event_date)
        ORDER BY month
        """
    )
    if not df.empty:
        df["month"] = pd.to_datetime(df["month"])
        df["recalls"] = (
            pd.to_numeric(df["recalls"], errors="coerce").fillna(0).astype(int)
        )
        chart = (
            alt.Chart(df)
            .mark_line(point=True, color="#2ca02c", strokeWidth=2)
            .encode(
                x=alt.X("month:T", title="Month"),
                y=alt.Y("recalls:Q", title="Recall campaigns"),
                tooltip=[alt.Tooltip("month:T", title="Month"), "recalls"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)
    else:
        st.info("No recall trend data available.")
except Exception as exc:
    st.warning(f"Trend query failed: `{exc}`")

st.divider()


# ---------------------------------------------------------------------------
# Active investigations table
# ---------------------------------------------------------------------------

st.subheader("Active ODI investigations")
try:
    df = _sql_df(
        f"""
        SELECT i.nhtsa_action_number AS action_number,
               i.investigation_type AS type,
               i.status,
               i.days_open,
               o.oem_group AS oem,
               c.component_group AS component,
               i.event_date AS opened
        FROM {CATALOG_SCHEMA}.gold_investigations_fact i
        LEFT JOIN {CATALOG_SCHEMA}.dim_oem_group o
          ON i.oem_group_id = o.oem_group_id
        LEFT JOIN {CATALOG_SCHEMA}.dim_component c
          ON i.component_id = c.component_id
        WHERE i.close_date IS NULL OR UPPER(i.status) = 'OPEN'
        ORDER BY i.event_date DESC
        LIMIT 50
        """
    )
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No active investigations.")
except Exception as exc:
    st.warning(f"Investigations query failed: `{exc}`")

st.divider()


# ---------------------------------------------------------------------------
# Coming soon (TSBs + SGO)
# ---------------------------------------------------------------------------

st.subheader("Coming soon")
c_tsb, c_sgo = st.columns(2)
c_tsb.info(
    "📄 **Technical Service Bulletins (TSBs)** — silver layer ingested; "
    "gold rollups planned. Will surface OEM repair patterns and emerging "
    "fixes before they escalate to recalls."
)
c_sgo.info(
    "🤖 **SGO AV Crashes** — silver layer ingested; gold rollups planned. "
    "Will track autonomous-vehicle safety reporting under NHTSA's Standing "
    "General Order."
)

st.divider()


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

st.subheader("💬 Ask the analyst")
st.caption(
    f"Powered by `{ENDPOINT_NAME}` — backed by Genie SQL, vector search "
    "over recall narratives, and an LLM. Ask about specific campaigns, "
    "defect patterns across OEMs, or competitive comparisons."
)

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    # UUID format required — agent's PostgresSessionStore types
    # session_id as UUID and rejects free-form strings.
    st.session_state.session_id = str(uuid.uuid4())

st.caption(f"Session: `{st.session_state.session_id}`")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("custom_outputs"):
            _render_trace(msg["custom_outputs"])
        if msg.get("request_id"):
            st.caption(f"request_id: `{msg['request_id']}`")

prompt = st.chat_input("e.g. How many recall campaigns were issued for Ford in 2024?")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking…_")
        raw_dump: dict | None = None
        custom_outputs: dict = {}
        request_id = str(uuid.uuid4())
        try:
            client = _build_openai_client()
            response = client.responses.create(
                model=ENDPOINT_NAME,
                input=[{"role": "user", "content": prompt}],
                extra_body={
                    "custom_inputs": {
                        "session_id": st.session_state.session_id,
                        "request_id": request_id,
                    }
                },
            )
            answer, raw_dump = _extract_answer(response)
            custom_outputs = _extract_custom_outputs(response)
        except Exception as exc:
            answer = f"**Error calling `{ENDPOINT_NAME}`:** `{exc}`"

        placeholder.markdown(answer)
        if custom_outputs:
            _render_trace(custom_outputs)
        st.caption(f"request_id: `{request_id}`")
        if raw_dump is not None:
            with st.expander("Raw response (debug)"):
                st.json(raw_dump)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": answer,
                "request_id": request_id,
                "custom_outputs": custom_outputs,
            }
        )


# ---------------------------------------------------------------------------
# Sidebar — debug (collapsed by default)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### NHTSA Defect Intel")
    st.caption("Internal preview — Databricks workspace users only.")
    st.divider()
    with st.expander("Debug info", expanded=False):
        st.json(_identity_debug())
        st.caption(f"Warehouse: `{WAREHOUSE_ID}`")
        st.caption(f"Schema: `{CATALOG_SCHEMA}`")
