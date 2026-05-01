"""Streamlit chat UI for the NHTSA Defect Intelligence agent.

Phase 1 scope: render a chat input, POST each turn to the existing
`nhtsa-agent-endpoint-dev-pg` Mosaic AI Model Serving endpoint, and
display the assistant reply. No history persistence, no citations
panel, no multi-customer config — those are Phase 2.

Auth: when running as a Databricks App, the user's OAuth token is
forwarded in the `X-Forwarded-Access-Token` header. We use that token
to build the `WorkspaceClient` so the serving endpoint OBOs as the
real user — the App SPN identity would hit the dim-table grant gap
on `mlops_dev.pralaygh` in Pralay's workspace. When running locally
(`streamlit run`), `WorkspaceClient()` falls back to the default auth
chain (`DATABRICKS_HOST` + `DATABRICKS_TOKEN` or `~/.databrickscfg`).
"""

from __future__ import annotations

import os
import uuid

import streamlit as st
from databricks.sdk import WorkspaceClient

ENDPOINT_NAME = os.environ.get("AGENT_ENDPOINT_NAME", "nhtsa-agent-endpoint-dev-pg")


def _build_workspace_client() -> WorkspaceClient:
    """Return a `WorkspaceClient` that calls serving as the end user.

    On Databricks Apps the platform injects `X-Forwarded-Access-Token`
    (the user's OAuth token) into every request. We pull it off
    `st.context.headers` and hand it to the SDK so the agent endpoint
    sees the real user. Falls back to the default auth chain when run
    outside Apps (local dev, CI).
    """
    headers = getattr(st, "context", None)
    user_token = None
    if headers is not None:
        try:
            user_token = st.context.headers.get("X-Forwarded-Access-Token")
        except Exception:
            user_token = None

    if user_token:
        host = os.environ.get("DATABRICKS_HOST") or os.environ.get("DATABRICKS_HOSTNAME")
        return WorkspaceClient(host=host, token=user_token, auth_type='pat')
    return WorkspaceClient()


def _extract_answer(response: object) -> str:
    """Pull the assistant text out of a Responses API result.

    The endpoint emits a single `message` output item whose `content`
    is a list of `output_text` parts (see `serving.py`
    `_responses_message_event`). We concatenate all text parts so any
    future multi-part responses still render.
    """
    parts: list[str] = []
    output = getattr(response, "output", None) or []
    for item in output:
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
                text = getattr(part, "text", None) or (
                    part.get("text") if isinstance(part, dict) else ""
                )
                if text:
                    parts.append(text)
    return "\n".join(parts).strip() or "(no response)"


st.set_page_config(page_title="NHTSA Defect Intel", page_icon="🚗", layout="wide")
st.title("NHTSA Defect Intelligence")
st.caption(f"Agent endpoint: `{ENDPOINT_NAME}`")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = f"app-{uuid.uuid4().hex[:12]}"

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

prompt = st.chat_input("Ask about a recall, complaint, or investigation…")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking…_")
        try:
            ws = _build_workspace_client()
            client = ws.serving_endpoints.get_open_ai_client()
            response = client.responses.create(
                model=ENDPOINT_NAME,
                input=[{"role": "user", "content": prompt}],
                extra_body={
                    "custom_inputs": {
                        "session_id": st.session_state.session_id,
                        "request_id": uuid.uuid4().hex,
                    }
                },
            )
            answer = _extract_answer(response)
        except Exception as exc:
            answer = f"**Error calling `{ENDPOINT_NAME}`:** `{exc}`"

        placeholder.markdown(answer)
        st.session_state.messages.append({"role": "assistant", "content": answer})
