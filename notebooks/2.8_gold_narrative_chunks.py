# Databricks notebook source
# MAGIC %md
# MAGIC # 2.8 — `gold_narrative_chunks` (Vector Search source)
# MAGIC
# MAGIC Builds the unified narrative-chunk table from three sources:
# MAGIC - `silver_complaints.narrative_clean`
# MAGIC - `silver_tsb_parsed.full_text` (joined to `silver_tsbs` for metadata)
# MAGIC - `silver_investigation_parsed.full_text` (joined to `silver_investigations`)
# MAGIC
# MAGIC Each chunk carries the metadata columns the agent will use as
# MAGIC filters: `make_norm`, `model_year`, `component_group`,
# MAGIC `event_date`, `source_dataset`, `oem_group`.
# MAGIC
# MAGIC Output table has CDF enabled — required for Vector Search index
# MAGIC sync in Phase 3.

# COMMAND ----------
import yaml
from pyspark.sql import SparkSession

from nhtsa_curator.config import ChunkingConfig, get_env, load_config
from nhtsa_curator.gold import write_gold_narrative_chunks

# COMMAND ----------
spark = SparkSession.builder.getOrCreate()

dbutils.widgets.text("env", "dev")
dbutils.widgets.text("run_id", "manual")
# sample_total=0 → full production build (~8.83M chunks; embeds in weeks
# on the shared Foundation Models endpoint).
# sample_total>0 → stratified sample across source_dataset. Default is 500K
# which embeds in ~24h and is the portfolio-friendly cut. Split is weighted
# toward the big narrative sources; small sources (investigation, sgo) cap
# at their actual size and effectively flow through full:
#   complaints     40%  → big corpus, rich consumer narratives
#   tsb            40%  → big corpus, technical bulletins
#   investigation  15%  → small; full ~23.8K passes through
#   sgo             5%  → small; full ~6.4K passes through (AV crashes)
dbutils.widgets.text("sample_total", "500000")
env = get_env(spark)

cfg = load_config("../project_config.yml", env)

with open("../project_config.yml") as fh:
    raw = yaml.safe_load(fh)
chunking = ChunkingConfig(**raw["chunking"])

sample_total = int(dbutils.widgets.get("sample_total"))
sample_per_source = (
    {
        "complaints": int(sample_total * 0.40),
        "tsb": int(sample_total * 0.40),
        "investigation": int(sample_total * 0.15),
        "sgo": int(sample_total * 0.05),
    }
    if sample_total > 0
    else None
)

# COMMAND ----------
write_gold_narrative_chunks(
    spark=spark,
    cfg=cfg,
    chunking=chunking,
    embedding_model=cfg.embedding_endpoint,
    sample_per_source=sample_per_source,
)

# COMMAND ----------
display(
    spark.sql(f"""
    SELECT source_dataset, count(*) AS chunks
    FROM {cfg.full_schema_name}.gold_narrative_chunks
    GROUP BY source_dataset
    ORDER BY chunks DESC
""")
)

display(
    spark.sql(f"""
    SELECT chunk_id, source_dataset, source_id, chunk_idx,
           substring(content, 1, 240) AS preview
    FROM {cfg.full_schema_name}.gold_narrative_chunks
    LIMIT 10
""")
)
