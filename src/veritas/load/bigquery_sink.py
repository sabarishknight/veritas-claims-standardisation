"""BigQuery sink - the production target.

The same schema definitions that build the local SQLite database generate the
BigQuery DDL, so the two cannot drift. What differs is *how* rows become
durable, and that difference is a cost decision worth stating:

**Load jobs, not streaming inserts.** Rows are written as newline-delimited
JSON to a staging table via a load job. Load jobs are free; the Storage Write
API is billed per gigabyte. At 30M rows/day the choice between them is the
difference between nothing and a five-figure annual line item, and the
pipeline's latency budget is 15 minutes (NFR-1.2) - minutes of batching are
free, so there is no reason to pay for seconds.

**MERGE for idempotency.** After the load, one MERGE moves staging into the
target on the deterministic ``id``. Re-processing a file updates the same rows
instead of appending duplicates (NFR-3.2). The staging table is per-run and
expires, so a failed run leaves no residue to clean up by hand.

**Partition and cluster for the queries that actually run.** Partitioning on
ingest date and clustering on ``(source_system, test_name_canonical,
test_analytics)`` means "today's flagged rows for clinic X" scans one partition
and one cluster block. Unpartitioned, the ops dashboard would full-scan a
growing table every time someone refreshed it.

The client is imported lazily, so the core pipeline keeps zero cloud
dependencies and the reviewer never needs a GCP account to run anything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from typing import Any

from ..observe.logging import get_logger
from . import schema
from .sqlite_sink import _document_row, _lab_row, _medication_row

log = get_logger(__name__)

_STAGING_EXPIRY_HOURS = 6


class BigQuerySink:
    """Load-job plus MERGE writer for BigQuery."""

    def __init__(self, project: str, dataset: str, *, location: str = "asia-south1") -> None:
        self.project = project
        self.dataset = dataset
        self.location = location
        self._client = None

    # -- client ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            try:
                from google.cloud import bigquery  # type: ignore
            except ImportError as exc:                     # pragma: no cover
                raise SystemExit(
                    "google-cloud-bigquery is required for the BigQuery sink.\n"
                    "Install it with:  python3 -m pip install -e '.[gcp]'"
                ) from exc
            self._client = bigquery.Client(project=self.project, location=self.location)
        return self._client

    def _ref(self, table: str) -> str:
        return f"{self.project}.{self.dataset}.{table}"

    # -- schema ----------------------------------------------------------
    def initialise(self, canonical_tests: Iterable[str]) -> None:
        """Create dataset, tables and views. Idempotent."""
        from google.cloud import bigquery  # type: ignore

        dataset_ref = bigquery.Dataset(f"{self.project}.{self.dataset}")
        dataset_ref.location = self.location
        dataset_ref.description = (
            "Veritas Claims standardised medical data. Managed by the "
            "veritas pipeline; schema is generated from load/schema.py."
        )
        self.client.create_dataset(dataset_ref, exists_ok=True)

        for statement in schema.bigquery_ddl(f"{self.project}.{self.dataset}"):
            self.client.query(statement).result()

        for view_sql in schema.all_views(canonical_tests):
            # The shared view definitions are written in SQLite's dialect;
            # BigQuery needs qualified names and its own CREATE syntax.
            self.client.query(_to_bigquery_view(view_sql, self.project, self.dataset)).result()

        log.info("BigQuery dataset ready",
                 extra={"dataset": self.dataset, "tables": len(schema.ALL_TABLES)})

    # -- writing ---------------------------------------------------------
    def _load_and_merge(self, table: schema.Table, rows: Sequence[dict[str, Any]],
                        run_id: str) -> int:
        if not rows:
            return 0
        from google.cloud import bigquery  # type: ignore

        staging = f"_stg_{table.name}_{run_id.replace('-', '_')[-24:]}"
        staging_ref = self._ref(staging)

        payload = "\n".join(
            json.dumps({c.name: _bq_value(row.get(c.name), c.kind)
                        for c in table.columns}, default=str)
            for row in rows
        ).encode("utf-8")

        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=[
                bigquery.SchemaField(c.name, _bq_type(c.kind)) for c in table.columns
            ],
        )
        import io
        self.client.load_table_from_file(
            io.BytesIO(payload), staging_ref, job_config=job_config
        ).result()

        # Expire the staging table rather than deleting it in a finally block:
        # a crashed run should not leave orphans, and should not need a human.
        self.client.query(
            f"ALTER TABLE `{staging_ref}` SET OPTIONS ("
            f"expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), "
            f"INTERVAL {_STAGING_EXPIRY_HOURS} HOUR))"
        ).result()

        key = table.primary_key[0]
        columns = [c.name for c in table.columns]
        updates = ",\n    ".join(f"T.{c} = S.{c}" for c in columns if c != key)
        merge = (
            f"MERGE `{self._ref(table.name)}` T\n"
            f"USING `{staging_ref}` S\n"
            f"ON T.{key} = S.{key}\n"
            f"WHEN MATCHED THEN UPDATE SET\n    {updates}\n"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) "
            f"VALUES ({', '.join('S.' + c for c in columns)})"
        )
        self.client.query(merge).result()
        return len(rows)

    def write_documents(self, documents: Iterable[Any], run_id: str) -> int:
        return self._load_and_merge(
            schema.CLINICAL_DOCUMENT, [_document_row(d) for d in documents], run_id)

    def write_lab_results(self, results: Iterable[Any], run_id: str) -> int:
        return self._load_and_merge(
            schema.LAB_RESULT, [_lab_row(r) for r in results], run_id)

    def write_medications(self, medications: Iterable[Any], run_id: str) -> int:
        return self._load_and_merge(
            schema.MEDICATION, [_medication_row(m) for m in medications], run_id)

    def write_errors(self, errors: Iterable[Any], run_id: str) -> int:
        return self._load_and_merge(
            schema.PROCESSING_ERROR, [asdict(e) for e in errors], run_id)

    def write_review_items(self, items: Iterable[Any], run_id: str) -> int:
        return self._load_and_merge(
            schema.REVIEW_ITEM, [asdict(i) for i in items], run_id)

    def write_run(self, summary: Any, **extra: Any) -> None:
        row = {**asdict(summary),
               "resolver_stats": json.dumps(extra.get("resolver_stats") or {}),
               "llm_usage": json.dumps(extra.get("llm_usage") or {})}
        self._load_and_merge(schema.PIPELINE_RUN, [row], summary.run_id)


_BQ_KIND_TO_TYPE = {"TEXT": "STRING", "REAL": "FLOAT64", "INT": "INT64",
                    "BOOL": "BOOL", "TS": "TIMESTAMP", "JSON": "JSON"}


def _bq_type(kind: str) -> str:
    return _BQ_KIND_TO_TYPE[kind]


def _bq_value(value: Any, kind: str) -> Any:
    """Coerce to the JSON shape BigQuery's loader expects."""
    if value is None:
        return None
    if kind == "BOOL":
        return bool(value)
    if kind == "INT":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == "REAL":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(getattr(v, "value", v)) for v in value)
    return str(getattr(value, "value", value))


def _to_bigquery_view(sqlite_view: str, project: str, dataset: str) -> str:
    """Translate the shared view SQL into BigQuery dialect.

    Small and mechanical on purpose. The alternative - maintaining two sets of
    view definitions - guarantees they diverge, and a dashboard that shows
    different numbers locally and in the cloud is worse than no dashboard.
    """
    sql = sqlite_view.replace("CREATE VIEW IF NOT EXISTS ", "CREATE OR REPLACE VIEW ")
    sql = sql.replace("CAST(r.result_value AS TEXT)", "CAST(r.result_value AS STRING)")
    sql = sql.replace("1.0 * SUM", "SAFE_DIVIDE_PLACEHOLDER")
    for table in schema.ALL_TABLES:
        for pattern in (f"FROM {table.name}", f"JOIN {table.name}"):
            sql = sql.replace(pattern, pattern.replace(
                table.name, f"`{project}.{dataset}.{table.name}`"))
    # Name the view itself in the dataset.
    for view_name in ("v_ideal_output_schema", "v_test_wide", "v_clinic_quality",
                      "v_flagged_rows", "v_duplicates", "v_lineage",
                      "v_panel_completeness"):
        sql = sql.replace(f"VIEW {view_name} AS", f"VIEW `{project}.{dataset}.{view_name}` AS")
    sql = sql.replace("SAFE_DIVIDE_PLACEHOLDER", "1.0 * SUM")
    sql = sql.replace("d.is_duplicate = 0", "d.is_duplicate = FALSE")
    return sql.rstrip(";")
