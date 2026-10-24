"""Canonical schema, defined once and rendered per dialect.

The column list lives in Python, not in two hand-maintained ``.sql`` files, so
SQLite and BigQuery cannot drift apart. ``python -m veritas ddl --dialect
bigquery`` prints the cloud DDL from the same definitions the local run uses,
which means the thing the reviewer runs on a laptop and the thing deployed to
BigQuery are provably the same shape.

Storage model
-------------
Narrow and normalised:

    clinical_document   one row per document (lab report or discharge summary)
    lab_result          one row per test observation      <- the fact grain
    medication          one row per prescribed item
    processing_error    dead-letter store (FR-4.2)
    review_item         human review queue (FR-5.3)
    file_ledger         idempotency and lineage ledger (NFR-3.2, NFR-4.2)
    pipeline_run        per-run metrics (FR-5.1, NFR-5.1)
    learned_alias       promoted name decisions (NFR-4.1)

The assignment's 79-column target shape is then delivered as a *view* over
these tables (:data:`IDEAL_OUTPUT_COLUMNS`), and FR-2.2's five-columns-per-test
presentation as a second view. Both required outputs exist exactly as
specified; neither dictates the storage underneath. The reasoning is in
``models.py`` and in docs/assumptions.md D-1.

Keys and idempotency
--------------------
Every primary key is a deterministic content hash, so re-running a file
produces byte-identical keys and the write degrades to a no-op upsert. There is
no sequence, no autoincrement and no "did I already load this" lookup -
idempotency is a property of the key, which is the only version of it that
survives parallel workers and partial failures (NFR-3.2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    kind: str                 # TEXT | REAL | INT | BOOL | TS | JSON
    primary_key: bool = False
    index: bool = False
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    comment: str = ""
    # BigQuery physical layout. Partitioning on ingest date and clustering on
    # the columns every ops query filters by is what keeps a full-day scan off
    # the bill: a query for one clinic's flagged rows touches one partition and
    # one cluster block instead of the whole table.
    partition_by: str | None = None
    cluster_by: tuple[str, ...] = ()

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.primary_key)


_SQLITE_TYPES = {"TEXT": "TEXT", "REAL": "REAL", "INT": "INTEGER",
                 "BOOL": "INTEGER", "TS": "TEXT", "JSON": "TEXT"}
_BQ_TYPES = {"TEXT": "STRING", "REAL": "FLOAT64", "INT": "INT64",
             "BOOL": "BOOL", "TS": "TIMESTAMP", "JSON": "JSON"}


# Lineage columns, identical on every fact table. NFR-4.2 asks that every
# output record be traceable to its source file, clinic and ingest time; the
# cheapest way to guarantee that is to make it structurally impossible to
# write a row without them.
_LINEAGE = (
    Column("file_uri", "TEXT", index=True, comment="source object, gs:// or file://"),
    Column("file_sha256", "TEXT", index=True, comment="content identity of the source file"),
    Column("document_id", "TEXT", index=True, comment="source document identifier"),
    Column("trace_id", "TEXT", index=True, comment="trace id as supplied by the source"),
    Column("correlation_id", "TEXT", index=True, comment="correlation id as supplied by the source"),
    Column("source_system", "TEXT", index=True, comment="FASTTRACK / ARTEMIS / ..."),
    Column("clinic_profile", "TEXT", comment="config profile applied"),
    Column("schema_version", "TEXT", comment="version of that profile"),
    Column("ingested_at", "TS", comment="when the file was read"),
    Column("processed_at", "TS", comment="when this row was produced"),
    Column("run_id", "TEXT", index=True, comment="pipeline run that wrote this row"),
    Column("source_row_index", "INT", comment="position within the source array"),
)


CLINICAL_DOCUMENT = Table(
    "clinical_document",
    (
        Column("id", "TEXT", primary_key=True, comment="deterministic document identity"),
        Column("record_type", "TEXT", index=True),
        *_LINEAGE,
        Column("claim_no", "TEXT", index=True),
        Column("nt_code", "TEXT"),
        Column("consumer_client_id", "TEXT"),
        Column("destination_identifier", "TEXT"),
        Column("case_id", "TEXT"),
        Column("patient_token", "TEXT", index=True, comment="HMAC token, not a name"),
        Column("patient_name", "TEXT", comment="null unless pii.mode=passthrough"),
        Column("age_years", "REAL"),
        Column("age_text", "TEXT"),
        Column("gender", "TEXT"),
        Column("uhid_token", "TEXT", index=True),
        Column("hospital_name", "TEXT", index=True),
        Column("hospital_address", "TEXT"),
        Column("doctor_name", "TEXT"),
        Column("ward", "TEXT"),
        Column("admission_date", "TEXT"),
        Column("discharge_date", "TEXT"),
        Column("report_date", "TEXT"),
        Column("bill_date", "TEXT"),
        Column("diagnosis", "TEXT"),
        Column("brief_history", "TEXT"),
        Column("general_examinations", "TEXT"),
        Column("recommendations", "TEXT"),
        Column("post_discharge_advice", "TEXT"),
        Column("course_during_hospitalisation", "TEXT"),
        Column("content_hash", "TEXT", index=True, comment="dedup key"),
        Column("is_duplicate", "BOOL", index=True),
        Column("duplicate_of", "TEXT"),
        Column("row_count", "INT"),
        Column("flags", "TEXT", comment="comma-separated reason codes"),
        Column("raw_payload", "TEXT", comment="FR-4.3 audit trail: the original JSON"),
    ),
    comment="One clinical document, deduplicated on content.",
    partition_by="DATE(ingested_at)",
    cluster_by=("source_system", "record_type", "claim_no"),
)


LAB_RESULT = Table(
    "lab_result",
    (
        Column("id", "TEXT", primary_key=True),
        Column("document_pk", "TEXT", index=True, comment="-> clinical_document.id"),
        *_LINEAGE,
        Column("page_no", "INT"),
        Column("occurrence_index", "INT", comment="nth appearance of this test in the report"),
        Column("test_name_canonical", "TEXT", index=True),
        Column("test_name_original", "TEXT"),
        Column("loinc_code", "TEXT"),
        Column("panel", "TEXT", index=True),
        Column("result_value", "REAL", comment="in unit_canonical"),
        Column("result_text", "TEXT"),
        Column("result_text_original", "TEXT"),
        Column("unit_canonical", "TEXT"),
        Column("unit_original", "TEXT"),
        Column("range_low", "REAL"),
        Column("range_high", "REAL"),
        Column("range_text", "TEXT"),
        Column("range_text_original", "TEXT"),
        Column("test_analytics", "TEXT", index=True,
               comment="Within/Above/Below Range | Outlier | Invalid | Not Applicable"),
        Column("range_source", "TEXT", comment="source | catalogue | none"),
        Column("normalization_method", "TEXT", index=True),
        Column("normalization_confidence", "REAL"),
        Column("assay_method", "TEXT", comment="where the source reported one"),
        Column("source_interpretation", "TEXT", comment="the lab's own verdict, as evidence"),
        Column("coherence_note", "TEXT"),
        Column("flags", "TEXT", index=True),
    ),
    comment="One test observation. The analytics fact grain.",
    partition_by="DATE(ingested_at)",
    cluster_by=("source_system", "test_name_canonical", "test_analytics"),
)


MEDICATION = Table(
    "medication",
    (
        Column("id", "TEXT", primary_key=True),
        Column("document_pk", "TEXT", index=True),
        *_LINEAGE,
        Column("occurrence_index", "INT"),
        Column("medicine_original", "TEXT"),
        Column("medicine_generic", "TEXT", index=True),
        Column("brand_matched", "TEXT"),
        Column("atc_code", "TEXT", index=True),
        Column("dosage_form", "TEXT"),
        Column("strength", "TEXT"),
        Column("dose", "TEXT"),
        Column("frequency", "TEXT"),
        Column("medicine_type", "TEXT"),
        Column("is_combination", "BOOL"),
        Column("confidence", "REAL"),
        Column("flags", "TEXT"),
    ),
    comment="One prescribed item, brand resolved to molecule.",
    partition_by="DATE(ingested_at)",
    cluster_by=("source_system", "medicine_generic"),
)


PROCESSING_ERROR = Table(
    "processing_error",
    (
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", index=True),
        Column("file_uri", "TEXT", index=True),
        Column("file_sha256", "TEXT", index=True),
        Column("document_id", "TEXT"),
        Column("trace_id", "TEXT"),
        Column("source_row_index", "INT"),
        Column("stage", "TEXT", index=True, comment="read|parse|route|standardise|load"),
        Column("reason", "TEXT", index=True, comment="closed vocabulary"),
        Column("detail", "TEXT"),
        Column("payload_excerpt", "TEXT"),
        Column("retryable", "BOOL"),
        Column("occurred_at", "TS"),
    ),
    comment="Dead-letter store. Enough context to reprocess unaided.",
    partition_by="DATE(occurred_at)",
    cluster_by=("reason", "stage"),
)


REVIEW_ITEM = Table(
    "review_item",
    (
        Column("id", "TEXT", primary_key=True),
        Column("run_id", "TEXT", index=True),
        Column("kind", "TEXT", index=True),
        Column("priority", "INT", index=True),
        Column("document_id", "TEXT"),
        Column("row_id", "TEXT"),
        Column("source_system", "TEXT", index=True),
        Column("subject", "TEXT"),
        Column("evidence", "TEXT"),
        Column("proposal", "TEXT"),
        Column("status", "TEXT", index=True),
        Column("created_at", "TS"),
    ),
    comment="Things the pipeline could do but should not do unsupervised.",
    partition_by="DATE(created_at)",
    cluster_by=("kind", "status"),
)


FILE_LEDGER = Table(
    "file_ledger",
    (
        Column("file_sha256", "TEXT", primary_key=True),
        Column("file_uri", "TEXT"),
        Column("size_bytes", "INT"),
        Column("first_seen_at", "TS"),
        Column("last_run_id", "TEXT", index=True),
        Column("status", "TEXT", index=True),
        Column("documents_emitted", "INT"),
        Column("rows_emitted", "INT"),
        Column("errors_emitted", "INT"),
    ),
    comment="Keyed on content, not filename: the same bytes are the same work.",
)


PIPELINE_RUN = Table(
    "pipeline_run",
    (
        Column("run_id", "TEXT", primary_key=True),
        Column("started_at", "TS"),
        Column("finished_at", "TS"),
        Column("files_discovered", "INT"),
        Column("files_processed", "INT"),
        Column("files_skipped_duplicate", "INT"),
        Column("files_failed", "INT"),
        Column("documents_emitted", "INT"),
        Column("documents_duplicate", "INT"),
        Column("rows_emitted", "INT"),
        Column("rows_flagged", "INT"),
        Column("rows_invalid", "INT"),
        Column("rows_outlier", "INT"),
        Column("rows_out_of_range", "INT"),
        Column("medications_emitted", "INT"),
        Column("errors_emitted", "INT"),
        Column("review_items_emitted", "INT"),
        Column("llm_calls", "INT"),
        Column("llm_cache_hits", "INT"),
        Column("reports_misaligned", "INT"),
        Column("duration_seconds", "REAL"),
        Column("config_fingerprint", "TEXT", comment="ties output to the config that made it"),
        Column("worker_count", "INT"),
        Column("resolver_stats", "TEXT"),
        Column("llm_usage", "TEXT"),
    ),
    comment="Per-run metrics, the source for the dashboard and alerts.",
)


LEARNED_ALIAS = Table(
    "learned_alias",
    (
        Column("source_name_normalised", "TEXT", primary_key=True),
        Column("canonical", "TEXT", index=True),
        Column("source_name_example", "TEXT"),
        Column("provider", "TEXT", comment="gemini | stub | human"),
        Column("confidence", "REAL"),
        Column("rationale", "TEXT"),
        Column("created_at", "TS"),
        Column("run_id", "TEXT"),
        Column("times_used", "INT"),
    ),
    comment="The ratchet behind NFR-4.1: each novel spelling is reasoned about once.",
)


ALL_TABLES: tuple[Table, ...] = (
    CLINICAL_DOCUMENT, LAB_RESULT, MEDICATION, PROCESSING_ERROR,
    REVIEW_ITEM, FILE_LEDGER, PIPELINE_RUN, LEARNED_ALIAS,
)


# ---------------------------------------------------------------------------
# DDL rendering
# ---------------------------------------------------------------------------

def sqlite_ddl(tables: Iterable[Table] = ALL_TABLES) -> list[str]:
    statements: list[str] = []
    for table in tables:
        cols = [
            f"  {c.name} {_SQLITE_TYPES[c.kind]}"
            + (" PRIMARY KEY" if c.primary_key and len(table.primary_key) == 1 else "")
            for c in table.columns
        ]
        if len(table.primary_key) > 1:
            cols.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
        statements.append(
            f"-- {table.comment}\nCREATE TABLE IF NOT EXISTS {table.name} (\n"
            + ",\n".join(cols) + "\n);"
        )
        for column in table.columns:
            if column.index and not column.primary_key:
                statements.append(
                    f"CREATE INDEX IF NOT EXISTS ix_{table.name}_{column.name} "
                    f"ON {table.name}({column.name});"
                )
    return statements


def bigquery_ddl(dataset: str, tables: Iterable[Table] = ALL_TABLES) -> list[str]:
    """BigQuery DDL with partitioning, clustering and column descriptions.

    BigQuery has no primary keys to enforce, so uniqueness is maintained by the
    loader's MERGE on the deterministic ``id``. Partition + cluster are chosen
    for the two queries that actually run at volume: "today's rows for clinic
    X" and "flagged rows by test".
    """
    statements: list[str] = []
    for table in tables:
        cols = [
            f"  {c.name} {_BQ_TYPES[c.kind]}"
            + (f" OPTIONS(description={c.comment!r})" if c.comment else "")
            for c in table.columns
        ]
        clauses = ""
        if table.partition_by:
            clauses += f"\nPARTITION BY {table.partition_by}"
        if table.cluster_by:
            clauses += f"\nCLUSTER BY {', '.join(table.cluster_by)}"
        statements.append(
            f"CREATE TABLE IF NOT EXISTS `{dataset}.{table.name}` (\n"
            + ",\n".join(cols) + f"\n){clauses}\nOPTIONS(description={table.comment!r});"
        )
    return statements


# ---------------------------------------------------------------------------
# The assignment's target output shape, delivered as a view
#
# Column order and names are taken verbatim from the supplied
# `Ourput-table-ideal-schema.csv` so the result can be diffed against it
# directly. Several of its columns are different spellings of the same fact
# (`age`, `age_text`, `age_years`, `basic_info_age`), which is what a landing
# table looks like after a few years of accretion. They are all populated from
# the one canonical field, so the view is faithful without the storage being
# redundant. docs/assumptions.md D-1 covers the reasoning.
# ---------------------------------------------------------------------------

IDEAL_OUTPUT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "r.id"),
    ("document_id", "r.document_id"),
    ("record_type", "d.record_type"),
    ("file_gcs_path", "r.file_uri"),
    ("trace_id", "r.trace_id"),
    ("correlation_id", "r.correlation_id"),
    ("source_system", "r.source_system"),
    ("claim_no", "d.claim_no"),
    ("nt_code", "d.nt_code"),
    ("consumer_client_id", "d.consumer_client_id"),
    ("destination_identifier", "d.destination_identifier"),
    ("patient_name", "COALESCE(d.patient_name, d.patient_token)"),
    ("age", "d.age_text"),
    ("gender", "d.gender"),
    ("uhid", "d.uhid_token"),
    ("hospital_name", "d.hospital_name"),
    ("doctor_name", "d.doctor_name"),
    ("bill_date", "d.bill_date"),
    ("reports_date", "d.report_date"),
    ("test_name_canonical", "r.test_name_canonical"),
    ("test_name_original", "r.test_name_original"),
    ("result_value", "r.result_value"),
    ("result_text", "r.result_text"),
    ("unit_canonical", "r.unit_canonical"),
    ("unit_original", "r.unit_original"),
    ("range_low", "r.range_low"),
    ("range_high", "r.range_high"),
    ("range_text", "r.range_text"),
    ("test_analytics", "r.test_analytics"),
    ("normalization_method", "r.normalization_method"),
    ("normalization_confidence", "r.normalization_confidence"),
    ("admission_date", "d.admission_date"),
    ("discharge_date", "d.discharge_date"),
    ("diagnosis", "d.diagnosis"),
    ("brief_history", "d.brief_history"),
    ("general_examinations", "d.general_examinations"),
    ("recommendations", "d.recommendations"),
    ("hospital_address", "d.hospital_address"),
    ("ward", "d.ward"),
    ("post_discharge_advice", "d.post_discharge_advice"),
    ("medicine", "NULL"),
    ("dose", "NULL"),
    ("frequency", "NULL"),
    ("medicine_type", "NULL"),
    ("processed_at", "r.processed_at"),
    ("ingested_at", "r.ingested_at"),
    ("basic_info_age", "d.age_text"),
    ("basic_info_bill_date", "d.bill_date"),
    ("medicine_injections_investigation", "NULL"),
    ("discharge_medications_dose", "NULL"),
    ("metadetails", "NULL"),
    ("discharge_medications_frequency", "NULL"),
    ("discharge_medications_medicine", "NULL"),
    ("lab_or_hospital_name", "d.hospital_name"),
    ("report_details_page_no", "r.page_no"),
    ("report_details_range", "r.range_text_original"),
    ("report_details_result", "r.result_text_original"),
    ("report_details_test_analytics", "r.source_interpretation"),
    ("report_details_test_name", "r.test_name_original"),
    ("report_details_unit", "r.unit_original"),
    ("age_text", "d.age_text"),
    ("other_med_inj_investigations", "NULL"),
    ("report_date", "d.report_date"),
    ("course_during_hospitalisation", "d.course_during_hospitalisation"),
    ("page_number", "r.page_no"),
    ("range_text_original", "r.range_text_original"),
    ("medication_dose", "NULL"),
    ("result_text_original", "r.result_text_original"),
    ("medication_frequency", "NULL"),
    ("course_during_hospitalization", "d.course_during_hospitalisation"),
    ("medication_name", "NULL"),
    ("page_no", "r.page_no"),
    ("test_name", "r.test_name_original"),
    ("medication_medicine", "NULL"),
    ("result", "r.result_text_original"),
    ("unit", "r.unit_original"),
    ("age_years", "d.age_years"),
    ("range", "r.range_text_original"),
)

# The medication half of the union: same columns, test fields null, medication
# fields populated. One view therefore covers both record types, which is what
# the 79-column union implies.
_MEDICATION_OVERRIDES: dict[str, str] = {
    "id": "m.id",
    "record_type": "'medication'",
    "file_gcs_path": "m.file_uri",
    "document_id": "m.document_id",
    "trace_id": "m.trace_id",
    "correlation_id": "m.correlation_id",
    "source_system": "m.source_system",
    "medicine": "m.medicine_generic",
    "dose": "m.dose",
    "frequency": "m.frequency",
    "medicine_type": "m.medicine_type",
    "discharge_medications_dose": "m.dose",
    "discharge_medications_frequency": "m.frequency",
    "discharge_medications_medicine": "m.medicine_original",
    "medication_dose": "m.dose",
    "medication_frequency": "m.frequency",
    "medication_name": "m.medicine_generic",
    "medication_medicine": "m.medicine_original",
    "processed_at": "m.processed_at",
    "ingested_at": "m.ingested_at",
}
_LAB_ONLY_PREFIXES = ("r.",)


def ideal_output_view(name: str = "v_ideal_output_schema") -> str:
    """Render the 79-column target view (lab rows UNION ALL medication rows)."""
    lab_select = ",\n".join(f"  {expr} AS {col}" for col, expr in IDEAL_OUTPUT_COLUMNS)

    med_parts: list[str] = []
    for col, expr in IDEAL_OUTPUT_COLUMNS:
        override = _MEDICATION_OVERRIDES.get(col)
        if override is not None:
            med_parts.append(f"  {override} AS {col}")
        elif expr.startswith(_LAB_ONLY_PREFIXES):
            med_parts.append(f"  NULL AS {col}")
        else:
            med_parts.append(f"  {expr} AS {col}")
    med_select = ",\n".join(med_parts)

    return (
        f"CREATE VIEW IF NOT EXISTS {name} AS\n"
        f"SELECT\n{lab_select}\n"
        f"FROM lab_result r\n"
        f"JOIN clinical_document d ON d.id = r.document_pk\n"
        f"WHERE d.is_duplicate = 0\n"
        f"UNION ALL\n"
        f"SELECT\n{med_select}\n"
        f"FROM medication m\n"
        f"JOIN clinical_document d ON d.id = m.document_pk\n"
        f"WHERE d.is_duplicate = 0;"
    )


# ---------------------------------------------------------------------------
# FR-2.2: exactly five columns per defined test
# ---------------------------------------------------------------------------

def _column_safe(canonical: str) -> str:
    out = "".join(ch if ch.isalnum() else "_" for ch in canonical)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def test_wide_view(canonical_tests: Iterable[str],
                   name: str = "v_test_wide") -> str:
    """One row per document, five columns per catalogue test.

    FR-2.2 asks for ``Test_Name``, ``Test_Name_Result``, ``Test_Name_Range``,
    ``Test_Name_Unit`` and ``Test_Name_Analytics`` per test, blank where the
    test is absent from a report. Generated from the catalogue rather than
    typed out, so adding a test to config adds its five columns automatically -
    which is the whole point of NFR-2.1.

    Worth stating plainly: this shape is for human and spreadsheet consumption.
    It is a view, not storage, because as storage it would mean an ALTER TABLE
    every time a clinic sends a test we have not seen.
    """
    selects: list[str] = ["  d.id AS document_pk", "  d.document_id",
                          "  d.source_system", "  d.record_type",
                          "  d.report_date", "  d.claim_no"]
    for canonical in sorted(canonical_tests):
        safe = _column_safe(canonical)
        cond = f"CASE WHEN r.test_name_canonical = '{canonical}' THEN"
        selects.extend([
            f"  MAX({cond} r.test_name_canonical END) AS {safe}",
            f"  MAX({cond} COALESCE(CAST(r.result_value AS TEXT), r.result_text) END) AS {safe}_Result",
            f"  MAX({cond} r.range_text_original END) AS {safe}_Range",
            f"  MAX({cond} r.unit_canonical END) AS {safe}_Unit",
            f"  MAX({cond} r.test_analytics END) AS {safe}_Analytics",
        ])
    return (
        f"CREATE VIEW IF NOT EXISTS {name} AS\nSELECT\n"
        + ",\n".join(selects)
        + "\nFROM clinical_document d\n"
          "LEFT JOIN lab_result r ON r.document_pk = d.id\n"
          "WHERE d.is_duplicate = 0\n"
          "GROUP BY d.id, d.document_id, d.source_system, d.record_type,\n"
          "         d.report_date, d.claim_no;"
    )


# ---------------------------------------------------------------------------
# Operational views - what the dashboard and the alerts read
# ---------------------------------------------------------------------------

OPERATIONAL_VIEWS: tuple[str, ...] = (
    # Per-source data quality (FR-5.4). These are the numbers that turn a
    # vague "the data is messy" into a specific conversation with one clinic.
    """CREATE VIEW IF NOT EXISTS v_clinic_quality AS
SELECT
  r.source_system,
  COUNT(*)                                                            AS rows_total,
  SUM(CASE WHEN r.test_name_canonical IS NULL THEN 1 ELSE 0 END)      AS rows_unmapped_test,
  SUM(CASE WHEN r.test_analytics = 'Invalid' THEN 1 ELSE 0 END)       AS rows_invalid,
  SUM(CASE WHEN r.test_analytics = 'Outlier' THEN 1 ELSE 0 END)       AS rows_outlier,
  SUM(CASE WHEN r.test_analytics IN ('Above Range','Below Range') THEN 1 ELSE 0 END)
                                                                      AS rows_out_of_range,
  SUM(CASE WHEN r.flags LIKE '%unit_dimension_mismatch%' THEN 1 ELSE 0 END)
                                                                      AS rows_unit_mismatch,
  SUM(CASE WHEN r.flags LIKE '%source_interpretation_disagrees%' THEN 1 ELSE 0 END)
                                                                      AS rows_source_disagrees,
  SUM(CASE WHEN r.flags LIKE '%report_misalignment_suspected%' THEN 1 ELSE 0 END)
                                                                      AS rows_in_misaligned_reports,
  SUM(CASE WHEN r.normalization_method IN ('fuzzy','fuzzy_affix') THEN 1 ELSE 0 END)
                                                                      AS rows_fuzzy_matched,
  SUM(CASE WHEN r.normalization_method = 'llm' THEN 1 ELSE 0 END)     AS rows_llm_matched,
  ROUND(AVG(r.normalization_confidence), 4)                           AS avg_confidence,
  ROUND(1.0 * SUM(CASE WHEN r.test_name_canonical IS NULL THEN 1 ELSE 0 END)
        / COUNT(*), 4)                                                AS unmapped_rate,
  ROUND(1.0 * SUM(CASE WHEN r.test_analytics = 'Invalid' THEN 1 ELSE 0 END)
        / COUNT(*), 4)                                                AS invalid_rate
FROM lab_result r
GROUP BY r.source_system;""",

    # Everything an ops analyst needs to triage one flagged row, in one row.
    """CREATE VIEW IF NOT EXISTS v_flagged_rows AS
SELECT
  r.id, r.document_pk, r.source_system, r.file_uri, r.correlation_id,
  r.page_no, r.test_name_original, r.test_name_canonical,
  r.result_text_original, r.unit_original, r.range_text_original,
  r.result_value, r.unit_canonical, r.range_low, r.range_high,
  r.test_analytics, r.normalization_method, r.normalization_confidence,
  r.source_interpretation, r.coherence_note, r.flags,
  d.claim_no, d.report_date, d.hospital_name
FROM lab_result r
JOIN clinical_document d ON d.id = r.document_pk
WHERE r.flags <> '' OR r.test_analytics IN ('Invalid','Outlier');""",

    # Duplicate suppression, made visible. A silent deduplicator is one nobody
    # trusts; this view is how an analyst confirms a claim was not dropped.
    """CREATE VIEW IF NOT EXISTS v_duplicates AS
SELECT
  d.id, d.content_hash, d.record_type, d.source_system, d.claim_no,
  d.document_id, d.file_uri, d.duplicate_of,
  k.document_id AS kept_document_id, k.claim_no AS kept_claim_no,
  k.file_uri AS kept_file_uri
FROM clinical_document d
LEFT JOIN clinical_document k ON k.id = d.duplicate_of
WHERE d.is_duplicate = 1;""",

    # Lineage in one hop (NFR-4.2).
    """CREATE VIEW IF NOT EXISTS v_lineage AS
SELECT
  r.id AS row_id, r.test_name_canonical, r.result_value, r.unit_canonical,
  r.document_pk, r.document_id, r.trace_id, r.correlation_id,
  r.file_uri, r.file_sha256, r.source_system, r.clinic_profile,
  r.schema_version, r.ingested_at, r.processed_at, r.run_id,
  r.source_row_index, r.page_no,
  l.first_seen_at AS file_first_seen_at, l.status AS file_status,
  p.config_fingerprint
FROM lab_result r
LEFT JOIN file_ledger l ON l.file_sha256 = r.file_sha256
LEFT JOIN pipeline_run p ON p.run_id = r.run_id;""",

    # Panel completeness: which members of a panel a report is missing. Useful
    # clinically and as an OCR-quality signal.
    """CREATE VIEW IF NOT EXISTS v_panel_completeness AS
SELECT
  d.id AS document_pk, d.source_system, r.panel,
  COUNT(DISTINCT r.test_name_canonical) AS tests_present,
  SUM(CASE WHEN r.test_analytics = 'Invalid' THEN 1 ELSE 0 END) AS invalid_in_panel
FROM clinical_document d
JOIN lab_result r ON r.document_pk = d.id
WHERE d.is_duplicate = 0 AND r.panel IS NOT NULL
GROUP BY d.id, d.source_system, r.panel;""",
)


def all_views(canonical_tests: Iterable[str]) -> list[str]:
    return [ideal_output_view(), test_wide_view(canonical_tests), *OPERATIONAL_VIEWS]
