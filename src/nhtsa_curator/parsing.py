"""Document parsing via Databricks ``ai_parse_document``.

``ai_parse_document(content)`` is a Databricks built-in SQL function
that takes binary PDF/Office bytes and returns a structured payload
(text, pages, optional table extractions). It runs on a serverless
inference endpoint, so we treat it as a potentially expensive call:

* **Idempotency**: we maintain a parsed-tracker table per dataset
  (``silver_<dataset>_parsed_tracker``) and only invoke ``ai_parse_document``
  on PDFs we haven't seen before. Re-running the silver_parse notebook
  is therefore a no-op for already-parsed docs.
* **Schema stability**: we materialise the parsed output into typed
  columns rather than carrying the raw STRUCT around. The first time
  we see a new ``ai_parse_document`` schema version the writer will
  log a warning and rebuild — see ``silver_<dataset>_parsed`` rebuilds
  triggered by the ``--full-rebuild`` flag in the notebook.
* **Cost guardrails**: callers can pass ``max_docs_per_run`` so the
  silver job stays predictable in runtime + spend. The unparsed
  backlog drains across consecutive runs.

This module deliberately does NOT chunk — chunking is gold's job (the
output here is one row per doc, not per chunk).
"""

from __future__ import annotations

from loguru import logger
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

from .config import ProjectConfig

# Spark Connect has a 3 GB hard cap on local-relation payloads (the bytes
# pushed via spark.createDataFrame). NHTSA PDFs average ~3.5 MB, so chunks of
# ~100 keep each createDataFrame call near 350 MB — well clear of the limit
# even with size variance. The user-facing max_docs_per_run is the OUTER cap;
# this is just an internal materialisation guardrail.
_INTERNAL_CHUNK_SIZE = 100


_TRACKER_SCHEMA = T.StructType(
    [
        T.StructField("doc_id", T.StringType(), False),
        T.StructField("volume_path", T.StringType(), False),
        T.StructField("parsed_at", T.TimestampType(), False),
        T.StructField("parser_version", T.StringType(), True),
        T.StructField("parse_status", T.StringType(), False),  # ok | error
        T.StructField("error_message", T.StringType(), True),
    ]
)


def _ensure_tracker(spark: SparkSession, table: str) -> None:
    if not spark.catalog.tableExists(table):
        spark.createDataFrame([], _TRACKER_SCHEMA).write.format("delta").saveAsTable(
            table
        )
        logger.info(f"Created parse tracker {table}")


def _unparsed_paths(
    spark: SparkSession,
    docs_table: str,
    tracker_table: str,
    doc_id_col: str,
    path_col: str,
    limit: int,
) -> DataFrame:
    """Return ``(doc_id, volume_path)`` for docs that aren't yet parsed."""
    return spark.sql(f"""
        SELECT d.{doc_id_col} AS doc_id, d.{path_col} AS volume_path
        FROM {docs_table} d
        LEFT JOIN {tracker_table} t
          ON d.{doc_id_col} = t.doc_id
        WHERE t.doc_id IS NULL
        LIMIT {int(limit)}
    """)


def parse_documents(
    spark: SparkSession,
    cfg: ProjectConfig,
    *,
    dataset: str,
    docs_table: str,
    doc_id_col: str,
    path_col: str,
    parsed_table: str,
    tracker_table: str,
    max_docs_per_run: int = 200,
    parser_version: str = "ai_parse_document/v1",
) -> dict:
    """Run ``ai_parse_document`` on the next batch of unparsed PDFs.

    Args:
        dataset: short label used in logs (``"tsb"`` | ``"investigation"``).
        docs_table: bronze table with ``doc_id_col`` + ``path_col``
            (e.g. ``bronze_tsb_documents`` with cols ``tsb_id`` +
            ``volume_path``).
        parsed_table: target silver-parsed table; CREATE-if-missing.
        tracker_table: per-dataset tracker for idempotency.
        max_docs_per_run: cap on documents parsed in this invocation.

    Returns:
        Tally dict ``{queued, parsed_ok, parsed_err}``.
    """
    _ensure_tracker(spark, tracker_table)

    todo = _unparsed_paths(
        spark,
        docs_table,
        tracker_table,
        doc_id_col,
        path_col,
        max_docs_per_run,
    )
    n_todo = todo.count()
    logger.info(f"[{dataset}] {n_todo} docs queued for ai_parse_document")
    if n_todo == 0:
        return {"queued": 0, "parsed_ok": 0, "parsed_err": 0}

    # Read PDF bytes from the UC volume on the driver, materialise as a Spark
    # DataFrame, run ai_parse_document via SQL. Process in internal chunks so
    # the createDataFrame payload stays under Spark Connect's 3 GB local-relation
    # cap (PDFs average ~3.5 MB, so chunks of 100 = ~350 MB, well clear of the
    # limit even with size variance). The user-facing max_docs_per_run cap is
    # the OUTER bound; this chunking is just to keep each materialisation small.
    todo_rows = todo.collect()
    n_ok = 0
    for chunk_start in range(0, len(todo_rows), _INTERNAL_CHUNK_SIZE):
        chunk = todo_rows[chunk_start : chunk_start + _INTERNAL_CHUNK_SIZE]
        pdf_rows: list[tuple[str, str, bytes]] = []
        for r in chunk:
            try:
                with open(r["volume_path"], "rb") as fh:
                    pdf_rows.append((r["doc_id"], r["volume_path"], fh.read()))
            except FileNotFoundError:
                logger.warning(
                    f"[{dataset}] PDF missing on disk, skipping: {r['volume_path']}"
                )
        if not pdf_rows:
            continue

        pdfs_df = spark.createDataFrame(
            pdf_rows, "doc_id string, volume_path string, content binary"
        )
        pdfs_df.createOrReplaceTempView(f"_{dataset}_pdfs")

        # ai_parse_document returns VARIANT (Databricks changed it from STRUCT).
        # Extract full_text by concatenating per-element content/description;
        # fall back to a top-level `text` field or the raw JSON if neither path
        # is present. Raw VARIANT is also kept in `parsed_raw` so downstream
        # code can navigate any field without re-parsing.
        parsed = spark.sql(f"""
            WITH pdfs AS (
                SELECT
                    doc_id,
                    volume_path,
                    ai_parse_document(content) AS parsed
                FROM _{dataset}_pdfs
            )
            SELECT
                doc_id,
                volume_path,
                coalesce(
                    nullif(
                        array_join(
                            transform(
                                filter(
                                    try_cast(
                                        parsed:document:elements
                                        AS array<struct<content: string, description: string, type: string>>
                                    ),
                                    x -> x.type NOT IN ('page_footer', 'page_number')
                                      AND coalesce(nullif(x.content, ''), x.description) IS NOT NULL
                                ),
                                x -> coalesce(nullif(x.content, ''), x.description)
                            ),
                            '\n'
                        ),
                        ''
                    ),
                    try_variant_get(parsed, '$.text', 'string'),
                    to_json(parsed)
                ) AS full_text,
                parsed:document:pages AS pages,
                parsed:metadata       AS doc_metadata,
                parsed                AS parsed_raw,
                current_timestamp()   AS parsed_at,
                '{parser_version}'    AS parser_version
            FROM pdfs
        """)

        if not spark.catalog.tableExists(parsed_table):
            parsed.limit(0).write.format("delta").option(
                "delta.enableChangeDataFeed", "true"
            ).saveAsTable(parsed_table)
            logger.info(f"Created {parsed_table}")

        # Persist parse outputs and tracker rows in lockstep per chunk so a
        # mid-run failure leaves us with a consistent state we can resume from.
        parsed.write.mode("append").saveAsTable(parsed_table)

        tracker_rows = parsed.select(
            F.col("doc_id"),
            F.col("volume_path"),
            F.col("parsed_at"),
            F.col("parser_version"),
            F.lit("ok").alias("parse_status"),
            F.lit(None).cast("string").alias("error_message"),
        )
        tracker_rows.write.mode("append").saveAsTable(tracker_table)

        chunk_ok = tracker_rows.count()
        n_ok += chunk_ok
        logger.info(
            f"[{dataset}] chunk {chunk_start // _INTERNAL_CHUNK_SIZE + 1}: "
            f"parsed {chunk_ok} (running total: {n_ok})"
        )

    logger.info(f"[{dataset}] parsed_ok={n_ok} of {n_todo} queued")
    return {"queued": n_todo, "parsed_ok": n_ok, "parsed_err": n_todo - n_ok}


# ---------------------------------------------------------------------------
# Convenience wrappers per dataset — keeps notebook code uncluttered.
# ---------------------------------------------------------------------------


def parse_tsb_documents(
    spark: SparkSession,
    cfg: ProjectConfig,
    *,
    max_docs_per_run: int = 200,
) -> dict:
    return parse_documents(
        spark,
        cfg,
        dataset="tsb",
        docs_table=f"{cfg.full_schema_name}.bronze_tsb_documents",
        doc_id_col="tsb_id",
        path_col="volume_path",
        parsed_table=f"{cfg.full_schema_name}.silver_tsb_parsed",
        tracker_table=f"{cfg.full_schema_name}.silver_tsb_parsed_tracker",
        max_docs_per_run=max_docs_per_run,
    )


def parse_investigation_documents(
    spark: SparkSession,
    cfg: ProjectConfig,
    *,
    max_docs_per_run: int = 200,
) -> dict:
    return parse_documents(
        spark,
        cfg,
        dataset="investigation",
        docs_table=f"{cfg.full_schema_name}.bronze_investigation_documents",
        doc_id_col="document_id",
        path_col="volume_path",
        parsed_table=f"{cfg.full_schema_name}.silver_investigation_parsed",
        tracker_table=f"{cfg.full_schema_name}.silver_investigation_parsed_tracker",
        max_docs_per_run=max_docs_per_run,
    )
