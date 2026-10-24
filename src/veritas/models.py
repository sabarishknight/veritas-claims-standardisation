"""Canonical data model and the vocabulary of findings.

DESIGN NOTE - why a narrow fact table
-------------------------------------
The assignment's target schema (``docs/ideal-output-schema.csv``, 79 columns)
is a wide union of every field any source has ever sent, which is what you get
when a landing table grows by accretion. It is a fine *landing* shape and a
poor *analytics* shape: adding a test adds columns, a report with 193 rows
cannot be represented without repeating 40 patient columns 193 times, and
``age`` / ``age_text`` / ``age_years`` / ``basic_info_age`` all mean the same
thing.

So the pipeline writes a narrow model - one row per test observation, one row
per document, one row per medication - and then *generates* the 79-column
shape as a SQL view over it (``load/schema.py``). The required output is
delivered exactly as specified; the storage underneath is one that can take
500 more clinics. FR-2.2's five-columns-per-test presentation is a second view
over the same fact table.

Trade-off accepted: an analyst who wants one row per patient must join or use
the view. That is a cost paid once in SQL, not every time a clinic adds a test.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Finding vocabulary
#
# Reason codes are a closed set, not free text, because they are consumed by
# three things that cannot read prose: the ops review queue, the per-source
# quality SLOs, and the alerting rules. A new code is a deliberate addition.
#
# These subclass `(str, Enum)` rather than `enum.StrEnum`, which linters prefer
# on 3.11+. StrEnum would raise the minimum Python to 3.11 for a purely
# cosmetic gain, and the reviewer's interpreter is not ours to choose - this
# was caught by running the suite on 3.10. The behaviour is identical for
# everything the pipeline does with them.
# --------------------------------------------------------------------------

class Analytics(str, enum.Enum):  # noqa: UP042 - see note above
    """FR-3.3 classification of a single result."""

    WITHIN_RANGE = "Within Range"
    ABOVE_RANGE = "Above Range"
    BELOW_RANGE = "Below Range"
    OUTLIER = "Outlier"
    INVALID = "Invalid"
    NOT_APPLICABLE = "Not Applicable"   # qualitative or narrative result


class Flag(str, enum.Enum):  # noqa: UP042
    """Why a row is not clean. A row may carry several."""

    # --- structural: the row is not a test result at all
    HEADER_ROW_LEAKED = "header_row_leaked"
    NON_TEST_ROW = "non_test_row"
    MULTI_VALUE_PACKED = "multi_value_packed"
    VALUE_EMBEDDED_IN_NAME = "value_embedded_in_name"

    # --- name resolution
    TEST_UNMAPPED = "test_unmapped"
    TEST_LOW_CONFIDENCE = "test_low_confidence"

    # --- result parsing
    RESULT_MISSING = "result_missing"
    RESULT_NON_NUMERIC = "result_non_numeric"
    RESULT_EQUALS_RANGE = "result_equals_range"
    RESULT_PREFIX_MARKER = "result_prefix_marker"
    OCR_GARBAGE_TOKEN = "ocr_garbage_token"
    COMPOUND_SPLIT = "compound_split"

    # --- units
    UNIT_MISSING = "unit_missing"
    UNIT_UNRECOGNISED = "unit_unrecognised"
    UNIT_NOT_A_UNIT = "unit_not_a_unit"
    UNIT_EQUALS_TEST_NAME = "unit_equals_test_name"
    UNIT_DIMENSION_MISMATCH = "unit_dimension_mismatch"
    UNIT_CONVERTED = "unit_converted"

    # --- ranges
    RANGE_MISSING = "range_missing"
    RANGE_UNPARSEABLE = "range_unparseable"
    RANGE_IMPLAUSIBLE_FOR_TEST = "range_implausible_for_test"

    # --- clinical judgement
    OUTLIER_IMPLAUSIBLE = "outlier_implausible"
    SOURCE_INTERPRETATION_DISAGREES = "source_interpretation_disagrees"

    # --- report level
    REPORT_MISALIGNMENT_SUSPECTED = "report_misalignment_suspected"

    # --- demographics
    PLACEHOLDER_DATE = "placeholder_date"
    DATE_UNPARSEABLE = "date_unparseable"
    AGE_UNPARSEABLE = "age_unparseable"

    # --- lifecycle
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    BRAND_UNVERIFIED = "brand_unverified"


class Method(str, enum.Enum):  # noqa: UP042
    """How a test name was resolved. Written to ``normalization_method``."""

    EXACT = "exact"
    ALIAS = "alias"
    NORMALISED = "normalised"
    FUZZY = "fuzzy"
    FUZZY_AFFIX = "fuzzy_affix"
    LLM = "llm"
    LEARNED_ALIAS = "learned_alias"
    UNMAPPED = "unmapped"
    NON_TEST = "non_test"


class RecordType(str, enum.Enum):  # noqa: UP042
    LAB_RESULT = "lab_result"
    DISCHARGE_SUMMARY = "discharge_summary"
    MEDICATION = "medication"


# --------------------------------------------------------------------------
# Lineage - NFR-4.2
#
# Carried on every emitted row. A claims analyst looking at one number must be
# able to get back to the exact file, the exact source row and the ingest that
# produced it, without a developer.
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Lineage:
    file_uri: str                  # where the file landed (gs:// or file://)
    file_sha256: str               # content identity of the whole file
    document_id: str               # source's document identifier
    trace_id: str                  # source's trace id, carried through
    correlation_id: str            # source's correlation id, carried through
    source_system: str             # FASTTRACK / ARTEMIS / ...
    clinic_profile: str            # which config profile was applied
    schema_version: str            # which version of that profile
    ingested_at: str               # ISO 8601 UTC
    processed_at: str              # ISO 8601 UTC
    run_id: str                    # pipeline run that wrote the row
    source_row_index: int = -1     # position in the source array
    page_no: int | None = None     # page of the originating PDF, if known


@dataclass(slots=True)
class NameResolution:
    """Outcome of tiered test-name resolution."""

    canonical: str | None
    original: str
    method: Method
    confidence: float
    candidates: list[tuple[str, float]] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class ParsedResult:
    """Outcome of parsing the free-text ``result`` column."""

    value: float | None = None
    text: str | None = None
    original: str = ""
    inline_unit: str | None = None
    prefix_marker: str | None = None
    flags: list[Flag] = field(default_factory=list)
    components: list[tuple[str, float]] = field(default_factory=list)


@dataclass(slots=True)
class ParsedRange:
    low: float | None = None
    high: float | None = None
    text: str | None = None
    original: str = ""
    operator: str | None = None     # "<", ">", "between", None
    flags: list[Flag] = field(default_factory=list)


@dataclass(slots=True)
class LabResult:
    """One test observation: the grain of the fact table."""

    id: str                        # deterministic; the MERGE key (NFR-3.2)
    document_pk: str               # -> ClinicalDocument.id
    lineage: Lineage

    test_name_canonical: str | None
    test_name_original: str
    loinc_code: str | None
    panel: str | None

    result_value: float | None
    result_text: str | None
    result_original: str

    unit_canonical: str | None
    unit_original: str

    range_low: float | None
    range_high: float | None
    range_text: str | None
    range_original: str

    test_analytics: Analytics
    normalization_method: Method
    normalization_confidence: float

    flags: list[Flag] = field(default_factory=list)
    occurrence_index: int = 0       # nth time this test appears in the report
    assay_method: str | None = None
    source_interpretation: str | None = None
    coherence_note: str = ""
    # Which reference range drove the classification: the lab's own, the
    # catalogue's fallback, or none. Recorded because "Above Range" means
    # something different depending on the answer.
    range_source: str = "none"

    def flag_values(self) -> list[str]:
        return [f.value for f in self.flags]


@dataclass(slots=True)
class ClinicalDocument:
    """One document: a lab report or a discharge summary."""

    id: str
    lineage: Lineage
    record_type: RecordType

    claim_no: str | None = None
    nt_code: str | None = None
    consumer_client_id: str | None = None
    destination_identifier: str | None = None
    case_id: str | None = None

    patient_token: str | None = None
    patient_name: str | None = None
    age_years: float | None = None
    age_text: str | None = None
    gender: str | None = None
    uhid_token: str | None = None

    hospital_name: str | None = None
    hospital_address: str | None = None
    doctor_name: str | None = None
    ward: str | None = None

    admission_date: str | None = None
    discharge_date: str | None = None
    report_date: str | None = None
    bill_date: str | None = None

    diagnosis: str | None = None
    brief_history: str | None = None
    general_examinations: str | None = None
    recommendations: str | None = None
    post_discharge_advice: str | None = None
    course_during_hospitalisation: str | None = None

    content_hash: str = ""
    is_duplicate: bool = False
    duplicate_of: str | None = None
    flags: list[Flag] = field(default_factory=list)
    row_count: int = 0
    raw_payload: str | None = None   # FR-4.3 audit trail

    def flag_values(self) -> list[str]:
        return [f.value for f in self.flags]


@dataclass(slots=True)
class Medication:
    """One prescribed item from a discharge summary (FR-2.6)."""

    id: str
    lineage: Lineage
    document_id_fk: str
    medicine_original: str
    medicine_generic: str | None
    brand_matched: str | None
    atc_code: str | None
    dosage_form: str | None
    strength: str | None
    dose: str | None
    frequency: str | None
    medicine_type: str | None
    is_combination: bool = False
    confidence: float = 0.0
    occurrence_index: int = 0
    flags: list[Flag] = field(default_factory=list)

    def flag_values(self) -> list[str]:
        return [f.value for f in self.flags]


@dataclass(slots=True)
class ProcessingError:
    """A dead-letter entry (FR-4.2). Enough context to reprocess without the
    original operator."""

    id: str
    file_uri: str
    file_sha256: str
    run_id: str
    stage: str                     # read | parse | route | standardise | load
    reason: str                    # closed vocabulary where possible
    detail: str
    occurred_at: str
    document_id: str | None = None
    trace_id: str | None = None
    source_row_index: int | None = None
    payload_excerpt: str | None = None
    retryable: bool = True


@dataclass(slots=True)
class ReviewItem:
    """An item for the ops review queue (FR-5.3).

    Separate from ProcessingError on purpose: an error is something the
    pipeline could not do, a review item is something the pipeline *could* do
    but should not do unsupervised. Mixing them makes the queue unusable.
    """

    id: str
    run_id: str
    kind: str                      # unmapped_test | low_confidence | outlier |
                                   # misaligned_report | unverified_brand
    priority: int                  # 1 highest
    document_id: str | None
    source_system: str | None
    subject: str                   # the thing to decide about
    evidence: str                  # why we are asking
    proposal: str | None           # what we think the answer is
    created_at: str
    row_id: str | None = None
    status: str = "open"


@dataclass(slots=True)
class FileLedgerEntry:
    """Idempotency and lineage ledger (NFR-3.2, NFR-4.2).

    Keyed on content hash rather than filename: the same bytes re-landed under
    a new name are the same work, and a rerun must not double-count them.
    """

    file_sha256: str
    file_uri: str
    size_bytes: int
    first_seen_at: str
    last_run_id: str
    status: str                    # processed | failed | skipped_duplicate
    documents_emitted: int = 0
    rows_emitted: int = 0
    errors_emitted: int = 0


@dataclass(slots=True)
class RunSummary:
    """Per-run metrics (FR-5.1, NFR-5.1)."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    files_discovered: int = 0
    files_processed: int = 0
    files_skipped_duplicate: int = 0
    files_failed: int = 0
    documents_emitted: int = 0
    documents_duplicate: int = 0
    rows_emitted: int = 0
    rows_flagged: int = 0
    rows_invalid: int = 0
    rows_outlier: int = 0
    rows_out_of_range: int = 0
    medications_emitted: int = 0
    errors_emitted: int = 0
    review_items_emitted: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    reports_misaligned: int = 0
    duration_seconds: float = 0.0
    config_fingerprint: str = ""
    worker_count: int = 1

    @property
    def error_rate(self) -> float:
        total = self.files_processed + self.files_failed
        return (self.files_failed / total) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["error_rate"] = self.error_rate
        return d
