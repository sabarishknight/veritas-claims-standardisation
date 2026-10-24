"""Reference-range parsing (supports FR-3.1).

The ``range`` column in this corpus holds at least nine different grammars:

    4000-10000          plain interval
    8.0 - 23.0          interval with spaces
    01-04               interval with leading zeros
    1.005 TO 1.030      worded separator
    <50  / <= 1.0       upper bound only
    >90                 lower bound only
    0.0 -< 1.0          mixed interval and operator
    36.7 - 37.2ºF       interval with a unit glued on
    Less than 1:80      titre, prose
    = 1 COI - Equivocal assay-specific interpretation key
    NEGATIVE / ABSENT   qualitative expectation
    12-16, 35-45, 4.5-5.5, ...   a whole panel's ranges packed into one cell
    range               the PDF's header row, leaked into the data

Everything that parses numerically becomes ``range_low`` / ``range_high`` and
drives classification. Everything else is preserved verbatim in
``range_text``, flagged, and classification is withheld - a range we cannot
read is a reason to abstain, not to guess. ``range_text_original`` keeps the
raw cell either way, so the parse is always checkable.
"""

from __future__ import annotations

import re

from ..models import Flag, ParsedRange

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_INTERVAL_RE = re.compile(
    rf"^\(?\s*({_NUM})\s*(?:-|–|—|to|TO|To|~)\s*<?=?\s*({_NUM})\s*\)?\s*[^\d]*$"
)
_UPPER_RE = re.compile(rf"^(?:<|<=|less\s+than|upto|up\s+to|under)\s*=?\s*({_NUM})\s*\D*$",
                       re.IGNORECASE)
_LOWER_RE = re.compile(rf"^(?:>|>=|greater\s+than|above|over)\s*=?\s*({_NUM})\s*\D*$",
                       re.IGNORECASE)
_SINGLE_RE = re.compile(rf"^({_NUM})\s*\D{{0,12}}$")
_PACKED_RE = re.compile(rf"{_NUM}\s*(?:-|–)\s*{_NUM}")

_QUALITATIVE_TOKENS = frozenset({
    "negative", "positive", "absent", "present", "nil", "normal", "nad",
    "not detected", "detected", "non reactive", "reactive", "trace",
})


def _to_float(token: str) -> float | None:
    """Parse one numeric token.

    A comma may be a thousands separator (``4,290``) or a decimal comma
    (``4,29``). Indian lab software emits the former; the heuristic is that a
    comma followed by exactly three digits with something before it is a
    thousands separator. Ambiguous beyond that, so we prefer the dominant
    local convention and keep the original text for audit.
    """
    t = token.strip()
    if not t:
        return None
    if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+", t):
        t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_range(raw: object, *, qualitative_expected: bool = False) -> ParsedRange:
    """Parse a reference-range cell."""
    original = "" if raw is None else str(raw)
    text = original.strip()
    out = ParsedRange(original=original)

    if not text:
        out.flags.append(Flag.RANGE_MISSING)
        return out

    # A whole panel's ranges in one cell: two or more intervals separated by
    # commas. Parsing the first would silently classify every test in the panel
    # against the first test's range.
    if len(_PACKED_RE.findall(text)) > 1:
        out.text = text
        out.flags.append(Flag.RANGE_UNPARSEABLE)
        return out

    lowered = text.casefold()
    if lowered in _QUALITATIVE_TOKENS:
        out.text = text
        if not qualitative_expected:
            out.flags.append(Flag.RANGE_UNPARSEABLE)
        return out

    match = _INTERVAL_RE.match(text)
    if match:
        low, high = _to_float(match.group(1)), _to_float(match.group(2))
        if low is not None and high is not None:
            # Some labs write the bounds the wrong way round; an inverted
            # interval would classify every value as out of range.
            if low > high:
                low, high = high, low
            out.low, out.high, out.operator = low, high, "between"
            out.text = text
            return out

    match = _UPPER_RE.match(text)
    if match:
        out.high = _to_float(match.group(1))
        out.operator = "<"
        out.text = text
        return out

    match = _LOWER_RE.match(text)
    if match:
        out.low = _to_float(match.group(1))
        out.operator = ">"
        out.text = text
        return out

    # A bare number is not a range. It is usually the neighbouring column
    # bleeding in (``range: "1.31"`` sitting beside ``result: "1.31"``), so it
    # is recorded and flagged rather than read as a bound.
    if _SINGLE_RE.match(text):
        out.text = text
        out.flags.append(Flag.RANGE_UNPARSEABLE)
        return out

    out.text = text
    out.flags.append(Flag.RANGE_UNPARSEABLE)
    return out


def range_matches_envelope(
    parsed: ParsedRange,
    envelope: tuple[float | None, float | None],
    tolerance: float,
) -> bool:
    """Could this reference range plausibly belong to a test with this envelope?

    The coherence engine's second signal. Labs legitimately differ on reference
    ranges, so the envelope is widened by ``tolerance`` before judging; the
    question being asked is not "is this the exact range we expect" but "is
    this range even the right order of magnitude for this analyte".

    A haemoglobin range of ``4000-10000`` fails this against an envelope of
    ``1-25`` by three orders of magnitude, which is the point.

    ``tolerance`` is a *multiplier*, not an additive span, because analyte
    magnitudes differ by six orders between a platelet count and a bilirubin.
    Additive slack that is reasonable for one is meaningless for the other;
    scaling by ratio treats every analyte alike. At the default 2.0 a lab may
    use a range twice as wide as our envelope in either direction before we
    start to doubt that the row belongs to this test.

    A bound at or below zero carries no magnitude information (a bilirubin
    envelope legitimately starts at 0), so that side is left unconstrained
    rather than scaled - scaling zero would reject every valid range.
    """
    lo_env, hi_env = envelope
    if lo_env is None and hi_env is None:
        return True
    observed = [v for v in (parsed.low, parsed.high) if v is not None]
    if not observed:
        return True

    tol = max(tolerance, 1.0)
    lo_allowed = lo_env / tol if (lo_env is not None and lo_env > 0) else float("-inf")
    hi_allowed = hi_env * tol if (hi_env is not None and hi_env > 0) else float("inf")

    return all(lo_allowed <= v <= hi_allowed for v in observed)
