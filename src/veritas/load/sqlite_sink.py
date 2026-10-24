"""SQLite sink - the local target, and the one the reviewer runs.

Why SQLite and not DuckDB
-------------------------
DuckDB would be the better analytical engine and the code is written so that
swapping it in is a sink implementation, not a pipeline change. SQLite was
chosen for the submitted prototype for one reason that outranks query speed
here: it is in the Python standard library, so ``make demo`` works on a
reviewer's machine with nothing installed. At the prototype's data volume the
difference is unmeasurable; at production volume neither is the answer -
BigQuery is (``bigquery_sink.py``), and the same schema definitions generate
both.

Idempotency (NFR-3.2)
---------------------
Every primary key is a deterministic content hash, so ``INSERT OR REPLACE``
*is* a MERGE: re-running the same input rewrites the same rows with the same
values and the table is unchanged. There is no read-before-write and no
"have I seen this" bookkeeping, which is what makes the property hold under
parallel workers and after a crash halfway through a batch. The test
``test_idempotency.py`` asserts it by running the whole pipeline twice and
comparing row counts and a checksum.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..models import (
    ClinicalDocument,
    FileLedgerEntry,
    LabResult,
    Medication,
    ProcessingError,
    ReviewItem,
    RunSummary,
)
from ..observe.logging import get_logger
from . import schema

log = get_logger(__name__)


def _flatten(value: Any) -> Any:
    """Coerce a Python value into something SQLite accepts."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return ",".join(str(_enum_value(v)) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(_enum_value(value))


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


class SqliteSink:
    """Transactional, idempotent writer plus the queries the ops UI needs."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        # WAL lets the ops UI read while a run is writing; without it the
        # dashboard blocks exactly when someone wants to watch a run.
        #
        # WAL needs shared-memory support, which network filesystems - SMB,
        # NFS, a synced folder, a VM mount - do not provide. There it fails
        # with a bare "disk I/O error" on the first write, which is a
        # thoroughly unhelpful message to hand a reviewer who simply cloned
        # the repo onto a shared drive. So the mode is attempted, verified,
        # and quietly downgraded if it did not take.
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            mode = (cur.fetchone() or [""])[0]
        except sqlite3.DatabaseError:
            mode = ""
        if str(mode).lower() != "wal":
            cur.execute("PRAGMA journal_mode=DELETE")
            log.info(
                "WAL unavailable on this filesystem; using the rollback journal. "
                "The pipeline is unaffected; the ops UI may block briefly while a "
                "run is writing.",
                extra={"database": str(self.path), "journal_mode": mode or "unknown"},
            )
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        self._conn.commit()

    # -- schema ----------------------------------------------------------
    def initialise(self, canonical_tests: Iterable[str]) -> None:
        cur = self._conn.cursor()
        for statement in schema.sqlite_ddl():
            cur.executescript(statement)
        for view in schema.all_views(canonical_tests):
            try:
                cur.executescript(view)
            except sqlite3.Error as exc:
                # A view failing must not stop a load: the facts are the
                # deliverable, the presentations are derived.
                log.error("could not create view", extra={"error": str(exc)})
        self._conn.commit()

    def rebuild_views(self, canonical_tests: Iterable[str]) -> None:
        """Drop and recreate views, so a catalogue change is reflected.

        Needed because ``v_test_wide`` is generated from the catalogue: add a
        test to config and its five columns should appear without a migration.
        """
        cur = self._conn.cursor()
        for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall():
            cur.execute(f"DROP VIEW IF EXISTS {row['name']}")
        self._conn.commit()
        for view in schema.all_views(canonical_tests):
            try:
                cur.executescript(view)
            except sqlite3.Error as exc:
                log.error("could not create view", extra={"error": str(exc)})
        self._conn.commit()

    # -- writing ---------------------------------------------------------
    def _upsert(self, table: schema.Table, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        names = [c.name for c in table.columns]
        placeholders = ",".join("?" for _ in names)
        sql = (f"INSERT OR REPLACE INTO {table.name} ({','.join(names)}) "
               f"VALUES ({placeholders})")
        payload = [tuple(_flatten(row.get(n)) for n in names) for row in rows]
        with closing(self._conn.cursor()) as cur:
            cur.executemany(sql, payload)
        return len(payload)

    def write_documents(self, documents: Iterable[ClinicalDocument]) -> int:
        rows = [_document_row(doc) for doc in documents]
        return self._upsert(schema.CLINICAL_DOCUMENT, rows)

    def write_lab_results(self, results: Iterable[LabResult]) -> int:
        return self._upsert(schema.LAB_RESULT, [_lab_row(r) for r in results])

    def write_medications(self, medications: Iterable[Medication]) -> int:
        return self._upsert(schema.MEDICATION, [_medication_row(m) for m in medications])

    def write_errors(self, errors: Iterable[ProcessingError]) -> int:
        return self._upsert(
            schema.PROCESSING_ERROR,
            [{**asdict(e)} for e in errors],
        )

    def write_review_items(self, items: Iterable[ReviewItem]) -> int:
        return self._upsert(schema.REVIEW_ITEM, [{**asdict(i)} for i in items])

    def write_file_ledger(self, entries: Iterable[FileLedgerEntry]) -> int:
        return self._upsert(schema.FILE_LEDGER, [{**asdict(e)} for e in entries])

    def write_run(self, summary: RunSummary, *, resolver_stats: dict[str, Any] | None = None,
                  llm_usage: dict[str, Any] | None = None) -> None:
        row = {**asdict(summary),
               "resolver_stats": json.dumps(resolver_stats or {}),
               "llm_usage": json.dumps(llm_usage or {})}
        self._upsert(schema.PIPELINE_RUN, [row])
        self._conn.commit()

    def write_learned_aliases(self, decisions: Iterable[dict[str, Any]]) -> int:
        return self._upsert(schema.LEARNED_ALIAS, list(decisions))

    def load_learned_aliases(self) -> dict[str, str]:
        """Seed the resolver's learned-alias cache from previous runs."""
        try:
            rows = self._conn.execute(
                "SELECT source_name_normalised, canonical FROM learned_alias"
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {r["source_name_normalised"]: r["canonical"] for r in rows}

    def seen_file_hashes(self) -> set[str]:
        try:
            rows = self._conn.execute(
                "SELECT file_sha256 FROM file_ledger WHERE status = 'processed'"
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {r["file_sha256"] for r in rows}

    def known_content_hashes(self) -> dict[str, str]:
        """Content hash -> document id, for cross-run duplicate detection.

        The in-run registry catches duplicates inside one batch; this catches
        the same report resubmitted next week, which is the case that actually
        causes double adjudication.
        """
        try:
            rows = self._conn.execute(
                "SELECT content_hash, id FROM clinical_document WHERE is_duplicate = 0"
            ).fetchall()
        except sqlite3.Error:
            return {}
        return {r["content_hash"]: r["id"] for r in rows}

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # -- reading (ops UI) ------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def table_exists(self, name: str) -> bool:
        return self.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = ? AND type IN ('table','view')",
            (name,),
        ) > 0

    def checksum(self) -> str:
        """Order-independent content checksum, used to prove idempotency."""
        value = self.scalar(
            "SELECT COALESCE(GROUP_CONCAT(h), '') FROM ("
            "  SELECT id || ':' || COALESCE(CAST(result_value AS TEXT),'') || ':' "
            "         || COALESCE(test_analytics,'') AS h"
            "  FROM lab_result ORDER BY id)"
        ) or ""
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def __enter__(self) -> SqliteSink:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Row mappers. Explicit rather than reflective: a column rename should be a
# visible, reviewable edit, not a silent behaviour change.
# ---------------------------------------------------------------------------

def _lineage_cols(lineage: Any) -> dict[str, Any]:
    return {
        "file_uri": lineage.file_uri,
        "file_sha256": lineage.file_sha256,
        "document_id": lineage.document_id,
        "trace_id": lineage.trace_id,
        "correlation_id": lineage.correlation_id,
        "source_system": lineage.source_system,
        "clinic_profile": lineage.clinic_profile,
        "schema_version": lineage.schema_version,
        "ingested_at": lineage.ingested_at,
        "processed_at": lineage.processed_at,
        "run_id": lineage.run_id,
        "source_row_index": lineage.source_row_index,
    }


def _document_row(doc: ClinicalDocument) -> dict[str, Any]:
    return {
        "id": doc.id,
        "record_type": _enum_value(doc.record_type),
        **_lineage_cols(doc.lineage),
        "claim_no": doc.claim_no,
        "nt_code": doc.nt_code,
        "consumer_client_id": doc.consumer_client_id,
        "destination_identifier": doc.destination_identifier,
        "case_id": doc.case_id,
        "patient_token": doc.patient_token,
        "patient_name": doc.patient_name,
        "age_years": doc.age_years,
        "age_text": doc.age_text,
        "gender": doc.gender,
        "uhid_token": doc.uhid_token,
        "hospital_name": doc.hospital_name,
        "hospital_address": doc.hospital_address,
        "doctor_name": doc.doctor_name,
        "ward": doc.ward,
        "admission_date": doc.admission_date,
        "discharge_date": doc.discharge_date,
        "report_date": doc.report_date,
        "bill_date": doc.bill_date,
        "diagnosis": doc.diagnosis,
        "brief_history": doc.brief_history,
        "general_examinations": doc.general_examinations,
        "recommendations": doc.recommendations,
        "post_discharge_advice": doc.post_discharge_advice,
        "course_during_hospitalisation": doc.course_during_hospitalisation,
        "content_hash": doc.content_hash,
        "is_duplicate": int(doc.is_duplicate),
        "duplicate_of": doc.duplicate_of,
        "row_count": doc.row_count,
        "flags": ",".join(doc.flag_values()),
        "raw_payload": doc.raw_payload,
    }


def _lab_row(result: LabResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "document_pk": result.document_pk,
        **_lineage_cols(result.lineage),
        "page_no": result.lineage.page_no,
        "occurrence_index": result.occurrence_index,
        "test_name_canonical": result.test_name_canonical,
        "test_name_original": result.test_name_original,
        "loinc_code": result.loinc_code,
        "panel": result.panel,
        "result_value": result.result_value,
        "result_text": result.result_text,
        "result_text_original": result.result_original,
        "unit_canonical": result.unit_canonical,
        "unit_original": result.unit_original,
        "range_low": result.range_low,
        "range_high": result.range_high,
        "range_text": result.range_text,
        "range_text_original": result.range_original,
        "test_analytics": _enum_value(result.test_analytics),
        "range_source": result.range_source,
        "normalization_method": _enum_value(result.normalization_method),
        "normalization_confidence": result.normalization_confidence,
        "assay_method": result.assay_method,
        "source_interpretation": result.source_interpretation,
        "coherence_note": result.coherence_note,
        "flags": ",".join(result.flag_values()),
    }


def _medication_row(med: Medication) -> dict[str, Any]:
    return {
        "id": med.id,
        "document_pk": med.document_id_fk,
        **_lineage_cols(med.lineage),
        "occurrence_index": med.occurrence_index,
        "medicine_original": med.medicine_original,
        "medicine_generic": med.medicine_generic,
        "brand_matched": med.brand_matched,
        "atc_code": med.atc_code,
        "dosage_form": med.dosage_form,
        "strength": med.strength,
        "dose": med.dose,
        "frequency": med.frequency,
        "medicine_type": med.medicine_type,
        "is_combination": int(med.is_combination),
        "confidence": med.confidence,
        "flags": ",".join(med.flag_values()),
    }
