"""Pipeline orchestration.

Shape of a run
--------------
    discover files  ->  for each chunk of N files:
                            parse envelope
                            route documents by classifier
                            suppress duplicates
                            standardise rows
                            check coherence (row, then report)
                            resolve the unmapped tail (one batched LLM call)
                            classify
                            write in one transaction
                        ->  write run summary

Three decisions worth defending:

**Chunked, not whole-run, buffering.** Rows are held in memory for one chunk
only. That bounds memory independently of input size - 200,000 files run in the
same footprint as 5 - and it gives the LLM tier a batch big enough to be cheap
without waiting for the whole run to finish.

**Single writer, parallel parse.** Parsing and standardising are CPU-bound and
embarrassingly parallel; writing is not. Workers return plain rows and the
parent performs every write, so there is no database contention, no
distributed transaction, and the idempotency argument stays simple. Scaling out
is then just more parse workers (NFR-1.3) - which in the deployed topology is
more Cloud Run instances, each a single writer to BigQuery.

**A failure is a row, not an exception.** Any file that cannot be read, routed
or standardised becomes a dead-letter record and the run continues (NFR-3.1).
``fail_fast`` exists for development only; the default is false, because one
clinic's malformed upload must never stop the other 499.
"""

from __future__ import annotations

import json
import time
import traceback
import uuid
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .config import Config, load_config, normalise_name
from .ingest import dedup
from .ingest.envelope import EnvelopeError, parse_json, unwrap
from .ingest.source import FileRef, build_source
from .load.sqlite_sink import SqliteSink
from .models import (
    Analytics,
    ClinicalDocument,
    FileLedgerEntry,
    Flag,
    LabResult,
    Lineage,
    Medication,
    Method,
    ProcessingError,
    RecordType,
    ReviewItem,
    RunSummary,
)
from .observe.logging import get_logger, log_context
from .standardise import llm as llm_module
from .standardise.demographics import parse_age, parse_date, parse_gender
from .standardise.medicines import MedicineResolver
from .standardise.names import NameResolver
from .standardise.numeric import extract_embedded_values, parse_result
from .standardise.pii import Tokeniser
from .standardise.ranges import parse_range
from .validate.coherence import CoherenceEngine
from .validate.rules import classify, harmonise

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Batch:
    """Everything produced from one chunk of files, before it is written."""

    documents: list[ClinicalDocument] = field(default_factory=list)
    lab_results: list[LabResult] = field(default_factory=list)
    medications: list[Medication] = field(default_factory=list)
    errors: list[ProcessingError] = field(default_factory=list)
    reviews: list[ReviewItem] = field(default_factory=list)
    ledger: list[FileLedgerEntry] = field(default_factory=list)
    unmapped_names: dict[str, list[LabResult]] = field(default_factory=dict)
    misaligned_reports: int = 0

    def extend(self, other: Batch) -> None:
        self.documents.extend(other.documents)
        self.lab_results.extend(other.lab_results)
        self.medications.extend(other.medications)
        self.errors.extend(other.errors)
        self.reviews.extend(other.reviews)
        self.ledger.extend(other.ledger)
        for name, rows in other.unmapped_names.items():
            self.unmapped_names.setdefault(name, []).extend(rows)
        self.misaligned_reports += other.misaligned_reports


class Standardiser:
    """Stateless-per-file standardisation. Built once per process."""

    def __init__(self, config: Config, run_id: str,
                 learned_aliases: dict[str, str] | None = None) -> None:
        self.config = config
        self.run_id = run_id
        resolver_cfg = config.section("resolver")
        self.names = NameResolver(
            config.catalog,
            accept_threshold=float(resolver_cfg.get("fuzzy_accept_threshold", 0.86)),
            review_threshold=float(resolver_cfg.get("fuzzy_review_threshold", 0.70)),
            affix_tolerance=int(resolver_cfg.get("affix_tolerance", 3)),
            max_candidates=int(resolver_cfg.get("max_candidates", 5)),
            learned_aliases=learned_aliases or {},
        )
        self.medicines = MedicineResolver(config.medicines)
        self.coherence = CoherenceEngine(config.catalog, config.section("coherence"))
        pii_cfg = config.section("pii")
        self.tokeniser = Tokeniser(
            mode=str(pii_cfg.get("mode", "tokenise")),
            fields=pii_cfg.get("tokenise_fields") or (),
            keep_plaintext=pii_cfg.get("keep_plaintext_fields") or (),
            key_env=str(pii_cfg.get("hmac_key_env", "VERITAS_PII_KEY")),
            redaction_markers=pii_cfg.get("redaction_markers") or (),
        )
        validation = config.section("validation")
        self.garbage_tokens = tuple(validation.get("ocr_garbage_tokens") or ())
        self.redaction_markers = tuple(pii_cfg.get("redaction_markers") or ())
        self.dedup_cfg = config.section("dedup")
        audit_cfg = config.section("audit")
        self.retain_raw = bool(audit_cfg.get("retain_raw_payload", True))
        self.max_raw_bytes = int(audit_cfg.get("max_raw_payload_bytes", 262144))
        self.registry = dedup.SeenRegistry()
        self.known_hashes: dict[str, str] = {}

    # -- file ------------------------------------------------------------
    def process_file(self, ref: FileRef) -> Batch:
        batch = Batch()
        ingested_at = _now()

        if not ref.raw:
            batch.errors.append(self._error(ref, "read", "empty_or_oversized_file",
                                            f"{ref.size_bytes} bytes"))
            batch.ledger.append(self._ledger(ref, ingested_at, "failed", errors=1))
            return batch

        try:
            document = parse_json(ref.raw, ref.uri)
        except EnvelopeError as exc:
            batch.errors.append(self._error(ref, "parse", exc.reason, exc.detail,
                                            excerpt=ref.raw[:400].decode("utf-8", "replace")))
            batch.ledger.append(self._ledger(ref, ingested_at, "failed", errors=1))
            return batch

        # The profile is chosen from envelope metadata, so the envelope has to
        # be read with the fallback profile first. An unknown source is
        # processed under `_default`, never rejected (FR-1.3).
        fallback = self.config.profile_for({})
        try:
            probe = unwrap(document, fallback)
        except EnvelopeError as exc:
            batch.errors.append(self._error(ref, "route", exc.reason, exc.detail))
            batch.ledger.append(self._ledger(ref, ingested_at, "failed", errors=1))
            return batch

        profile = self.config.profile_for(probe.meta)
        try:
            envelope = unwrap(document, profile) if profile.clinic_id != fallback.clinic_id else probe
        except EnvelopeError as exc:
            batch.errors.append(self._error(ref, "route", exc.reason, exc.detail))
            batch.ledger.append(self._ledger(ref, ingested_at, "failed", errors=1))
            return batch

        if envelope.unmapped_meta_keys:
            # A metadata key we have never seen is the earliest signal that a
            # clinic changed its format (NFR-2.3).
            batch.reviews.append(self._review(
                kind="unknown_metadata_key", priority=4,
                document_id=envelope.document_id, source_system=envelope.source_system,
                subject=", ".join(sorted(set(envelope.unmapped_meta_keys))),
                evidence=f"{ref.uri} carried metadata keys absent from profile "
                         f"{profile.clinic_id!r}",
                proposal="add to envelope.meta.map or confirm it can be dropped",
            ))

        with log_context(file_sha256=ref.sha256, trace_id=envelope.trace_id,
                         correlation_id=envelope.correlation_id,
                         source_system=envelope.source_system):
            for raw_doc in envelope.documents:
                spec = profile.doc_spec(raw_doc.classifier)
                if spec is None:
                    batch.errors.append(self._error(
                        ref, "route", "unknown_classifier",
                        f"profile {profile.clinic_id!r} has no shape for "
                        f"{raw_doc.classifier!r}",
                        document_id=envelope.document_id,
                        row_index=raw_doc.index,
                    ))
                    continue
                if raw_doc.status and str(raw_doc.status).lower() not in ("success", "true", "ok"):
                    batch.errors.append(self._error(
                        ref, "route", "source_marked_failed",
                        f"status={raw_doc.status!r} error={raw_doc.error_message!r}",
                        document_id=envelope.document_id, row_index=raw_doc.index,
                    ))
                    continue
                try:
                    self._process_document(ref, envelope, profile, spec, raw_doc,
                                           ingested_at, batch)
                except Exception as exc:                         # noqa: BLE001
                    if self.config.section("runtime").get("fail_fast"):
                        raise
                    batch.errors.append(self._error(
                        ref, "standardise", "unhandled_exception",
                        f"{type(exc).__name__}: {exc}",
                        document_id=envelope.document_id, row_index=raw_doc.index,
                        excerpt=traceback.format_exc()[-800:],
                    ))

        batch.ledger.append(self._ledger(
            ref, ingested_at, "processed",
            documents=len(batch.documents), rows=len(batch.lab_results),
            errors=len(batch.errors),
        ))
        return batch

    # -- document --------------------------------------------------------
    def _process_document(self, ref: FileRef, envelope: Any, profile: Any,
                          spec: dict[str, Any], raw_doc: Any,
                          ingested_at: str, batch: Batch) -> None:
        from .ingest.envelope import first_present, resolve_many, resolve_path

        payload = raw_doc.payload
        if payload is None:
            batch.errors.append(self._error(
                ref, "route", "empty_payload", f"classifier={raw_doc.classifier}",
                document_id=envelope.document_id, row_index=raw_doc.index))
            return

        header_path = spec.get("header_path", ".")
        header_src = resolve_path(payload, header_path) or {}
        header = {
            field_name: first_present(header_src, candidates)
            for field_name, candidates in (spec.get("header_map") or {}).items()
        }

        strategy = str((profile.dedup or {}).get("strategy")
                       or self.dedup_cfg.get("strategy", "content_hash"))
        excludes = self.dedup_cfg.get("content_hash_excludes") or ()
        record_type = (RecordType.LAB_RESULT if raw_doc.classifier == "lab_report"
                       else RecordType.DISCHARGE_SUMMARY)
        content_hash = dedup.content_hash(payload, excludes)
        document_pk = dedup.document_identity(
            strategy=strategy, payload=payload, header=header,
            record_type=raw_doc.classifier, document_id=envelope.document_id,
            source_row_index=raw_doc.index, excludes=excludes,
        )

        lineage = Lineage(
            file_uri=ref.uri, file_sha256=ref.sha256,
            document_id=envelope.document_id, trace_id=envelope.trace_id,
            correlation_id=envelope.correlation_id,
            source_system=envelope.source_system,
            clinic_profile=profile.clinic_id, schema_version=profile.schema_version,
            ingested_at=ingested_at, processed_at=_now(), run_id=self.run_id,
            source_row_index=raw_doc.index,
        )

        # Duplicate suppression: in-run registry plus the cross-run content
        # hashes loaded from the warehouse.
        #
        # A document is never its own duplicate. Reprocessing the same file -
        # after a catalogue change, or to replay a dead letter - must re-emit
        # the same rows so the MERGE refreshes them. Treating a known content
        # hash as a duplicate regardless of which document it belongs to would
        # make every rerun a no-op, and the warehouse would quietly keep stale
        # values after a config fix. Only a hash that maps to a *different*
        # document is a genuine resubmission.
        first_seen = self.registry.check(document_pk, f"{ref.uri}#{raw_doc.index}")
        prior = self.known_hashes.get(content_hash)
        is_duplicate = first_seen is not None or (prior is not None and prior != document_pk)

        doc = self._build_document(
            document_pk, lineage, record_type, envelope, header, profile,
            content_hash, is_duplicate,
            duplicate_of=(prior or document_pk if is_duplicate else None),
        )
        doc.raw_payload = self._raw_payload(payload)
        if is_duplicate:
            doc.flags.append(Flag.DUPLICATE_SUPPRESSED)
        else:
            self.known_hashes[content_hash] = document_pk

        batch.documents.append(doc)
        if is_duplicate:
            # Recorded for audit (v_duplicates) but contributes no facts.
            return

        if raw_doc.classifier == "lab_report":
            rows = resolve_many(payload, spec.get("rows_path", ""))
            self._process_lab_rows(rows, spec, doc, lineage, batch)
        else:
            meds = resolve_many(payload, spec.get("medications_path", ""))
            self._process_medications(meds, spec, doc, lineage, batch)

        doc.row_count = sum(1 for r in batch.lab_results if r.document_pk == doc.id)

    def _raw_payload(self, payload: Any) -> str | None:
        """The clinic's JSON for this document, for the audit trail (FR-4.3).

        Truncated rather than dropped above the size ceiling: a pointer to the
        source object is more useful than nothing, and the object itself is
        still in GCS.
        """
        if not self.retain_raw:
            return None
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        except (TypeError, ValueError):
            return None
        if len(text) > self.max_raw_bytes:
            return json.dumps({
                "_truncated": True,
                "_reason": f"payload exceeds audit.max_raw_payload_bytes "
                           f"({self.max_raw_bytes})",
                "_see": "the authoritative copy is the source object at file_uri",
                "_bytes": len(text),
            }, indent=1)
        return text

    def _build_document(self, pk: str, lineage: Lineage, record_type: RecordType,
                        envelope: Any, header: dict[str, Any], profile: Any,
                        content_hash: str, is_duplicate: bool,
                        duplicate_of: str | None) -> ClinicalDocument:
        flags: list[Flag] = []
        # Date formats are a property of the source, so they come from the
        # profile rather than being guessed per value: "03-04-2025" is
        # unknowable in isolation and unambiguous once the clinic is known.
        formats = profile.date_formats

        patient_token, patient_name = self.tokeniser.process(
            "patient_name", header.get("patient_name"))
        uhid_token, _ = self.tokeniser.process("uhid", header.get("uhid"))
        _, doctor_name = self.tokeniser.process("doctor_name", header.get("doctor_name"))
        _, hospital_address = self.tokeniser.process(
            "hospital_address", header.get("hospital_address"))

        age = parse_age(header.get("age"), self.redaction_markers)
        flags.extend(age.flags)
        gender, _gender_raw = parse_gender(header.get("gender"), self.redaction_markers)

        dates: dict[str, str | None] = {}
        for target, source_key in (
            ("admission_date", "admission_date"),
            ("discharge_date", "discharge_date"),
            ("report_date", "reports_date"),
            ("bill_date", "bill_date"),
        ):
            iso, _original, date_flags = parse_date(
                header.get(source_key), formats, self.redaction_markers)
            dates[target] = iso
            flags.extend(date_flags)

        return ClinicalDocument(
            id=pk, lineage=lineage, record_type=record_type,
            claim_no=envelope.meta.get("claim_no"),
            nt_code=envelope.meta.get("nt_code"),
            consumer_client_id=envelope.meta.get("consumer_client_id"),
            destination_identifier=envelope.meta.get("destination_identifier"),
            case_id=envelope.meta.get("case_id"),
            patient_token=patient_token, patient_name=patient_name,
            age_years=age.years, age_text=age.text, gender=gender,
            uhid_token=uhid_token,
            hospital_name=(header.get("hospital_name")
                           or header.get("lab_or_hospital_name")),
            hospital_address=hospital_address,
            doctor_name=doctor_name, ward=header.get("ward"),
            admission_date=dates["admission_date"],
            discharge_date=dates["discharge_date"],
            report_date=dates["report_date"], bill_date=dates["bill_date"],
            diagnosis=header.get("diagnosis"),
            brief_history=header.get("brief_history"),
            general_examinations=header.get("general_examinations"),
            recommendations=header.get("recommendations"),
            post_discharge_advice=header.get("post_discharge_advice"),
            course_during_hospitalisation=_as_text(
                header.get("course_during_hospitalisation")),
            content_hash=content_hash, is_duplicate=is_duplicate,
            duplicate_of=duplicate_of, flags=flags,
        )

    # -- lab rows --------------------------------------------------------
    def _process_lab_rows(self, raw_rows: list[Any], spec: dict[str, Any],
                          doc: ClinicalDocument, lineage: Lineage,
                          batch: Batch) -> None:
        """Standardise a lab report's rows.

        Everything source-specific arrives via ``spec`` - the profile's
        already-resolved document shape - so this never needs the profile
        itself. Passing it anyway would invite reaching past the resolved
        spec into raw config from deep in the row loop.
        """
        from .ingest.envelope import first_present

        row_map = spec.get("row_map") or {}
        null_tokens = {str(t).casefold() for t in (spec.get("null_tokens") or [])}
        prefix_markers = spec.get("result_prefix_markers") or {}
        semantics = str(spec.get("source_interpretation_semantics", "unknown"))
        vocabulary = {str(v).casefold() for v in (spec.get("interpretation_vocabulary") or [])}

        normalised: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            normalised.append({
                "test_name": _blank_if_null(first_present(raw, row_map.get("test_name")), null_tokens),
                "result": _blank_if_null(first_present(raw, row_map.get("result")), null_tokens),
                "range": _blank_if_null(first_present(raw, row_map.get("range")), null_tokens),
                "unit": _blank_if_null(first_present(raw, row_map.get("unit")), null_tokens),
                "page_no": first_present(raw, row_map.get("page_no")),
                "source_interpretation": first_present(
                    raw, row_map.get("source_interpretation")),
            })

        collapse = dedup.collapse_repeat_rows(
            normalised,
            enabled=bool(self.dedup_cfg.get("collapse_repeat_rows_within_report", True)),
            keep_first=bool(self.dedup_cfg.get("keep_first_on_collapse", True)),
        )
        rows = collapse.kept
        if collapse.collapsed:
            log.info("collapsed repeat rows within report",
                     extra={"collapsed": collapse.collapsed, "kept": len(rows)})

        # --- pass 1: resolve names and parse, so coherence can see the report
        parsed: list[dict[str, Any]] = []
        occurrence: dict[str, int] = {}
        for index, row in enumerate(rows):
            name = str(row["test_name"] or "")
            resolution = self.names.resolve(name, row["unit"])
            test = (self.config.catalog.tests.get(resolution.canonical)
                    if resolution.canonical else None)
            rng = parse_range(
                row["range"],
                qualitative_expected=bool(test and test.value_type == "qualitative"),
            )
            result = parse_result(
                row["result"], test=test, range_original=row["range"],
                garbage_tokens=self.garbage_tokens, prefix_markers=prefix_markers,
            )
            parsed.append({
                "index": index, "row": row, "resolution": resolution,
                "test": test, "range": rng, "result": result,
            })
            key = resolution.canonical or f"__raw__{normalise_name(name)}"
            occurrence[key] = occurrence.get(key, 0)

        # --- pass 2: report-level coherence
        tests = [p["test"] for p in parsed]
        units = [str(p["row"]["unit"] or "") for p in parsed]
        ranges = [p["range"] for p in parsed]
        verdicts = [
            self.coherence.check_row(p["index"], p["test"], units[p["index"]], ranges[p["index"]])
            for p in parsed
        ]
        misalignment = self.coherence.detect_misalignment(tests, units, ranges, verdicts)
        if misalignment.suspected:
            batch.misaligned_reports += 1
            doc.flags.append(Flag.REPORT_MISALIGNMENT_SUSPECTED)
            batch.reviews.append(self._review(
                kind="misaligned_report", priority=1,
                document_id=doc.lineage.document_id,
                source_system=doc.lineage.source_system,
                subject=f"{doc.lineage.file_uri} (document {doc.lineage.source_row_index})",
                evidence=misalignment.summary() + " | " + " | ".join(misalignment.evidence[:4]),
                proposal=(f"shift value columns by {misalignment.offset:+d} after human "
                          f"confirmation; not applied automatically"),
            ))
            log.warning("report misalignment suspected",
                        extra={"offset": misalignment.offset,
                               "support": misalignment.support,
                               "failing_rows": misalignment.failing_rows,
                               "document_id": doc.lineage.document_id})

        # --- pass 3: emit
        for p, verdict in zip(parsed, verdicts, strict=True):
            self._emit_lab_row(p, verdict, misalignment, occurrence, semantics,
                               vocabulary, doc, lineage, batch)

    def _emit_lab_row(self, p: dict[str, Any], verdict: Any, misalignment: Any,
                      occurrence: dict[str, int], semantics: str,
                      vocabulary: set[str], doc: ClinicalDocument,
                      lineage: Lineage, batch: Batch) -> None:
        row, resolution, test = p["row"], p["resolution"], p["test"]
        rng, result = p["range"], p["result"]
        name = str(row["test_name"] or "")

        # Rows that are not tests go to the dead-letter store with a precise
        # reason, so the review queue stays actionable.
        if resolution.method is Method.NON_TEST:
            reason = resolution.note or "non_test_row"
            if reason == "empty test name":
                reason = "empty_test_name"
            batch.errors.append(ProcessingError(
                id=dedup.row_id(doc.id, f"nontest:{name}", p["index"]),
                file_uri=lineage.file_uri, file_sha256=lineage.file_sha256,
                run_id=self.run_id, stage="standardise",
                reason=reason,
                detail=f"test_name={name!r} result={row['result']!r} unit={row['unit']!r}",
                occurred_at=_now(), document_id=lineage.document_id,
                trace_id=lineage.trace_id, source_row_index=p["index"],
                retryable=False,
            ))
            return

        # Values buried inside a name, recovered as their own rows.
        if result.value is None and not result.text and name:
            for label, value in extract_embedded_values(name):
                sub = self.names.resolve(label, row["unit"])
                if not sub.canonical:
                    continue
                sub_test = self.config.catalog.tests.get(sub.canonical)
                self._emit_recovered_row(label, value, sub, sub_test, doc, lineage,
                                         occurrence, p["index"], batch, name)

        flags: list[Flag] = [*result.flags, *rng.flags, *verdict.flags]
        if resolution.method is Method.UNMAPPED:
            flags.append(Flag.TEST_UNMAPPED)
        elif resolution.confidence < 0.86:
            flags.append(Flag.TEST_LOW_CONFIDENCE)
        if misalignment.suspected:
            flags.append(Flag.REPORT_MISALIGNMENT_SUSPECTED)

        harmonised = harmonise(self.config.catalog, result, rng,
                               str(row["unit"] or ""), test, name)
        flags.extend(harmonised.flags)

        source_interpretation = assay_method = None
        raw_interpretation = row.get("source_interpretation")
        if raw_interpretation:
            text = str(raw_interpretation).strip()
            if semantics == "interpretation":
                source_interpretation = text
            elif semantics == "method":
                assay_method = text
            elif semantics == "mixed":
                # Same field, two meanings, decided per row by vocabulary.
                if text.casefold() in vocabulary:
                    source_interpretation = text
                else:
                    assay_method = text

        adjusted = parse_result(row["result"], test=test, range_original=row["range"],
                                garbage_tokens=self.garbage_tokens)
        adjusted.value = harmonised.value
        adjusted.text = result.text
        classification = classify(
            adjusted,
            type(rng)(low=harmonised.range_low, high=harmonised.range_high,
                      text=rng.text, original=rng.original, operator=rng.operator),
            test, sex=doc.gender, existing_flags=tuple(flags),
            source_interpretation=source_interpretation,
        )

        key = resolution.canonical or f"__raw__{normalise_name(name)}"
        occurrence[key] = occurrence.get(key, 0) + 1
        index = occurrence[key] - 1

        lab_lineage = replace(
            lineage, source_row_index=p["index"],
            page_no=_as_int(row.get("page_no")), processed_at=_now(),
        )
        notes = [n for n in (verdict.note, harmonised.note, classification.note,
                             resolution.note) if n]
        record = LabResult(
            id=dedup.row_id(doc.id, resolution.canonical or name, index),
            document_pk=doc.id,
            lineage=lab_lineage,
            test_name_canonical=resolution.canonical,
            test_name_original=name,
            loinc_code=test.loinc if test else None,
            panel=test.panel if test else None,
            result_value=harmonised.value,
            result_text=result.text,
            result_original=result.original,
            unit_canonical=harmonised.unit_canonical,
            unit_original=str(row["unit"] or ""),
            range_low=harmonised.range_low, range_high=harmonised.range_high,
            range_text=rng.text, range_original=rng.original,
            test_analytics=classification.analytics,
            normalization_method=resolution.method,
            normalization_confidence=resolution.confidence,
            flags=_dedupe_flags(classification.flags),
            occurrence_index=index,
            assay_method=assay_method,
            source_interpretation=source_interpretation,
            coherence_note="; ".join(notes)[:1000],
            range_source=classification.range_source,
        )
        batch.lab_results.append(record)

        if resolution.method is Method.UNMAPPED and name.strip():
            batch.unmapped_names.setdefault(name, []).append(record)

        self._maybe_review(record, resolution, batch)

    def _emit_recovered_row(self, label: str, value: float, resolution: Any,
                            test: Any, doc: ClinicalDocument, lineage: Lineage,
                            occurrence: dict[str, int], source_index: int,
                            batch: Batch, parent_name: str) -> None:
        """Emit a value that was embedded in a test name.

        Kept as its own row, flagged, and never merged silently into the parent:
        the recovery is an inference and must be visible as one.
        """
        key = resolution.canonical
        occurrence[key] = occurrence.get(key, 0) + 1
        index = occurrence[key] - 1
        reference = test.range_for(doc.gender) if test else None
        low = reference.low if reference else None
        high = reference.high if reference else None
        analytics = Analytics.NOT_APPLICABLE
        if low is not None or high is not None:
            analytics = (Analytics.BELOW_RANGE if low is not None and value < low
                         else Analytics.ABOVE_RANGE if high is not None and value > high
                         else Analytics.WITHIN_RANGE)
        record = LabResult(
            id=dedup.row_id(doc.id, f"embedded:{key}", index),
            document_pk=doc.id,
            lineage=replace(lineage, source_row_index=source_index, processed_at=_now()),
            test_name_canonical=key, test_name_original=f"{parent_name} -> {label}",
            loinc_code=test.loinc if test else None,
            panel=test.panel if test else None,
            result_value=value, result_text=None, result_original=parent_name,
            unit_canonical=test.canonical_unit if test else None, unit_original="",
            range_low=low, range_high=high, range_text=None, range_original="",
            test_analytics=analytics,
            normalization_method=resolution.method,
            normalization_confidence=round(resolution.confidence * 0.9, 4),
            flags=[Flag.VALUE_EMBEDDED_IN_NAME, Flag.UNIT_MISSING],
            occurrence_index=index,
            coherence_note=("recovered from a test name that contained its own "
                            "value; classified against the catalogue range"),
            range_source="catalogue",
        )
        batch.lab_results.append(record)
        batch.reviews.append(self._review(
            kind="recovered_embedded_value", priority=3,
            document_id=lineage.document_id, source_system=lineage.source_system,
            subject=f"{label} = {value}", row_id=record.id,
            evidence=f"extracted from test name {parent_name!r}",
            proposal=f"confirm the value belongs to {key}",
        ))

    def _maybe_review(self, record: LabResult, resolution: Any, batch: Batch) -> None:
        if record.test_name_canonical is None and record.test_name_original.strip():
            batch.reviews.append(self._review(
                kind="unmapped_test", priority=2,
                document_id=record.lineage.document_id,
                source_system=record.lineage.source_system,
                subject=record.test_name_original, row_id=record.id,
                evidence=(f"{resolution.note or 'no candidate matched'}; "
                          f"result={record.result_original!r} "
                          f"unit={record.unit_original!r}"),
                proposal=(", ".join(f"{c} ({s})" for c, s in resolution.candidates[:3])
                          or None),
            ))
        elif Flag.TEST_LOW_CONFIDENCE in record.flags:
            batch.reviews.append(self._review(
                kind="low_confidence_match", priority=3,
                document_id=record.lineage.document_id,
                source_system=record.lineage.source_system,
                subject=f"{record.test_name_original} -> {record.test_name_canonical}",
                row_id=record.id,
                evidence=f"confidence {record.normalization_confidence} "
                         f"via {record.normalization_method.value}",
                proposal="accept as a learned alias, or correct it",
            ))
        if record.test_analytics is Analytics.OUTLIER:
            batch.reviews.append(self._review(
                kind="outlier", priority=2,
                document_id=record.lineage.document_id,
                source_system=record.lineage.source_system,
                subject=f"{record.test_name_canonical} = {record.result_value} "
                        f"{record.unit_canonical or ''}".strip(),
                row_id=record.id, evidence=record.coherence_note,
                proposal="confirm whether this is a data error or a real critical value",
            ))

    # -- medications -----------------------------------------------------
    def _process_medications(self, raw_meds: list[Any], spec: dict[str, Any],
                             doc: ClinicalDocument, lineage: Lineage,
                             batch: Batch) -> None:
        from .ingest.envelope import first_present

        med_map = spec.get("medication_map") or {}
        seen: dict[str, int] = {}
        for index, raw in enumerate(raw_meds):
            if not isinstance(raw, dict):
                continue
            original = first_present(raw, med_map.get("medicine"))
            if not original:
                continue
            resolved = self.medicines.resolve(original)
            if resolved.skip_reason in ("not_a_medicine", "empty", "no_brand_token"):
                continue
            key = str(original).casefold()
            seen[key] = seen.get(key, 0) + 1
            occurrence = seen[key] - 1
            record = Medication(
                id=dedup.medication_id(doc.id, str(original), occurrence),
                lineage=replace(lineage, source_row_index=index, processed_at=_now()),
                document_id_fk=doc.id,
                medicine_original=str(original),
                medicine_generic=resolved.generic,
                brand_matched=resolved.brand,
                atc_code=resolved.atc_code,
                dosage_form=resolved.dosage_form,
                strength=resolved.strength,
                dose=_as_text(first_present(raw, med_map.get("dose"))),
                frequency=_as_text(first_present(raw, med_map.get("frequency"))),
                medicine_type=_as_text(first_present(raw, med_map.get("medicine_type"))),
                is_combination=resolved.is_combination,
                confidence=resolved.confidence,
                occurrence_index=occurrence,
                flags=list(resolved.flags),
            )
            batch.medications.append(record)

            if resolved.generic is None:
                batch.reviews.append(self._review(
                    kind="unmapped_brand", priority=4,
                    document_id=lineage.document_id,
                    source_system=lineage.source_system,
                    subject=str(original), evidence=resolved.skip_reason or "no match",
                    proposal="add to config/medicines.yaml or confirm it is a consumable",
                ))
            elif Flag.BRAND_UNVERIFIED in resolved.flags:
                batch.reviews.append(self._review(
                    kind="unverified_brand", priority=5,
                    document_id=lineage.document_id,
                    source_system=lineage.source_system,
                    subject=f"{original} -> {resolved.generic}",
                    evidence="mapping marked verify:true in the reference table",
                    proposal="pharmacist sign-off before clinical analytics use",
                ))

    # -- helpers ---------------------------------------------------------
    def _error(self, ref: FileRef, stage: str, reason: str, detail: str, *,
               document_id: str | None = None, row_index: int | None = None,
               excerpt: str | None = None) -> ProcessingError:
        return ProcessingError(
            id=dedup.row_id(ref.sha256 or ref.uri, f"{stage}:{reason}", row_index or 0),
            file_uri=ref.uri, file_sha256=ref.sha256, run_id=self.run_id,
            stage=stage, reason=reason, detail=detail[:2000],
            occurred_at=_now(), document_id=document_id,
            source_row_index=row_index, payload_excerpt=excerpt,
            retryable=reason not in ("malformed_json", "invalid_encoding",
                                     "empty_file", "non_test_row"),
        )

    def _ledger(self, ref: FileRef, ingested_at: str, status: str, *,
                documents: int = 0, rows: int = 0, errors: int = 0) -> FileLedgerEntry:
        return FileLedgerEntry(
            file_sha256=ref.sha256 or f"unhashed:{ref.uri}", file_uri=ref.uri,
            size_bytes=ref.size_bytes, first_seen_at=ingested_at,
            last_run_id=self.run_id, status=status,
            documents_emitted=documents, rows_emitted=rows, errors_emitted=errors,
        )

    def _review(self, *, kind: str, priority: int, document_id: str | None,
                source_system: str | None, subject: str, evidence: str,
                proposal: str | None = None, row_id: str | None = None) -> ReviewItem:
        # Identity is the *finding*, not the run that noticed it. Keying on
        # run_id would refill the queue with duplicates on every rerun, and an
        # analyst who already dismissed an item would see it return. `run_id`
        # is still carried as a column: last run that observed this finding.
        return ReviewItem(
            id=dedup.row_id(f"{kind}|{document_id or ''}|{row_id or ''}", subject, priority),
            run_id=self.run_id, kind=kind, priority=priority,
            document_id=document_id, row_id=row_id, source_system=source_system,
            subject=subject[:500], evidence=evidence[:2000],
            proposal=(proposal or None), created_at=_now(),
        )


def _dedupe_flags(flags: Iterable[Flag]) -> list[Flag]:
    out: list[Flag] = []
    for flag in flags:
        if flag not in out:
            out.append(flag)
    return out


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value if v) or None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _blank_if_null(value: Any, null_tokens: set[str]) -> Any:
    """Map a source's literal placeholders to empty.

    ARTEMIS writes ``"DD/MM/YYYY"`` and ``"unit"`` instead of leaving a field
    blank. Those are absences wearing a costume, and treating them as values
    is how a placeholder ends up in a report.
    """
    if value is None:
        return value
    if isinstance(value, str) and value.strip().casefold() in null_tokens:
        return ""
    return value


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

_WORKER: dict[str, Any] = {}


def _worker_init(config_dir: str | None, run_id: str,
                 learned: dict[str, str]) -> None:
    """Build one Standardiser per worker process.

    Config is loaded inside the worker rather than pickled across: compiled
    regexes and the trigram index are cheaper to rebuild than to serialise, and
    it keeps the parent's object graph out of the fork.
    """
    from .observe.logging import configure_from
    config = load_config(config_dir)
    configure_from(config.pipeline)
    _WORKER["standardiser"] = Standardiser(config, run_id, learned)


def _worker_run(refs: list[FileRef]) -> Batch:
    standardiser: Standardiser = _WORKER["standardiser"]
    batch = Batch()
    for ref in refs:
        batch.extend(standardiser.process_file(ref))
    return batch


class Pipeline:
    """End-to-end run: discover, standardise, validate, load."""

    def __init__(self, config: Config, sink: SqliteSink, *,
                 run_id: str | None = None, config_dir: str | None = None) -> None:
        self.config = config
        self.sink = sink
        self.run_id = run_id or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.config_dir = config_dir
        self.summary = RunSummary(
            run_id=self.run_id, started_at=_now(),
            config_fingerprint=config.fingerprint,
        )

    # -- public ----------------------------------------------------------
    def run(self, input_uri: str, *, workers: int | None = None,
            limit: int | None = None, skip_seen_files: bool = False) -> RunSummary:
        runtime = self.config.section("runtime")
        batch_size = int(runtime.get("batch_size", 500))
        if workers is None:
            workers = int(runtime.get("workers", 0)) or _cpu_count()
        workers = max(1, workers)

        self.sink.initialise(self.config.catalog.tests.keys())
        learned = self.sink.load_learned_aliases()
        known_hashes = self.sink.known_content_hashes()
        already_seen = self.sink.seen_file_hashes() if skip_seen_files else set()

        source = build_source(input_uri, int(runtime.get("max_file_bytes", 20 * 1024 * 1024)))
        tail = llm_module.build_tail_resolver(
            self.config.catalog, self.config.section("llm"), learned)

        started = time.perf_counter()
        serial = Standardiser(self.config, self.run_id, learned)
        serial.known_hashes = dict(known_hashes)

        executor = None
        if workers > 1:
            executor = ProcessPoolExecutor(
                max_workers=workers, initializer=_worker_init,
                initargs=(self.config_dir, self.run_id, learned),
            )

        try:
            for chunk in _chunked(self._discover(source, limit, already_seen), batch_size):
                if executor is None:
                    batch = Batch()
                    for ref in chunk:
                        batch.extend(serial.process_file(ref))
                else:
                    slice_size = max(1, len(chunk) // workers)
                    futures = [
                        executor.submit(_worker_run, chunk[i:i + slice_size])
                        for i in range(0, len(chunk), slice_size)
                    ]
                    batch = Batch()
                    for future in futures:
                        batch.extend(future.result())
                    # Duplicate detection must be centralised: workers cannot
                    # see each other's registries, so the parent resolves
                    # cross-worker duplicates before writing.
                    self._resolve_cross_worker_duplicates(batch, serial.known_hashes)

                if tail is not None and batch.unmapped_names:
                    self._resolve_tail(tail, batch)

                self._write(batch)
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        if tail is not None:
            self._persist_learned(tail)
            self.summary.llm_calls = tail.usage.calls
            self.summary.llm_cache_hits = tail.usage.cache_hits

        self.summary.duration_seconds = round(time.perf_counter() - started, 3)
        self.summary.finished_at = _now()
        self.summary.worker_count = workers
        self.sink.write_run(
            self.summary,
            resolver_stats=serial.names.stats.as_dict(),
            llm_usage=(tail.usage.as_dict() if tail else {}),
        )
        log.info("run complete", extra=self.summary.as_dict())
        return self.summary

    # -- internals -------------------------------------------------------
    def _discover(self, source: Any, limit: int | None,
                  already_seen: set[str]) -> Iterator[FileRef]:
        for index, ref in enumerate(source.discover()):
            if limit is not None and index >= limit:
                break
            self.summary.files_discovered += 1
            if ref.sha256 and ref.sha256 in already_seen:
                # Content-hash skip, not filename skip: the same bytes under a
                # new name are the same work (NFR-3.2).
                self.summary.files_skipped_duplicate += 1
                continue
            yield ref

    def _resolve_cross_worker_duplicates(self, batch: Batch,
                                         known: dict[str, str]) -> None:
        for doc in batch.documents:
            if doc.is_duplicate:
                continue
            prior = known.get(doc.content_hash)
            if prior is not None and prior != doc.id:
                doc.is_duplicate = True
                doc.duplicate_of = prior
                if Flag.DUPLICATE_SUPPRESSED not in doc.flags:
                    doc.flags.append(Flag.DUPLICATE_SUPPRESSED)
            else:
                known[doc.content_hash] = doc.id
        suppressed = {d.id for d in batch.documents if d.is_duplicate}
        if suppressed:
            batch.lab_results = [
                r for r in batch.lab_results if r.document_pk not in suppressed
            ]
            batch.medications = [
                m for m in batch.medications if m.document_id_fk not in suppressed
            ]

    def _resolve_tail(self, tail: Any, batch: Batch) -> None:
        """One batched call for every unmapped name in the chunk.

        Rows are patched in place before they are written, so a name the tail
        resolves lands in the warehouse on this run rather than the next.
        """
        decisions = tail.resolve_many(list(batch.unmapped_names))
        for name, decision in decisions.items():
            if not decision.canonical or decision.confidence < tail.min_confidence:
                continue
            test = self.config.catalog.tests.get(decision.canonical)
            for record in batch.unmapped_names.get(name, []):
                record.test_name_canonical = decision.canonical
                record.loinc_code = test.loinc if test else None
                record.panel = test.panel if test else None
                record.normalization_method = (
                    Method.LEARNED_ALIAS if decision.provider == "cache" else Method.LLM
                )
                record.normalization_confidence = decision.confidence
                if Flag.TEST_UNMAPPED in record.flags:
                    record.flags.remove(Flag.TEST_UNMAPPED)
                record.coherence_note = (
                    f"{record.coherence_note}; resolved by {decision.provider}: "
                    f"{decision.rationale}"
                ).strip("; ")
                # Resolved late, so re-apply the two checks that depend on the
                # test's identity. A tail-resolved name gets no less scrutiny
                # than a dictionary hit.
                if test is not None:
                    rng = type(parse_range(""))(
                        low=record.range_low, high=record.range_high,
                        text=record.range_text, original=record.range_original)
                    verdict = self.coherence_check(record, test, rng)
                    if verdict:
                        record.flags.extend(verdict)
                        record.test_analytics = Analytics.INVALID
            batch.reviews.append(ReviewItem(
                id=dedup.row_id("tail_resolution", name, 0),
                run_id=self.run_id, kind="tail_resolution", priority=3,
                document_id=None, source_system=None,
                subject=f"{name} -> {decision.canonical}",
                evidence=f"{decision.provider}: {decision.rationale} "
                         f"(confidence {decision.confidence})",
                proposal="confirm to promote into config/test_catalog.yaml",
                created_at=_now(),
            ))

    def coherence_check(self, record: LabResult, test: Any, rng: Any) -> list[Flag]:
        engine = CoherenceEngine(self.config.catalog, self.config.section("coherence"))
        verdict = engine.check_row(0, test, record.unit_original, rng)
        return list(verdict.flags)

    def _persist_learned(self, tail: Any) -> None:
        rows = []
        for decision in tail.decisions:
            if not decision.canonical or decision.confidence < tail.min_confidence:
                continue
            rows.append({
                "source_name_normalised": normalise_name(decision.source_name),
                "canonical": decision.canonical,
                "source_name_example": decision.source_name,
                "provider": decision.provider,
                "confidence": decision.confidence,
                "rationale": decision.rationale,
                "created_at": _now(),
                "run_id": self.run_id,
                "times_used": 1,
            })
        if rows:
            self.sink.write_learned_aliases(rows)
            log.info("promoted learned aliases", extra={"count": len(rows)})

    def _write(self, batch: Batch) -> None:
        self.sink.write_documents(batch.documents)
        self.sink.write_lab_results(batch.lab_results)
        self.sink.write_medications(batch.medications)
        self.sink.write_errors(batch.errors)
        self.sink.write_review_items(batch.reviews)
        self.sink.write_file_ledger(batch.ledger)
        self.sink.commit()

        s = self.summary
        s.files_processed += sum(1 for e in batch.ledger if e.status == "processed")
        s.files_failed += sum(1 for e in batch.ledger if e.status == "failed")
        s.documents_emitted += sum(1 for d in batch.documents if not d.is_duplicate)
        s.documents_duplicate += sum(1 for d in batch.documents if d.is_duplicate)
        s.rows_emitted += len(batch.lab_results)
        s.rows_flagged += sum(1 for r in batch.lab_results if r.flags)
        s.rows_invalid += sum(1 for r in batch.lab_results
                              if r.test_analytics is Analytics.INVALID)
        s.rows_outlier += sum(1 for r in batch.lab_results
                              if r.test_analytics is Analytics.OUTLIER)
        s.rows_out_of_range += sum(
            1 for r in batch.lab_results
            if r.test_analytics in (Analytics.ABOVE_RANGE, Analytics.BELOW_RANGE))
        s.medications_emitted += len(batch.medications)
        s.errors_emitted += len(batch.errors)
        s.review_items_emitted += len(batch.reviews)
        s.reports_misaligned += batch.misaligned_reports


def _chunked(items: Iterable[Any], size: int) -> Iterator[list[Any]]:
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _cpu_count() -> int:
    import os
    return os.cpu_count() or 1
