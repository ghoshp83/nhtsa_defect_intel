# Databricks notebook source
# MAGIC %md
# MAGIC # Deploy the NHTSA Defect Intelligence Agent
# MAGIC
# MAGIC Picks the newest `latest-model` version of
# MAGIC `<catalog>.<schema>.nhtsa_agent_pg` registered by the sibling
# MAGIC `log_register_agent.py` task, and calls
# MAGIC `databricks.agents.deploy` to (re)create the serving endpoint.
# MAGIC
# MAGIC Endpoint naming: `nhtsa-agent-endpoint-<env>-pg`.
# MAGIC
# MAGIC Scale-to-zero in dev/acc; min-1 in prd (hot path).
# MAGIC Env vars injected into the container are propagated onto every
# MAGIC MLflow trace by `NhtsaResponsesAgent._stamp_trace_deploy_tags`.

# COMMAND ----------
import time

from databricks import agents
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from loguru import logger
from mlflow import MlflowClient

from nhtsa_curator.config import load_config
from nhtsa_curator.utils.common import get_widget, set_mlflow_tracking_uri

set_mlflow_tracking_uri()

env = get_widget("env", "dev")
git_sha = get_widget("git_sha", "local")

cfg = load_config("../../project_config.yml", env=env)

model_name = f"{cfg.catalog}.{cfg.db_schema}.nhtsa_agent_pg"
endpoint_name = f"nhtsa-agent-endpoint-{env}-pg"

client = MlflowClient()
model_version = client.get_model_version_by_alias(model_name, "latest-model").version
experiment = client.get_experiment_by_name(cfg.experiment_name)

logger.info("Deploying NHTSA agent:")
logger.info(f"  Model   : {model_name}")
logger.info(f"  Version : {model_version}")
logger.info(f"  Endpoint: {endpoint_name}")
logger.info(f"  Env     : {env}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Self-heal a broken endpoint
# MAGIC If a previous deploy left the endpoint in a failed state, its
# MAGIC `config` attribute can be `None`. `databricks.agents.deploy` then
# MAGIC crashes on `config.auto_capture_config` when it tries the update
# MAGIC path. Deleting the endpoint first forces the clean create path.


# COMMAND ----------
def _delete_if_broken(endpoint: str) -> None:
    w = WorkspaceClient()
    try:
        ep = w.serving_endpoints.get(endpoint)
    except NotFound:
        logger.info(f"Endpoint {endpoint} does not exist — create path.")
        return

    config_missing = getattr(ep, "config", None) is None
    state = getattr(ep, "state", None)
    config_update = getattr(state, "config_update", None) if state else None
    # Enum compares cleanly via its string value — avoid importing the enum
    # itself since the SDK name has changed across versions.
    update_failed = str(config_update).endswith("UPDATE_FAILED")

    if config_missing or update_failed:
        logger.warning(
            f"Endpoint {endpoint} is in a broken state "
            f"(config_missing={config_missing}, update_failed={update_failed}) "
            "— deleting so agents.deploy takes the create path."
        )
        w.serving_endpoints.delete(endpoint)
        for _ in range(60):  # up to ~5 min
            try:
                w.serving_endpoints.get(endpoint)
            except NotFound:
                logger.info(f"Endpoint {endpoint} deleted.")
                return
            time.sleep(5)
        raise RuntimeError(f"Endpoint {endpoint} still present after delete timeout.")

    logger.info(f"Endpoint {endpoint} exists and is healthy — update path.")


_delete_if_broken(endpoint_name)

# COMMAND ----------
# MAGIC %md
# MAGIC ## databricks.agents.deploy
# MAGIC The usage policy rate-limits + content-moderates the endpoint.
# MAGIC
# MAGIC `DATABRICKS_TOKEN` + `DATABRICKS_AUTH_TYPE=pat` route every
# MAGIC `WorkspaceClient()` inside the container to authenticate as
# MAGIC Pralay's PAT instead of the endpoint's auto-managed SPN. The
# MAGIC first deploy on this workspace (without PAT) failed at runtime
# MAGIC with `psycopg.OperationalError: password authentication failed
# MAGIC for user '<spn-uuid>'` — the auto-managed SPN had no Postgres
# MAGIC role on the Lakebase `nhtsa-agent-lakebase-pg` project. PAT auth
# MAGIC sidesteps this because Pralay owns the Lakebase project (created
# MAGIC by 4.2). The same PAT also gives `ws.genie` / `ws.statement_execution`
# MAGIC the SELECT grants on `mlops_dev.pralaygh.*` that the SPN lacks.
# MAGIC True OBO via `ModelServingUserCredentials` would need (a) workspace
# MAGIC admin enables the OBO preview and (b) `UserAuthPolicy` declared at
# MAGIC log_model time; neither is in place on Free Edition as of 2026-05.

# COMMAND ----------
# prd keeps one warm replica; dev/acc scale to zero between demos.
scale_to_zero = env != "prd"

agents.deploy(
    model_name=model_name,
    model_version=int(model_version),
    endpoint_name=endpoint_name,
    usage_policy_id=cfg.usage_policy_id,
    scale_to_zero=scale_to_zero,
    workload_size="Small",
    deploy_feedback_model=False,
    environment_vars={
        "GIT_SHA": git_sha,
        "MODEL_VERSION": str(model_version),
        "MODEL_SERVING_ENDPOINT_NAME": endpoint_name,
        "MLFLOW_EXPERIMENT_ID": experiment.experiment_id,
        "ENV": env,
        # See markdown above — PAT auth fixes both Lakebase (Postgres
        # role) and Genie/SQL (UC SELECT) auth deltas vs the endpoint's
        # auto-managed SPN. DATABRICKS_AUTH_TYPE=pat forces the SDK to
        # ignore the auto-injected DATABRICKS_CLIENT_ID/SECRET.
        # DATABRICKS_HOST is required because explicit PAT mode disables
        # the SDK's auto-discovery of the Model-Serving-injected host;
        # without it the SDK errors with "default auth: cannot configure
        # default credentials" at agent module load.
        "DATABRICKS_HOST": WorkspaceClient().config.host,
        "DATABRICKS_TOKEN": "{{secrets/pralaygh-personal/pralay_pat}}",
        "DATABRICKS_AUTH_TYPE": "pat",
    },
)

logger.info("✓ Deployment complete — endpoint is warming up.")
