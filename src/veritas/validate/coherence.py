"""Semantic coherence: the safety net that makes name matching safe.

The problem this exists to solve
--------------------------------
``Sample_JSON_file2.json`` contains this row:

    test_name: "aemoglobin"   range: "4000-10000"   unit: "cell/cu.mm"   result: "9700"

A fuzzy matcher resolves ``aemoglobin`` to ``HAEMOGLOBIN`` with 0.97
confidence - and it is right about the name. But the value is not a
haemoglobin. Haemoglobin is a mass concentration around 13-17 g/dL; this row
carries a count concentration around 4000-10000 cells/cu.mm, which is a total
white-cell count. Reading the next rows makes the mechanism plain: the test
names are offset by one position from the values they are printed beside, and
every name has lost a character from its left edge, so the OCR dropped a row
and shifted the column.

    row  name (as received)      range         unit          what the values actually are
    0    aemoglobin              4000-10000    cell/cu.mm    a WBC count
    1    tal WBC Count           4.50-5.50     mil/cu.mm     an RBC count
    2    BC INDICES              36-46         %             a haematocrit
    3    ematocrit HCT           83-101        FL            an MCV

The same pattern appears in ``Sample_JSON_file5.json``, from a different source
system, so it is a systemic OCR failure mode rather than one bad file.

A pipeline that resolves the name and loads the value produces a warehouse in
which thousands of patients have a haemoglobin of 9700. Automated adjudication
then acts on it. This is the single most consequential defect in the corpus and
it is invisible to string matching, because the string matching is *correct*.

How it is caught
----------------
Every canonical test declares what its measurements must look like: a unit
dimension and a plausible value envelope. Two independent checks follow:

1.  **Dimension** - does the row's unit measure the right kind of quantity?
    ``cell/cu.mm`` is a count; haemoglobin is a mass concentration. Fails.
2.  **Range magnitude** - could this reference range belong to this analyte?
    ``4000-10000`` against an envelope of ``1-25`` is three orders out. Fails.

Either failure invalidates the row. Then, at report level, if enough rows fail
*and* those rows would cohere with the test named one position away, the
report is quarantined with a proposed offset.

What is deliberately NOT done
-----------------------------
The pipeline does not realign the rows, even though it can calculate the
offset with high confidence. Auto-correction here would write a
*plausible-looking wrong value* into a claim decision whenever the inference
was wrong, and the error would be undetectable downstream because the result
would look perfectly normal. So the offset is a proposal in the review queue
for a human to accept, and the default for ``coherence.misalignment.auto_correct``
is ``false``. Refusing to be clever is the correct engineering choice when the
cost of being wrong is a mis-adjudicated medical claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CanonicalTest, Catalog
from ..models import Flag, ParsedRange
from ..standardise.ranges import range_matches_envelope


@dataclass(slots=True)
class RowCoherence:
    """Verdict for one row."""

    index: int
    test_canonical: str | None
    evaluable: bool
    coherent: bool
    flags: list[Flag] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        return "; ".join(self.reasons)


@dataclass(slots=True)
class MisalignmentVerdict:
    """Report-level verdict."""

    suspected: bool = False
    offset: int | None = None
    support: int = 0
    base_support: int = 0
    failing_rows: int = 0
    evaluable_rows: int = 0
    total_rows: int = 0
    testable_rows: int = 0
    explained_fraction: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def failing_fraction(self) -> float:
        return self.failing_rows / self.evaluable_rows if self.evaluable_rows else 0.0

    def summary(self) -> str:
        if not self.suspected:
            return "no misalignment detected"
        direction = "earlier" if (self.offset or 0) < 0 else "later"
        return (
            f"{self.failing_rows} of {self.evaluable_rows} checkable rows are incoherent "
            f"with the test they name; {self.support} of the {self.testable_rows} rows where "
            f"the hypothesis is testable ({self.explained_fraction:.0%}) cohere instead with "
            f"the test named {abs(self.offset or 0)} row(s) {direction}. The value columns "
            f"appear shifted by {self.offset:+d}."
        )


class CoherenceEngine:
    """Row-level and report-level coherence checks."""

    def __init__(self, catalog: Catalog, coherence_cfg: dict | None = None) -> None:
        cfg = coherence_cfg or {}
        self.catalog = catalog
        self.enabled = bool(cfg.get("enabled", True))
        self.require_dimension_match = bool(cfg.get("require_dimension_match", True))
        self.range_tolerance = float(cfg.get("range_envelope_tolerance", 2.0))

        mis = cfg.get("misalignment") or {}
        self.misalignment_enabled = bool(mis.get("enabled", True))
        self.min_failing_rows = int(mis.get("min_failing_rows", 3))
        self.min_failing_fraction = float(mis.get("min_failing_fraction", 0.25))
        self.probe_offsets = [int(o) for o in (mis.get("probe_offsets") or [-2, -1, 1, 2])]
        self.min_offset_support = int(mis.get("min_offset_support", 2))
        self.min_offset_explains_fraction = float(
            mis.get("min_offset_explains_fraction", 0.5)
        )
        self.action = str(mis.get("action", "quarantine_report"))
        self.auto_correct = bool(mis.get("auto_correct", False))

    # -- row level -------------------------------------------------------
    def _unit_dimension(self, unit_original: str) -> str | None:
        spec = self.catalog.unit_spec(unit_original or "")
        return spec.dimension if spec else None

    def check_row(
        self,
        index: int,
        test: CanonicalTest | None,
        unit_original: str,
        rng: ParsedRange,
    ) -> RowCoherence:
        """Is this row's evidence consistent with the test it names?"""
        verdict = RowCoherence(index, test.canonical if test else None, False, True)
        if not self.enabled or test is None:
            return verdict

        dimension = self._unit_dimension(unit_original)
        has_range = rng.low is not None or rng.high is not None

        # A row with neither a recognised unit nor a parsed range offers no
        # evidence either way. Judging it would manufacture a finding.
        if dimension is None and not has_range:
            return verdict

        verdict.evaluable = True

        if dimension is not None and self.require_dimension_match and dimension != test.dimension:
            verdict.coherent = False
            verdict.flags.append(Flag.UNIT_DIMENSION_MISMATCH)
            verdict.reasons.append(
                f"unit {unit_original!r} measures {dimension}, but {test.canonical} "
                f"is measured in {test.dimension}"
            )

        if has_range and not range_matches_envelope(rng, test.envelope(), self.range_tolerance):
            low, high = test.envelope()
            verdict.coherent = False
            verdict.flags.append(Flag.RANGE_IMPLAUSIBLE_FOR_TEST)
            verdict.reasons.append(
                f"reference range {rng.text or rng.original!r} is implausible for "
                f"{test.canonical} (physiological envelope [{low}, {high}])"
            )

        return verdict

    def _coheres_with(
        self, test: CanonicalTest | None, unit_original: str, rng: ParsedRange
    ) -> bool:
        """Would this row's evidence be coherent if it belonged to ``test``?"""
        if test is None:
            return False
        dimension = self._unit_dimension(unit_original)
        has_range = rng.low is not None or rng.high is not None
        if dimension is None and not has_range:
            return False
        if dimension is not None and dimension != test.dimension:
            return False
        return not (
            has_range
            and not range_matches_envelope(rng, test.envelope(), self.range_tolerance)
        )

    # -- report level ----------------------------------------------------
    def detect_misalignment(
        self,
        tests: list[CanonicalTest | None],
        units: list[str],
        ranges: list[ParsedRange],
        row_verdicts: list[RowCoherence],
    ) -> MisalignmentVerdict:
        """Test the hypothesis that the value columns are shifted.

        The hypothesis is only scored on rows where it is *testable*: a failing
        row whose neighbour at the candidate offset has no resolved test offers
        no evidence either way, and counting it as a miss would bury a real
        shift under rows we simply cannot judge. So support is measured as

            rows explained by the offset / failing rows testable at that offset

        and a shift is reported only when it explains at least
        ``min_offset_explains_fraction`` of them, with at least
        ``min_offset_support`` rows in absolute terms. Both guards are needed:
        the fraction stops a couple of coincidences becoming a verdict, and the
        absolute floor stops "1 of 1 testable rows" reading as certainty.
        """
        total = len(tests)
        evaluable = [v for v in row_verdicts if v.evaluable]
        failing = [v for v in evaluable if not v.coherent]
        verdict = MisalignmentVerdict(
            failing_rows=len(failing),
            evaluable_rows=len(evaluable),
            total_rows=total,
            base_support=len(evaluable) - len(failing),
        )

        if not (self.enabled and self.misalignment_enabled):
            return verdict
        if len(failing) < self.min_failing_rows:
            return verdict
        if verdict.failing_fraction < self.min_failing_fraction:
            return verdict

        best_offset: int | None = None
        best_support = 0
        best_testable = 0
        for offset in self.probe_offsets:
            support = testable = 0
            for v in failing:
                i, j = v.index, v.index + offset
                if not (0 <= j < total) or tests[j] is None:
                    continue            # hypothesis not testable on this row
                testable += 1
                if self._coheres_with(tests[j], units[i], ranges[i]):
                    support += 1
            if support > best_support:
                best_support, best_testable, best_offset = support, testable, offset

        if best_offset is None or best_support < self.min_offset_support:
            return verdict
        explained = best_support / best_testable if best_testable else 0.0
        if explained < self.min_offset_explains_fraction:
            return verdict

        verdict.suspected = True
        verdict.offset = best_offset
        verdict.support = best_support
        verdict.testable_rows = best_testable
        verdict.explained_fraction = round(explained, 3)
        for v in failing[:6]:
            j = v.index + best_offset
            proposed = tests[j].canonical if 0 <= j < total and tests[j] else "?"
            verdict.evidence.append(
                f"row {v.index}: named {v.test_canonical!r}; "
                f"evidence fits {proposed!r} ({v.note})"
            )
        return verdict

    def misalignment_flags(self, verdict: MisalignmentVerdict) -> list[Flag]:
        if verdict.suspected and self.action == "quarantine_report":
            return [Flag.REPORT_MISALIGNMENT_SUSPECTED]
        return [Flag.REPORT_MISALIGNMENT_SUSPECTED] if verdict.suspected else []
