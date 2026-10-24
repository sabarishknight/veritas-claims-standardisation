"""Brand-to-generic medicine resolution (FR-2.6).

Indian discharge summaries prescribe in brands, free-text, with the dosage
form glued to the front and the strength to the back:

    "Tab. miso"            "CAP. PAN 40"        "TAB. CEFTAS- 400"
    "INJ PANTOP 40 MG IV 12 HRLY"               "SYP DUPHALAC"
    "POWDER NATUROLAX"     "Inj. Pan"           "N/A"

Claims are priced on the brand but analysed on the molecule, so the mapping is
what makes drug-utilisation and duplicate-therapy detection possible at all:
``Tab. Pan 40``, ``CAP. PAN 40`` and ``INJ PANTOP 40 MG`` are one molecule
across three claims.

Three deliberate restraints:

1.  **Consumables are not drugs.** ``IV FIXER``, ``SYRINGE 10ML``,
    ``IVSET INTRAFLOW`` and ``N/A`` are filtered by config, not mapped to a
    nearest-looking molecule.
2.  **Combinations stay combinations.** ``PAN D`` is pantoprazole *and*
    domperidone. Collapsing it to pantoprazole would under-count a molecule
    and hide a duplicate-therapy signal.
3.  **Uncertainty is labelled, not hidden.** Entries the catalogue marks
    ``verify: true`` resolve, but the row carries ``brand_unverified`` so it
    can be excluded from clinical analytics until a pharmacist signs it off.
    In production this table is a licensed drug master, not a hand-built YAML
    (docs/assumptions.md D-9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import normalise_name
from ..models import Flag

# Strength and its unit, at the end or glued on with punctuation.
_STRENGTH_RE = re.compile(
    r"[-\s]*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>mg|mcg|g|gm|gr|ml|iu|units?|%)?\b",
    re.IGNORECASE,
)
# Administration instructions that trail the medicine name.
_ROUTE_NOISE_RE = re.compile(
    r"\b(iv|im|po|sos|stat|bd|od|tds|qid|hrly|hourly|daily|days?|"
    r"before|after|food|breakfast|bed\s*time|night|morning)\b.*$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ResolvedMedicine:
    original: str
    brand: str | None = None
    generic: str | None = None
    atc_code: str | None = None
    dosage_form: str | None = None
    strength: str | None = None
    is_combination: bool = False
    confidence: float = 0.0
    flags: tuple[Flag, ...] = ()
    skip_reason: str | None = None


class MedicineResolver:
    """Config-driven brand resolution.

    Index is built once; lookups are dict hits on a normalised brand token,
    with a bounded prefix fallback for brands that arrive with trailing noise
    the regexes did not strip.
    """

    def __init__(self, medicines_cfg: dict[str, Any]) -> None:
        self._forms: list[tuple[str, str]] = [
            (str(entry["token"]).casefold(), str(entry["form"]))
            for entry in (medicines_cfg.get("dosage_forms") or [])
        ]
        # Longest token first, so "tab." wins over "tab".
        self._forms.sort(key=lambda t: -len(t[0]))

        self._skip: frozenset[str] = frozenset(
            normalise_name(t) for t in (medicines_cfg.get("non_medicine_tokens") or [])
        )

        self._variants: dict[str, dict[str, Any]] = {}
        for brand in medicines_cfg.get("brands") or []:
            for variant in brand.get("variants") or []:
                self._variants.setdefault(normalise_name(variant), brand)
        # Prefix fallback, longest variant first so "pan d" beats "pan".
        self._variant_keys: list[str] = sorted(self._variants, key=len, reverse=True)

    # -- parsing ---------------------------------------------------------
    def _split_form(self, text: str) -> tuple[str | None, str]:
        stripped = text.strip().strip(".,;:-").strip()
        lowered = stripped.casefold()
        for token, form in self._forms:
            if lowered.startswith(token):
                remainder = stripped[len(token):].strip(" .,-")
                # Only treat it as a form prefix if something survives; "IV"
                # alone is a fluid order, not a tablet of nothing.
                if remainder:
                    return form, remainder
        return None, stripped

    def _split_strength(self, text: str) -> tuple[str | None, str]:
        matches = list(_STRENGTH_RE.finditer(text))
        if not matches:
            return None, text
        last = matches[-1]
        # Only strip it if it really is trailing; a digit inside a name
        # ("B12") must survive.
        if last.end() < len(text.rstrip()) - 1:
            return None, text
        unit = (last.group("unit") or "").lower()
        strength = f"{last.group('value')}{(' ' + unit) if unit else ''}".strip()
        return strength, text[: last.start()].strip(" .,-")

    # -- resolution ------------------------------------------------------
    def resolve(self, raw: object) -> ResolvedMedicine:
        original = "" if raw is None else str(raw).strip()
        if not original:
            return ResolvedMedicine(original, skip_reason="empty")

        if normalise_name(original) in self._skip:
            return ResolvedMedicine(original, skip_reason="not_a_medicine")

        form, remainder = self._split_form(original)
        remainder = _ROUTE_NOISE_RE.sub("", remainder).strip(" .,-")
        strength, brand_text = self._split_strength(remainder)
        brand_key = normalise_name(brand_text)

        if not brand_key:
            return ResolvedMedicine(original, dosage_form=form, strength=strength,
                                    skip_reason="no_brand_token")
        if brand_key in self._skip:
            return ResolvedMedicine(original, dosage_form=form, strength=strength,
                                    skip_reason="not_a_medicine")

        entry = self._variants.get(brand_key)
        confidence_penalty = 0.0
        if entry is None:
            # Bounded prefix fallback: the brand token starts with a known
            # variant and adds at most a short suffix of noise.
            for key in self._variant_keys:
                if brand_key.startswith(key + " ") or brand_key == key:
                    entry = self._variants[key]
                    confidence_penalty = 0.1
                    break

        if entry is None:
            return ResolvedMedicine(
                original, dosage_form=form, strength=strength,
                skip_reason="brand_not_in_reference",
            )

        flags: list[Flag] = []
        if entry.get("verify"):
            flags.append(Flag.BRAND_UNVERIFIED)

        return ResolvedMedicine(
            original=original,
            brand=entry.get("brand"),
            generic=entry.get("generic"),
            atc_code=entry.get("atc"),
            dosage_form=form,
            strength=strength,
            is_combination=bool(entry.get("combination")),
            confidence=round(max(0.0, float(entry.get("confidence", 0.8)) - confidence_penalty), 3),
            flags=tuple(flags),
        )
