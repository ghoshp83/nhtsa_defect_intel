# Databricks notebook source
# MAGIC %md
# MAGIC # 2.9 — Resample `gold_narrative_chunks` with date-bucket weighting
# MAGIC
# MAGIC Free Edition's shared Foundation Models endpoint embeds at
# MAGIC ~1.5–1.7 rows/s. The original 435K-chunk source pool built by
# MAGIC notebook 2.8 would take ~3 days to embed end-to-end and burns
# MAGIC serverless DBUs continuously while the Standard VS endpoint runs.
# MAGIC
# MAGIC This notebook trims `gold_narrative_chunks` to a 50K
# MAGIC date-stratified subset that embeds in ~8 hours while preserving
# MAGIC recency-weighted defect intel plus a historical-context tail:
# MAGIC
# MAGIC | bucket                                    | weight | n_rows |
# MAGIC |-------------------------------------------|--------|--------|
# MAGIC | `event_date >= 2016-01-01`                |   80%  | 40,000 |
# MAGIC | `2000-01-01 <= event_date < 2016-01-01`   |   10%  |  5,000 |
# MAGIC | `event_date < 2000-01-01`                 |   10%  |  5,000 |
# MAGIC
# MAGIC Rows with NULL `event_date` are excluded — they cannot be placed
# MAGIC in any bucket and the agent's date-aware queries rely on
# MAGIC populated event_date anyway.
# MAGIC
# MAGIC ### Operation
# MAGIC Overwrites the table in place (`mode=overwrite`,
# MAGIC `overwriteSchema=true`) and re-applies `delta.enableChangeDataFeed`
# MAGIC after the rewrite so the VS Delta Sync still works.
# MAGIC
# MAGIC ### What to do after this runs
# MAGIC 1. Confirm the bucket counts in the final diagnostic cell.
# MAGIC 2. Delete the existing `mlops_dev.pralaygh.gold_narrative_chunks_index` VS index.
# MAGIC 3. Recreate it (re-run the relevant cell of 3.1) pointing at the
# MAGIC    now-50K source table.
# MAGIC 4. Sync — should complete in ~8 hours.

# COMMAND ----------
from pyspark.sql import SparkSession, functions as F

from nhtsa_curator.config import get_env, load_config

# COMMAND ----------
spark = SparkSession.builder.getOrCreate()

# Widget defaults follow the 80/10/10 split for 50K rows. Reduce to
# 40K (32000/4000/4000) if a strict <8h sync ceiling is needed.
dbutils.widgets.text("env", "dev")
dbutils.widgets.text("n_post_2015", "40000")
dbutils.widgets.text("n_2000_2015", "5000")
dbutils.widgets.text("n_pre_2000",  "5000")
dbutils.widgets.text("seed", "42")

env = get_env(spark)
cfg = load_config("../project_config.yml", env)

n_post = int(dbutils.widgets.get("n_post_2015"))
n_mid  = int(dbutils.widgets.get("n_2000_2015"))
n_pre  = int(dbutils.widgets.get("n_pre_2000"))
seed   = int(dbutils.widgets.get("seed"))

chunks_table = f"{cfg.full_schema_name}.gold_narrative_chunks"
print(f"Resampling {chunks_table} → {n_post + n_mid + n_pre:,} rows "
      f"(post_2015={n_post:,}, 2000_2015={n_mid:,}, pre_2000={n_pre:,})")

# COMMAND ----------
# Pre-sample diagnostic — confirms bucket population on the existing 435K.
# If a bucket has fewer rows than its target, .limit() returns the full
# bucket and we under-sample by that delta; the run still succeeds.
display(
    spark.sql(f"""
    SELECT
      CASE
        WHEN event_date IS NULL                    THEN '~null (excluded)'
        WHEN event_date >= DATE'2016-01-01'        THEN 'post_2015'
        WHEN event_date >= DATE'2000-01-01'        THEN 'y2000_2015'
        ELSE                                            'pre_2000'
      END AS bucket,
      count(*) AS rows
    FROM {chunks_table}
    GROUP BY bucket
    ORDER BY bucket
""")
)

# COMMAND ----------
src = spark.table(chunks_table).where(F.col("event_date").isNotNull())

post_2015 = src.where(F.col("event_date") >= F.lit("2016-01-01"))
mid_15    = src.where(
    (F.col("event_date") >= F.lit("2000-01-01"))
    & (F.col("event_date") <  F.lit("2016-01-01"))
)
pre_2000  = src.where(F.col("event_date") < F.lit("2000-01-01"))

# orderBy(rand(seed)).limit(N) gives an exact-count random sample per
# bucket. Triggers a shuffle but 435K rows is comfortable on Free
# Edition compute.
sampled = (
    post_2015.orderBy(F.rand(seed=seed)).limit(n_post)
    .unionByName(mid_15.orderBy(F.rand(seed=seed)).limit(n_mid))
    .unionByName(pre_2000.orderBy(F.rand(seed=seed)).limit(n_pre))
)

# COMMAND ----------
(
    sampled
    .write
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(chunks_table)
)

# Re-apply CDF — overwrite can drop the table property in some
# Delta versions; VS Delta Sync requires it.
spark.sql(f"""
    ALTER TABLE {chunks_table}
    SET TBLPROPERTIES (delta.enableChangeDataFeed = true)
""")

# COMMAND ----------
# Post-sample verification — bucket totals + per-source mix.
display(
    spark.sql(f"""
    SELECT
      CASE
        WHEN event_date >= DATE'2016-01-01' THEN 'post_2015'
        WHEN event_date >= DATE'2000-01-01' THEN 'y2000_2015'
        ELSE                                     'pre_2000'
      END AS bucket,
      source_dataset,
      count(*) AS rows
    FROM {chunks_table}
    GROUP BY bucket, source_dataset
    ORDER BY bucket, rows DESC
""")
)

display(
    spark.sql(f"""
    SELECT
      min(event_date) AS earliest,
      max(event_date) AS latest,
      count(*)        AS total_rows
    FROM {chunks_table}
""")
)
