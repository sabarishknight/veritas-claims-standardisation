"""SQL for the operational UI, kept out of the rendering layer.

Every query here answers a question an operations analyst actually asks. They
are parameterised, bounded by LIMIT, and read through the views defined in
``load/schema.py`` wherever one exists, so the dashboard and the warehouse
cannot disagree about what "flagged" means.
"""

from __future__ import annotations

from typing import Any

from ..load.sqlite_sink import SqliteSink

ANALYTICS_ORDER = ["Within Range", "Above Range", "Below Range",
                   "Outlier", "Invalid", "Not Applicable"]

# Status role per classification. Used for the leading dot and the label, never
# as the bar fill: four status hues side by side fail the adjacent-pair
# separation gate, so magnitude is carried by one sequential hue and identity by
# the dot plus its text.
ANALYTICS_STATUS = {
    "Within Range": "good",
    "Above Range": "warning",
    "Below Range": "warning",
    "Outlier": "serious",
    "Invalid": "critical",
    "Not Applicable": "neutral",
}


def latest_run(sink: SqliteSink) -> dict[str, Any] | None:
    rows = sink.query(
        "SELECT * FROM pipeline_run ORDER BY started_at DESC LIMIT 1")
    return rows[0] if rows else None


def all_runs(sink: SqliteSink, limit: int = 25) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT run_id, started_at, finished_at, files_processed, rows_emitted, "
        "       rows_flagged, errors_emitted, duration_seconds, config_fingerprint "
        "FROM pipeline_run ORDER BY started_at DESC LIMIT ?", (limit,))


def headline(sink: SqliteSink) -> dict[str, Any]:
    """The numbers in the stat tiles."""
    return {
        "files": sink.scalar("SELECT COUNT(*) FROM file_ledger") or 0,
        "files_failed": sink.scalar(
            "SELECT COUNT(*) FROM file_ledger WHERE status='failed'") or 0,
        "documents": sink.scalar(
            "SELECT COUNT(*) FROM clinical_document WHERE is_duplicate=0") or 0,
        "duplicates": sink.scalar(
            "SELECT COUNT(*) FROM clinical_document WHERE is_duplicate=1") or 0,
        "rows": sink.scalar("SELECT COUNT(*) FROM lab_result") or 0,
        "flagged": sink.scalar("SELECT COUNT(*) FROM lab_result WHERE flags<>''") or 0,
        # Severe flags are the ones that stop a value being used. Separated from
        # the advisory ones (a missing unit, a missing range) because a single
        # "flagged" number that mixes the two is alarming and useless.
        "withheld": sink.scalar(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE test_analytics IN ('Invalid','Outlier')") or 0,
        "usable": sink.scalar(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE test_analytics IN ('Within Range','Above Range','Below Range')") or 0,
        "invalid": sink.scalar(
            "SELECT COUNT(*) FROM lab_result WHERE test_analytics='Invalid'") or 0,
        "outliers": sink.scalar(
            "SELECT COUNT(*) FROM lab_result WHERE test_analytics='Outlier'") or 0,
        "medications": sink.scalar("SELECT COUNT(*) FROM medication") or 0,
        "errors": sink.scalar("SELECT COUNT(*) FROM processing_error") or 0,
        "reviews_open": sink.scalar(
            "SELECT COUNT(*) FROM review_item WHERE status='open'") or 0,
        "unmapped": sink.scalar(
            "SELECT COUNT(*) FROM lab_result WHERE test_name_canonical IS NULL") or 0,
        "misaligned": sink.scalar(
            "SELECT COUNT(*) FROM review_item WHERE kind='misaligned_report'") or 0,
        "sources": sink.scalar(
            "SELECT COUNT(DISTINCT source_system) FROM lab_result") or 0,
    }


def analytics_distribution(sink: SqliteSink) -> list[dict[str, Any]]:
    rows = {r["test_analytics"]: r["n"] for r in sink.query(
        "SELECT test_analytics, COUNT(*) n FROM lab_result GROUP BY 1")}
    return [{"label": label, "value": rows.get(label, 0),
             "status": ANALYTICS_STATUS.get(label, "neutral")}
            for label in ANALYTICS_ORDER if rows.get(label)]


def resolution_methods(sink: SqliteSink) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT normalization_method AS label, COUNT(*) AS value "
        "FROM lab_result GROUP BY 1 ORDER BY value DESC")


def flag_counts(sink: SqliteSink, limit: int = 18) -> list[dict[str, Any]]:
    """Flags are stored comma-separated, so they are counted in Python.

    A normalised flag table would make this a GROUP BY, and that is the right
    production shape; for the prototype the simpler column keeps the row
    self-describing when an analyst selects it directly. Noted in
    docs/assumptions.md D-10.
    """
    counts: dict[str, int] = {}
    for row in sink.query("SELECT flags FROM lab_result WHERE flags<>''"):
        for flag in str(row["flags"]).split(","):
            if flag:
                counts[flag] = counts.get(flag, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [{"label": k, "value": v} for k, v in ordered]


def clinic_quality(sink: SqliteSink) -> list[dict[str, Any]]:
    return sink.query("SELECT * FROM v_clinic_quality ORDER BY rows_total DESC")


def dlq_reasons(sink: SqliteSink) -> list[dict[str, Any]]:
    # Aliased to both `reason` and `label` so the same rows feed a table
    # (which keys on the column name) and a bar chart (which keys on `label`).
    return sink.query(
        "SELECT reason, reason AS label, stage, COUNT(*) AS value "
        "FROM processing_error GROUP BY reason, stage ORDER BY value DESC")


def review_queue(sink: SqliteSink, kind: str | None = None,
                 limit: int = 200) -> list[dict[str, Any]]:
    if kind:
        return sink.query(
            "SELECT * FROM review_item WHERE kind=? AND status='open' "
            "ORDER BY priority, created_at LIMIT ?", (kind, limit))
    return sink.query(
        "SELECT * FROM review_item WHERE status='open' "
        "ORDER BY priority, created_at LIMIT ?", (limit,))


def review_kinds(sink: SqliteSink) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT kind AS label, MIN(priority) AS priority, COUNT(*) AS value "
        "FROM review_item WHERE status='open' GROUP BY kind "
        "ORDER BY priority, value DESC")


def flagged_rows(sink: SqliteSink, *, flag: str | None = None,
                 analytics: str | None = None, source: str | None = None,
                 limit: int = 300) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if flag:
        clauses.append("flags LIKE ?")
        params.append(f"%{flag}%")
    if analytics:
        clauses.append("test_analytics = ?")
        params.append(analytics)
    if source:
        clauses.append("source_system = ?")
        params.append(source)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return sink.query(
        f"SELECT * FROM v_flagged_rows WHERE 1=1{where} "
        f"ORDER BY CASE test_analytics WHEN 'Invalid' THEN 0 WHEN 'Outlier' THEN 1 "
        f"ELSE 2 END, test_name_original LIMIT ?", params)


def documents(sink: SqliteSink, *, search: str | None = None,
              limit: int = 120) -> list[dict[str, Any]]:
    base = (
        "SELECT d.id, d.document_id, d.record_type, d.source_system, d.claim_no, "
        "       d.hospital_name, d.report_date, d.admission_date, d.discharge_date, "
        "       d.is_duplicate, d.flags, d.file_uri, "
        "       (SELECT COUNT(*) FROM lab_result r WHERE r.document_pk = d.id) AS rows_n, "
        "       (SELECT COUNT(*) FROM medication m WHERE m.document_pk = d.id) AS meds_n "
        "FROM clinical_document d "
    )
    if search:
        like = f"%{search}%"
        return sink.query(
            base + "WHERE d.document_id LIKE ? OR d.claim_no LIKE ? "
                   "OR d.hospital_name LIKE ? OR d.id LIKE ? "
                   "ORDER BY d.is_duplicate, d.report_date DESC LIMIT ?",
            (like, like, like, like, limit))
    return sink.query(
        base + "ORDER BY d.is_duplicate, d.report_date DESC LIMIT ?", (limit,))


def document(sink: SqliteSink, document_pk: str) -> dict[str, Any] | None:
    rows = sink.query("SELECT * FROM clinical_document WHERE id = ?", (document_pk,))
    return rows[0] if rows else None


def document_rows(sink: SqliteSink, document_pk: str) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT * FROM lab_result WHERE document_pk = ? "
        "ORDER BY source_row_index, occurrence_index", (document_pk,))


def document_medications(sink: SqliteSink, document_pk: str) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT * FROM medication WHERE document_pk = ? ORDER BY source_row_index",
        (document_pk,))


def document_errors(sink: SqliteSink, document_id: str) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT * FROM processing_error WHERE document_id = ? "
        "ORDER BY source_row_index", (document_id,))


def duplicates(sink: SqliteSink, limit: int = 100) -> list[dict[str, Any]]:
    return sink.query("SELECT * FROM v_duplicates LIMIT ?", (limit,))


def lineage(sink: SqliteSink, row_id: str) -> dict[str, Any] | None:
    rows = sink.query("SELECT * FROM v_lineage WHERE row_id = ?", (row_id,))
    return rows[0] if rows else None


def learned_aliases(sink: SqliteSink, limit: int = 100) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT * FROM learned_alias ORDER BY created_at DESC LIMIT ?", (limit,))


def source_systems(sink: SqliteSink) -> list[str]:
    return [r["source_system"] for r in sink.query(
        "SELECT DISTINCT source_system FROM lab_result ORDER BY 1")]


def source_disagreement(sink: SqliteSink) -> dict[str, int]:
    """How often the lab's own verdict disagrees with the arithmetic.

    Reported with its denominator because the figure is meaningless without
    one: it is the share of rows where an interpretation *and* a usable value
    both exist.
    """
    checkable = sink.scalar(
        "SELECT COUNT(*) FROM lab_result WHERE source_interpretation IS NOT NULL "
        "AND source_interpretation <> '' AND result_value IS NOT NULL") or 0
    disagree = sink.scalar(
        "SELECT COUNT(*) FROM lab_result "
        "WHERE flags LIKE '%source_interpretation_disagrees%'") or 0
    return {"checkable": checkable, "disagree": disagree}


def misalignments(sink: SqliteSink) -> list[dict[str, Any]]:
    return sink.query(
        "SELECT * FROM review_item WHERE kind='misaligned_report' "
        "ORDER BY created_at DESC")


def unit_conversions(sink: SqliteSink, limit: int = 40) -> list[dict[str, Any]]:
    """Evidence that FR-2.4 is working: before and after, per row."""
    return sink.query(
        "SELECT test_name_canonical, result_text_original, unit_original, "
        "       result_value, unit_canonical, range_low, range_high, test_analytics "
        "FROM lab_result WHERE flags LIKE '%unit_converted%' "
        "ORDER BY test_name_canonical LIMIT ?", (limit,))
