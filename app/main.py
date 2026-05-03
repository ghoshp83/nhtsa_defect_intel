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
WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "96e26e80fcd91931")
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
    real-time refresh on every page interaction.
    """
    ws = _ws()
    res = ws.statement_execution.execute_statement(
        statement=sql,
        warehouse_id=WAREHOUSE_ID,
        wait_timeout="30s",
    )
    sr = getattr(res, "statement_response", None) or res
    manifest = getattr(sr, "manifest", None)
    cols = [c.name for c in manifest.schema.columns] if manifest else []
    data = getattr(getattr(sr, "result", None), "data_array", None) or []
    return pd.DataFrame(data, columns=cols)


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

try:
    kpis = _sql_df(
        f"""
        SELECT
          (SELECT COUNT(DISTINCT campaign_number)
             FROM {CATALOG_SCHEMA}.gold_recalls_fact
             WHERE event_date >= DATE_SUB(CURRENT_DATE(), 365)) AS recalls,
          (SELECT COALESCE(SUM(units_affected), 0)
             FROM {CATALOG_SCHEMA}.gold_recalls_fact
             WHERE event_date >= DATE_SUB(CURRENT_DATE(), 365)) AS units,
          (SELECT COUNT(*)
             FROM {CATALOG_SCHEMA}.gold_complaints_fact
             WHERE event_date >= DATE_SUB(CURRENT_DATE(), 90)) AS complaints,
          (SELECT COUNT(*)
             FROM {CATALOG_SCHEMA}.gold_investigations_fact
             WHERE close_date IS NULL OR UPPER(status) = 'OPEN') AS investigations
        """
    )
    if not kpis.empty:
        row = kpis.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Recall campaigns", f"{int(row['recalls'] or 0):,}")
        units = float(row["units"] or 0)
        units_label = (
            f"{units / 1_000_000:.1f}M" if units >= 1_000_000 else f"{int(units):,}"
        )
        c2.metric("Vehicles affected", units_label)
        c3.metric("Complaints (90d)", f"{int(row['complaints'] or 0):,}")
        c4.metric("Open investigations", f"{int(row['investigations'] or 0):,}")
    else:
        st.info("No KPI data returned — check warehouse access.")
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
        except Exception as exc:
            answer = f"**Error calling `{ENDPOINT_NAME}`:** `{exc}`"

        placeholder.markdown(answer)
        st.caption(f"request_id: `{request_id}`")
        if raw_dump is not None:
            with st.expander("Raw response (debug)"):
                st.json(raw_dump)
        st.session_state.messages.append(
            {"role": "assistant", "content": answer, "request_id": request_id}
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
