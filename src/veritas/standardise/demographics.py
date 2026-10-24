"""Demographic and date normalisation (FR-2.5).

Age, sex and dates are the fields regulatory reporting is graded on, and they
are the fields clinics are least consistent about. Observed in the corpus:

    age         "33Y11M26D", "45 Years", "6/12" (six months), "[AGE REDACTED]"
    gender      "M", "Male", "FEMALE", "F", "[GENDER REDACTED]"
    dates       "09-10-2025", "07-Oct-2025", "08/Oct/2025", "DD/MM/YYYY"
    bill_date   "LAB10945"  - a bill *number* in a date column

The two hard cases both appear in the samples and both are handled by refusing
to guess:

*   ``DD/MM/YYYY`` is a placeholder the source emitted instead of leaving the
    field empty. Parsed as a date it would silently become today, or 1900, or
    raise - all three are wrong. It is recognised as a placeholder, the column
    is left null and the row is flagged.
*   ``LAB10945`` in ``bill_date`` is a different field's value. There is no
    date here; inventing one would corrupt the billing timeline that claims
    ageing reports are built on.

Ambiguous day/month ordering is resolved by the clinic profile's
``date_formats`` list, in order, because the answer is a property of the source
system and not of the string: ``03-04-2025`` is unknowable in isolation and
unambiguous once you know the clinic writes ``%d-%m-%Y``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ..models import Flag

_PLACEHOLDER_DATES = frozenset({
    "dd/mm/yyyy", "dd-mm-yyyy", "yyyy-mm-dd", "mm/dd/yyyy", "dd/mm/yy",
    "date", "n/a", "na", "null", "none", "-", "--", "",
})

# "33Y11M26D" and friends: any subset, any order, with or without separators.
_AGE_YMD_RE = re.compile(
    r"(?:(?P<y>\d{1,3})\s*(?:y|yr|yrs|year|years)\b)?\s*"
    r"(?:(?P<m>\d{1,2})\s*(?:m|mo|mon|month|months)\b)?\s*"
    r"(?:(?P<d>\d{1,3})\s*(?:d|day|days)\b)?",
    re.IGNORECASE,
)
_AGE_COMPACT_RE = re.compile(
    r"^(?:(?P<y>\d{1,3})Y)?(?:(?P<m>\d{1,2})M)?(?:(?P<d>\d{1,3})D)?$", re.IGNORECASE
)
_AGE_PLAIN_RE = re.compile(r"^(?P<y>\d{1,3})(?:\.(?P<frac>\d+))?$")
# Paediatric shorthand: "6/12" is six months, "3/52" three weeks.
_AGE_FRACTION_RE = re.compile(r"^(?P<n>\d{1,3})\s*/\s*(?P<denom>12|52|365)$")

_MALE = frozenset({"m", "male", "man", "boy", "1"})
_FEMALE = frozenset({"f", "female", "woman", "girl", "2"})
_OTHER = frozenset({"o", "other", "transgender", "tg", "x", "3"})


@dataclass(slots=True)
class ParsedAge:
    years: float | None = None
    text: str | None = None
    flags: tuple[Flag, ...] = ()


def is_redacted(value: object, markers: frozenset[str] | tuple[str, ...] = ()) -> bool:
    """Whether a value is a redaction placeholder rather than data.

    Every identifier in the supplied samples is redacted
    (``[PATIENT NAME REDACTED]``). Treating those as real values would produce
    a warehouse where 200,000 patients share a name, and a tokeniser that
    hashes the marker into a single stable token - which looks exactly like one
    very busy patient.
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    if text in set(markers):
        return True
    # Generic shape: a bracketed all-caps marker containing REDACTED.
    return bool(re.fullmatch(r"\[[^\]]*REDACTED[^\]]*\]", text, re.IGNORECASE))


def parse_age(raw: object, markers: frozenset[str] | tuple[str, ...] = ()) -> ParsedAge:
    """Normalise an age to decimal years, keeping the original text."""
    if raw is None:
        return ParsedAge()
    text = str(raw).strip()
    if not text:
        return ParsedAge()
    if is_redacted(text, markers):
        return ParsedAge(None, text, (Flag.AGE_UNPARSEABLE,))

    compact = _AGE_COMPACT_RE.match(text.replace(" ", ""))
    if compact and any(compact.groupdict().values()):
        years = int(compact.group("y") or 0)
        months = int(compact.group("m") or 0)
        days = int(compact.group("d") or 0)
        return ParsedAge(round(years + months / 12 + days / 365.25, 3), text)

    fraction = _AGE_FRACTION_RE.match(text)
    if fraction:
        denom = int(fraction.group("denom"))
        return ParsedAge(round(int(fraction.group("n")) / denom, 3), text)

    plain = _AGE_PLAIN_RE.match(text)
    if plain:
        value = float(text)
        if 0 <= value <= 130:
            return ParsedAge(round(value, 3), text)
        return ParsedAge(None, text, (Flag.AGE_UNPARSEABLE,))

    worded = _AGE_YMD_RE.search(text)
    if worded and any(worded.groupdict().values()):
        years = int(worded.group("y") or 0)
        months = int(worded.group("m") or 0)
        days = int(worded.group("d") or 0)
        total = years + months / 12 + days / 365.25
        if 0 < total <= 130:
            return ParsedAge(round(total, 3), text)

    return ParsedAge(None, text, (Flag.AGE_UNPARSEABLE,))


def parse_gender(raw: object, markers: frozenset[str] | tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """Normalise sex to ``M`` / ``F`` / ``O``, returning ``(canonical, original)``.

    Unknown values return ``None`` rather than a default. Sex selects the
    reference range, so defaulting it would silently apply male ranges to
    female patients - a quiet, systematic accuracy loss.
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text or is_redacted(text, markers):
        return None, text or None
    lowered = text.casefold().strip(" .")
    if lowered in _MALE:
        return "M", text
    if lowered in _FEMALE:
        return "F", text
    if lowered in _OTHER:
        return "O", text
    return None, text


def parse_date(
    raw: object,
    formats: list[str] | tuple[str, ...] = (),
    markers: frozenset[str] | tuple[str, ...] = (),
) -> tuple[str | None, str | None, tuple[Flag, ...]]:
    """Parse a date to ISO 8601, returning ``(iso, original, flags)``."""
    if raw is None:
        return None, None, ()
    text = str(raw).strip()
    if not text:
        return None, None, ()
    if text.casefold() in _PLACEHOLDER_DATES:
        return None, text, (Flag.PLACEHOLDER_DATE,)
    if is_redacted(text, markers):
        return None, text, (Flag.PLACEHOLDER_DATE,)

    for fmt in list(formats) or ["%Y-%m-%d"]:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if not _plausible(parsed):
            return None, text, (Flag.DATE_UNPARSEABLE,)
        return parsed.isoformat(), text, ()

    # Last resort: ISO-ish strings with a time component.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        if _plausible(parsed):
            return parsed.isoformat(), text, ()
    except ValueError:
        pass

    return None, text, (Flag.DATE_UNPARSEABLE,)


def _plausible(value: date) -> bool:
    """Reject dates that parsed but cannot be a clinical event.

    A claim cannot reference a 1900 admission or one three years in the future;
    such a value is a parse accident, and accepting it corrupts every
    time-series built on the column.
    """
    year = value.year
    return 1900 < year <= date.today().year + 2
