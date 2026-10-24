"""Result parsing (FR-2.3) - turning a free-text cell into a number, a verdict
or an honest refusal.

The ``result`` column is the least disciplined field in the corpus. Observed
shapes, all of which occur in the five supplied files:

    9700                        plain integer
    5.45                        plain decimal
    04 / 00                     zero-padded
    4,290 cells/cu.mm           thousands comma plus an inline unit
    13.7 g/dl                   value with its unit repeated inside the cell
    H 0.7 %                     lab's own high/low marker glued on the front
    98.6 degree F               value needing an affine unit conversion
    100/60 mmHg                 compound: two measurements in one cell
    114/min                     value whose unit merely contains a slash
    99% On Room Air             value with clinical commentary appended
    POSITIVE / NEGATIVE / NIL   qualitative
    1.5-4.5                     the reference range, leaked into the result
    OTE                         OCR debris
    0-2 Hyaline Cast            a count range with a morphology note
    12.0 g/dL, 36.10, 13-17, 40-50, 85.5 fL, ...    a whole panel in one cell
    <long USG narrative>        a radiology report in a lab-result column

Two rules govern all of it:

1.  **Never invent a number.** If the cell cannot be read as a value for this
    test, no ``result_value`` is written. ``result_text`` keeps what was there
    and the row is flagged. A null an analyst can see beats a plausible number
    nobody can defend.
2.  **Never discard the original.** ``result_text_original`` always holds the
    raw cell, so every decision in this module is reversible and reviewable.

``100/60 mmHg`` versus ``114/min`` is decided by the *test*, not the string:
the catalogue declares ``BLOOD PRESSURE`` as ``value_type: compound`` with a
``/`` separator, so only there is the slash read as a separator. Elsewhere it
is part of a unit. Context beats cleverness.
"""

from __future__ import annotations

import re

from ..config import CanonicalTest
from ..models import Flag, ParsedResult

_NUM = r"[-+]?\d+(?:[.,]\d+)?"

# Leading value, then whatever follows. The remainder is examined separately
# rather than being assumed to be a unit.
_LEADING_NUM_RE = re.compile(rf"^\s*({_NUM})\s*(.*)$", re.DOTALL)
_PREFIX_MARKER_RE = re.compile(r"^\s*(?:(H|L|HH|LL)\b|(\*+))\s*(.+)$")
_OPERATOR_RE = re.compile(rf"^\s*(<=|>=|<|>)\s*({_NUM})\s*(.*)$")
_INTERVAL_RE = re.compile(rf"^\s*{_NUM}\s*(?:-|–|to|TO)\s*{_NUM}\s*")
_COMPOUND_RE = re.compile(rf"^\s*({_NUM})\s*/\s*({_NUM})\s*(.*)$")
# Two or more "number unit" groups separated by commas: a packed panel.
# Applied only after thousands commas are removed - "4,290 cells/cu.mm" is one
# value, not two, and reading it as a packed panel would discard a good result.
_PACKED_RE = re.compile(rf"{_NUM}[^,]*,\s*{_NUM}")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# The labelled variant: "Neutrophils: 2230.8 cells/cu.mm, Lymphocytes: 1716 ...".
# Four analytes in one cell, each recoverable - but attributing them to the row
# that names the panel would be wrong, so the row is flagged for the recovery
# path rather than parsed for a single value.
_PACKED_LABELLED_RE = re.compile(rf"[A-Za-z]{{3,}}\s*[:=]\s*{_NUM}")
# A unit-ish remainder: letters, %, slashes, dots, carets, digits. Anything
# with spaces between words is prose, not a unit.
_UNITISH_RE = re.compile(r"^[A-Za-z%µμ/.^0-9\-\s]{1,22}$")

_QUALITATIVE_SYNONYMS: dict[str, str] = {
    "positive": "POSITIVE", "pos": "POSITIVE", "reactive": "REACTIVE",
    "negative": "NEGATIVE", "neg": "NEGATIVE", "non reactive": "NON REACTIVE",
    "nonreactive": "NON REACTIVE", "not detected": "NOT DETECTED",
    "detected": "DETECTED", "absent": "ABSENT", "present": "PRESENT",
    "nil": "NIL", "normal": "NORMAL", "trace": "TRACE",
    "equivocal": "EQUIVOCAL", "nad": "NORMAL",
    "no growth": "NO GROWTH", "sterile": "NO GROWTH",
}

_NARRATIVE_MIN_WORDS = 8          # beyond this it is a report, not a result


def parse_number(token: str) -> float | None:
    """Parse one numeric token, handling Indian thousands commas.

    ``4,290`` is four thousand two hundred and ninety; ``4,29`` is a decimal
    comma. The discriminator is a comma followed by exactly three digits.
    """
    t = (token or "").strip()
    if not t:
        return None
    if re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+", t):
        t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        value = float(t)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def decimals_in(token: str) -> int:
    """Decimal places in the source token, so output precision mirrors input.

    ``12.0 g/dL`` should not become ``12`` and ``0.90`` should not become
    ``0.9``: a lab reports precision deliberately, and silently changing it
    changes what an analyst believes about the measurement.
    """
    match = re.search(r"[.,](\d+)", token or "")
    return len(match.group(1)) if match else 0


def round_like(value: float | None, source_token: str, extra: int = 2) -> float | None:
    """Round ``value`` to the source's precision, plus headroom for conversion."""
    if value is None:
        return None
    places = min(decimals_in(source_token) + extra, 10)
    rounded = round(value, places)
    # Collapse float noise such as 37.00000000296 -> 37.0
    return 0.0 if rounded == 0 else rounded


def _classify_remainder(remainder: str) -> tuple[str | None, bool]:
    """Interpret what follows the number.

    Returns ``(inline_unit, is_prose)``. Prose means the cell carried clinical
    commentary, which is kept as text and flagged - ``99% On Room Air`` is a
    valid saturation plus a fact about oxygen delivery that belongs in a note,
    not silently dropped.
    """
    rest = (remainder or "").strip().rstrip(".,;")
    if not rest:
        return None, False
    if len(rest.split()) >= 3 or len(rest) > 22:
        return None, True
    if _UNITISH_RE.match(rest):
        return rest, False
    return None, True


def parse_result(
    raw: object,
    *,
    test: CanonicalTest | None = None,
    range_original: str | None = None,
    garbage_tokens: frozenset[str] | tuple[str, ...] = (),
    prefix_markers: dict[str, str] | None = None,
) -> ParsedResult:
    """Parse a result cell in the context of the test it claims to be."""
    original = "" if raw is None else str(raw)
    text = original.strip()
    out = ParsedResult(original=original)

    if not text:
        out.flags.append(Flag.RESULT_MISSING)
        return out

    garbage = {g.casefold() for g in garbage_tokens}
    if text.casefold() in garbage:
        out.text = text
        out.flags.append(Flag.OCR_GARBAGE_TOKEN)
        return out

    # The result column repeating the reference range verbatim is a
    # column-collapse artefact. There is no measurement in this row; reading
    # "1.5-4.5" as 1.5 would fabricate one.
    if range_original is not None and text and text == str(range_original).strip():
        out.text = text
        out.flags.append(Flag.RESULT_EQUALS_RANGE)
        return out

    # The lab's own high/low marker. Recorded as evidence - it is a useful
    # cross-check against our own classification - then removed so the value
    # can be read.
    marker_match = _PREFIX_MARKER_RE.match(text)
    if marker_match:
        token = marker_match.group(1) or marker_match.group(2)
        mapped = (prefix_markers or {}).get(token) or (prefix_markers or {}).get(
            (token or "").upper()
        )
        if mapped or token in ("H", "L", "HH", "LL") or set(token or "") == {"*"}:
            out.prefix_marker = mapped or token
            out.flags.append(Flag.RESULT_PREFIX_MARKER)
            text = marker_match.group(3).strip()

    # A whole panel packed into one cell. Splitting it would attribute every
    # value to whichever test the row happens to name.
    without_thousands = _THOUSANDS_RE.sub("", text)
    if not _INTERVAL_RE.match(text) and (
        _PACKED_RE.search(without_thousands)
        or len(_PACKED_LABELLED_RE.findall(without_thousands)) > 1
    ):
        out.text = text[:512]
        out.flags.append(Flag.MULTI_VALUE_PACKED)
        return out

    # Narrative: a radiology or microbiology report in a lab-result column.
    if len(text.split()) >= _NARRATIVE_MIN_WORDS:
        out.text = text[:2000]
        out.flags.append(Flag.RESULT_NON_NUMERIC)
        return out

    # Compound, only where the catalogue says this test is compound.
    if test is not None and test.value_type == "compound" and test.compound_split:
        match = _COMPOUND_RE.match(text)
        if match:
            components = test.compound_split.get("components") or []
            first, second = parse_number(match.group(1)), parse_number(match.group(2))
            values = [v for v in (first, second) if v is not None]
            if len(values) == 2 and len(components) >= 2:
                out.components = [
                    (components[0], round_like(first, match.group(1))),
                    (components[1], round_like(second, match.group(2))),
                ]
                out.value = round_like(first, match.group(1))   # systolic, by convention
                out.inline_unit, _ = _classify_remainder(match.group(3))
                out.text = text
                out.flags.append(Flag.COMPOUND_SPLIT)
                return out

    # Qualitative: either the catalogue expects it, or the cell plainly is one.
    lowered = text.casefold().strip(" .")
    canonical_qualitative = _QUALITATIVE_SYNONYMS.get(lowered)
    if canonical_qualitative is None:
        for phrase, mapped in _QUALITATIVE_SYNONYMS.items():
            if lowered.startswith(phrase) and len(lowered) <= len(phrase) + 12:
                canonical_qualitative = mapped
                break
    if canonical_qualitative is not None:
        out.text = canonical_qualitative
        if test is not None and test.value_type not in ("qualitative", "narrative"):
            # A numeric test answered qualitatively: legitimate (a titre
            # reported as POSITIVE) but it cannot be range-checked, so it is
            # flagged rather than silently classified.
            out.flags.append(Flag.RESULT_NON_NUMERIC)
        return out

    # An interval where a single value belongs.
    if _INTERVAL_RE.match(text):
        out.text = text
        out.flags.append(Flag.RESULT_NON_NUMERIC)
        return out

    # Operator-qualified value: "<0.5" is a real, usable measurement.
    operator_match = _OPERATOR_RE.match(text)
    if operator_match:
        value = parse_number(operator_match.group(2))
        out.value = round_like(value, operator_match.group(2))
        out.text = text
        unit, prose = _classify_remainder(operator_match.group(3))
        out.inline_unit = unit
        if prose:
            out.flags.append(Flag.RESULT_NON_NUMERIC)
        return out

    match = _LEADING_NUM_RE.match(text)
    if match:
        value = parse_number(match.group(1))
        if value is not None:
            out.value = round_like(value, match.group(1))
            unit, prose = _classify_remainder(match.group(2))
            out.inline_unit = unit
            if prose:
                out.text = text
                out.flags.append(Flag.RESULT_NON_NUMERIC)
            return out

    out.text = text[:512]
    out.flags.append(Flag.RESULT_NON_NUMERIC)
    return out


# ---------------------------------------------------------------------------
# Values hidden inside test names
# ---------------------------------------------------------------------------

# Deliberately conservative: a name, a separator, a number. Anything looser
# starts matching strengths in brand names ("TAB. PAN 40") and panel codes.
# Two shapes occur: "SGOT - 38" (separator) and "Na + (127)" (parenthesised).
_EMBEDDED_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z .'/+()-]{1,38}?)\s*[-:=]\s*(?P<value>[-+]?\d+(?:\.\d+)?)"
    r"(?![\d/.-])"
)
_EMBEDDED_PAREN_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z .'/+-]{0,38}?)\s*\(\s*(?P<value>[-+]?\d+(?:\.\d+)?)\s*\)"
)
# Strips a leading panel label so "LFT ( SGOT" becomes "SGOT". The panel name
# is not lost - it stays in `test_name_original` on the parent row.
_PANEL_PREFIX_RE = re.compile(r"^[A-Za-z]{2,6}\s*\(\s*")


def extract_embedded_values(name: str, max_items: int = 8) -> list[tuple[str, float]]:
    """Pull ``(test, value)`` pairs out of a name that swallowed its results.

    ``Sample_JSON_file5.json`` has rows whose ``result`` is empty because the
    values were absorbed into the name:

        "LFT ( SGOT - 38, SGPT -14, ALP - 127)"  -> 3 results
        "KFT - sr creatinine - 0.3"              -> 1 result
        "Na + (127)"                             -> 1 result

    Three measurements in a cell no downstream rule will ever read. Regex
    recovers the regular shapes; the LLM tier
    (:mod:`veritas.standardise.llm`) is offered only what regex could not
    split, which is how GenAI stays a tail strategy rather than a line item.

    Recovered rows are emitted with ``normalization_method`` recording the
    recovery, never merged silently into the parent row.
    """
    if not name or not any(ch in name for ch in "(-:="):
        return []

    found: list[tuple[str, float]] = []
    seen: set[str] = set()

    def offer(label: str, raw_value: str) -> None:
        cleaned = _PANEL_PREFIX_RE.sub("", label).strip(" .'/+()-")
        value = parse_number(raw_value)
        if not cleaned or value is None or len(cleaned) < 2:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        found.append((cleaned, value))

    for match in _EMBEDDED_PAREN_RE.finditer(name):
        offer(match.group("name"), match.group("value"))
        if len(found) >= max_items:
            return found

    spaced = name.replace("(", " ( ").replace(")", " ) ")
    for match in _EMBEDDED_RE.finditer(spaced):
        offer(match.group("name"), match.group("value"))
        if len(found) >= max_items:
            break
    return found
