"""Result classification (FR-3.1, FR-3.2, FR-3.3, FR-3.4).

Three distinct questions, deliberately not collapsed into one:

    Is this value *readable*?        -> Invalid
    Is this value *possible*?        -> Outlier
    Is this value *normal*?          -> Within / Above / Below Range

Conflating the second and third is the expensive mistake. A haemoglobin of
0.1 g/dL is not "below range" - a patient with that value is dead, so it is
bad data, and routing it to the clinical review queue as an abnormal result
wastes a reviewer's time while hiding a broken feed. Equally, a genuinely
critical haemoglobin of 4.1 must not be dismissed as an outlier. The
separation is what makes both queues actionable.

Why we recompute instead of trusting the source
-----------------------------------------------
Every lab report in the corpus ships its own ``test_analytics`` column. On the
rows where that column holds an interpretation and the arithmetic is checkable,
**24 of 59 disagree with the arithmetic** - 41%. Examples:

    HAEMATOCRIT       82    range 83-101   source says "normal"   -> Below Range
    RDW-CV            14.2  range 11.6-14  source says "normal"   -> Above Range
    ALKALINE PHOS.    124   range 38-126   source says "high"     -> Within Range

So the source's verdict is recorded as evidence (``source_interpretation``)
and never used to classify. Where it disagrees with ours the row is flagged
``source_interpretation_disagrees``, which turns a silent data-quality problem
into a per-clinic metric someone can act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CanonicalTest
from ..models import Analytics, Flag, ParsedRange, ParsedResult
from ..standardise.numeric import round_like

# Flags that mean "there is no trustworthy measurement in this row". Any one of
# them forces Invalid, whatever else the row contains.
_HARD_INVALID: frozenset[Flag] = frozenset({
    Flag.RESULT_EQUALS_RANGE,
    Flag.OCR_GARBAGE_TOKEN,
    Flag.UNIT_EQUALS_TEST_NAME,
    Flag.UNIT_DIMENSION_MISMATCH,
    Flag.RANGE_IMPLAUSIBLE_FOR_TEST,
    Flag.MULTI_VALUE_PACKED,
    Flag.HEADER_ROW_LEAKED,
})

_INTERPRETATION_TO_ANALYTICS: dict[str, Analytics] = {
    "normal": Analytics.WITHIN_RANGE,
    "low": Analytics.BELOW_RANGE,
    "high": Analytics.ABOVE_RANGE,
}


@dataclass(slots=True)
class Classification:
    analytics: Analytics
    flags: list[Flag] = field(default_factory=list)
    range_source: str = "none"        # source | catalogue | none
    range_low: float | None = None
    range_high: float | None = None
    note: str = ""


def _within(value: float, low: float | None, high: float | None) -> Analytics:
    if low is not None and value < low:
        return Analytics.BELOW_RANGE
    if high is not None and value > high:
        return Analytics.ABOVE_RANGE
    return Analytics.WITHIN_RANGE


def is_outlier(value: float, test: CanonicalTest | None) -> bool:
    """Whether a value is physiologically impossible for this test.

    Uses the catalogue's hard envelope rather than a statistical rule, because
    a per-batch statistical rule is unstable at the boundary: the same value is
    an outlier on a quiet Tuesday and normal on a busy Friday, which makes the
    flag impossible to explain to a clinician. A declared physiological bound
    is stable, reviewable in config, and defensible in a claims dispute.
    """
    if test is None:
        return False
    low, high = test.envelope()
    if low is not None and value < low:
        return True
    return high is not None and value > high


def classify(
    result: ParsedResult,
    rng: ParsedRange,
    test: CanonicalTest | None,
    *,
    sex: str | None = None,
    existing_flags: tuple[Flag, ...] | list[Flag] = (),
    source_interpretation: str | None = None,
    fallback_to_catalogue_range: bool = True,
) -> Classification:
    """Classify one result. Abstains rather than guessing."""
    flags = list(existing_flags)
    out = Classification(Analytics.INVALID, flags)

    if any(flag in _HARD_INVALID for flag in flags):
        out.note = "unreadable row: " + ", ".join(
            f.value for f in flags if f in _HARD_INVALID
        )
        return out

    # Qualitative results are legitimate findings, not failures. POSITIVE for a
    # Widal test is the answer; it simply has no numeric band to sit in.
    if result.value is None:
        if result.text and test is not None and test.value_type in ("qualitative", "narrative"):
            out.analytics = Analytics.NOT_APPLICABLE
            out.note = "qualitative result"
            return out
        if result.text:
            out.analytics = Analytics.INVALID
            out.note = "non-numeric result where a number was expected"
            return out
        out.analytics = Analytics.INVALID
        out.note = "no result value"
        return out

    value = result.value

    # Impossibility is checked before abnormality, because an impossible value
    # tells you about the feed and an abnormal one tells you about the patient.
    if is_outlier(value, test):
        low, high = test.envelope() if test else (None, None)
        out.analytics = Analytics.OUTLIER
        out.flags.append(Flag.OUTLIER_IMPLAUSIBLE)
        out.note = f"outside physiological envelope [{low}, {high}]"
        return out

    # Prefer the lab's own reference range: it reflects their assay and
    # population. Fall back to the catalogue so a missing range costs coverage
    # rather than correctness - but say which was used, every time.
    if rng.low is not None or rng.high is not None:
        out.range_low, out.range_high, out.range_source = rng.low, rng.high, "source"
    elif fallback_to_catalogue_range and test is not None:
        reference = test.range_for(sex)
        if reference and (reference.low is not None or reference.high is not None):
            out.range_low, out.range_high, out.range_source = (
                reference.low, reference.high, "catalogue",
            )
            out.note = (
                f"classified against catalogue reference range "
                f"(sex={sex or 'unknown'})"
            )

    if out.range_low is None and out.range_high is None:
        out.analytics = Analytics.NOT_APPLICABLE
        out.note = "no reference range available from source or catalogue"
        return out

    out.analytics = _within(value, out.range_low, out.range_high)

    # Cross-check against the lab's own verdict. Disagreement is recorded, not
    # acted on: it is a measurement of the source's quality.
    if source_interpretation:
        claimed = _INTERPRETATION_TO_ANALYTICS.get(source_interpretation.strip().casefold())
        if claimed is not None and claimed != out.analytics:
            out.flags.append(Flag.SOURCE_INTERPRETATION_DISAGREES)
            out.note = (
                f"{out.note + '; ' if out.note else ''}"
                f"source said {source_interpretation!r}, arithmetic says "
                f"{out.analytics.value!r}"
            ).strip()

    return out


def resolve_unit(
    catalog, unit_original: str, test: CanonicalTest | None, test_name_original: str
) -> tuple[str | None, list[Flag], str]:
    """Normalise a unit and judge whether it belongs to this test.

    Returns ``(canonical_unit, flags, note)``. The dimension check here is the
    first half of the coherence engine; it is the single most valuable
    validation in the pipeline, because it is what stops a WBC count being
    loaded as a haemoglobin.
    """
    flags: list[Flag] = []
    raw = (unit_original or "").strip()

    if not raw:
        flags.append(Flag.UNIT_MISSING)
        return (test.canonical_unit if test else None), flags, "unit absent in source"

    # The test name leaking into the unit column means the columns collapsed;
    # nothing in this row can be trusted.
    if test_name_original and raw.strip().casefold() == test_name_original.strip().casefold():
        flags.append(Flag.UNIT_EQUALS_TEST_NAME)
        return None, flags, "unit column contains the test name"

    if catalog.is_non_unit_token(raw):
        flags.append(Flag.UNIT_NOT_A_UNIT)
        return None, flags, f"{raw!r} is a known non-unit token"

    spec = catalog.unit_spec(raw)
    if spec is None:
        flags.append(Flag.UNIT_UNRECOGNISED)
        return None, flags, f"unit {raw!r} is not in the unit catalogue"

    if test is None:
        return spec.spelling, flags, "no canonical test; unit recorded as given"

    if spec.dimension != test.dimension:
        flags.append(Flag.UNIT_DIMENSION_MISMATCH)
        return None, flags, (
            f"unit {raw!r} measures {spec.dimension}, but {test.canonical} is "
            f"measured in {test.dimension}"
        )

    return test.canonical_unit, flags, ""

# ---------------------------------------------------------------------------
# Unit harmonisation, which must happen *before* classification
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Harmonised:
    value: float | None
    range_low: float | None
    range_high: float | None
    unit_canonical: str | None
    flags: list[Flag] = field(default_factory=list)
    note: str = ""


def harmonise(
    catalog,
    result: ParsedResult,
    rng: ParsedRange,
    unit_original: str,
    test: CanonicalTest | None,
    test_name_original: str,
) -> Harmonised:
    """Convert value and reference range into the test's canonical unit.

    Ordering matters and is easy to get wrong: a body temperature of
    ``98.6 degree F`` compared against a canonical envelope of 25-45 degC
    looks like a wild outlier. Convert first, judge second.

    The reference range is converted with the *same* factor as the value,
    because a lab quotes both in its own unit - ``4.50-5.50 mil/cu.mm``
    alongside ``5.45``. Converting one without the other would classify every
    row in the report wrongly, and the error would be invisible because both
    numbers look reasonable on their own.

    When the unit is absent the value is taken to be already canonical. That
    is an assumption, not a fact, so the row carries ``unit_missing`` and the
    assumption is stated in docs/assumptions.md D-3.
    """
    canonical_unit, flags, note = resolve_unit(
        catalog, unit_original, test, test_name_original
    )
    out = Harmonised(result.value, rng.low, rng.high, canonical_unit, flags, note)

    # An inline unit inside the result cell ("13.7 g/dl") is authoritative when
    # the unit column is empty: the lab wrote it next to the number.
    effective_unit = unit_original
    if (not str(unit_original or "").strip()) and result.inline_unit \
            and catalog.unit_spec(result.inline_unit) is not None:
            effective_unit = result.inline_unit
            out.flags = [f for f in out.flags if f is not Flag.UNIT_MISSING]
            recheck_unit, recheck_flags, recheck_note = resolve_unit(
                catalog, effective_unit, test, test_name_original
            )
            out.unit_canonical, out.flags, out.note = (
                recheck_unit, out.flags + recheck_flags, recheck_note or out.note
            )
            out.note = (out.note + "; " if out.note else "") + "unit taken from the result cell"

    if test is None or result.value is None:
        return out
    if (Flag.UNIT_DIMENSION_MISMATCH in out.flags
            or Flag.UNIT_EQUALS_TEST_NAME in out.flags):
        return out                      # nothing to convert; the row is invalid
    if not str(effective_unit or "").strip():
        return out                      # assumed canonical already

    converted, status = catalog.convert(result.value, effective_unit, test)
    if status in ("converted", "identity") and converted is not None:
        if status == "converted" and converted != result.value:
            out.flags.append(Flag.UNIT_CONVERTED)
            out.note = (out.note + "; " if out.note else "") + (
                f"{result.value} {effective_unit} -> {converted} {test.canonical_unit}"
            )
        out.value = round_like(converted, result.original)
        for attr in ("range_low", "range_high"):
            bound = getattr(out, attr)
            if bound is None:
                continue
            bound_converted, bound_status = catalog.convert(bound, effective_unit, test)
            if bound_status in ("converted", "identity") and bound_converted is not None:
                setattr(out, attr, round_like(bound_converted, rng.original))

    return out
