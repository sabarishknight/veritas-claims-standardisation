"""Configuration loading and the in-memory indexes built from it.

Everything clinic-specific or domain-specific is config (NFR-2.1). This module
is the only place that reads it, so there is exactly one answer to "where does
this behaviour come from".

Two things worth noting:

*   **Profiles inherit.** A clinic profile states only what differs from
    ``_default``, which keeps a new clinic to a dozen lines and makes a diff
    between two clinics readable.
*   **A config fingerprint is recorded on every run.** Output that cannot be
    tied to the configuration that produced it is not auditable: the same file
    reprocessed after a catalogue change legitimately yields a different
    canonical name, and the fingerprint is how an analyst sees why.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:                                  # pragma: no cover
    raise SystemExit(
        "PyYAML is required to read the configuration.\n"
        "Install it with:  python3 -m pip install pyyaml\n"
        "(It is the pipeline's only runtime dependency.)"
    ) from exc


DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


# ---------------------------------------------------------------------------
# String normalisation
#
# Used for both alias lookup and fuzzy matching, so it must be stable and
# cheap: it runs tens of millions of times a day. Deliberately aggressive -
# "ALBUMIN, SERUM", "Albumin (serum)" and "albumin serum" must collapse to one
# key - but it never drops digits, because "VITAMIN B12" and "VITAMIN B6" are
# different tests.
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")

# Qualifiers that say where the specimen came from, not what was measured.
# Dropping them is what lets "CREATININE, SERUM" meet "Serum Creatinine".
_SPECIMEN_WORDS = frozenset({
    "serum", "plasma", "blood", "whole", "edta", "random", "urine", "fasting",
    "calculated", "quantitative", "qualitative", "method", "slide", "automated",
})


def normalise_name(value: str) -> str:
    """Fold a test name to a comparison key."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def normalise_name_strict(value: str) -> str:
    """As :func:`normalise_name`, additionally dropping specimen qualifiers and
    sorting tokens, so word order stops mattering."""
    base = normalise_name(value)
    if not base:
        return ""
    tokens = [t for t in base.split() if t not in _SPECIMEN_WORDS]
    if not tokens:
        tokens = base.split()
    return " ".join(sorted(tokens))


def normalise_unit(value: str) -> str:
    """Fold a unit spelling. Keeps ``/`` and ``.`` and ``^`` - they carry
    meaning in ``mil/cu.mm`` and ``10^6/uL`` - but folds case, spaces and
    micro-sign variants."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value)).strip()
    s = s.replace("µ", "u").replace("μ", "u")
    s = re.sub(r"\s+", "", s)
    return s.casefold()


# ---------------------------------------------------------------------------
# Catalogue types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ReferenceRange:
    sex: str
    low: float | None
    high: float | None


@dataclass(slots=True)
class CanonicalTest:
    canonical: str
    loinc: str | None
    panel: str | None
    dimension: str
    canonical_unit: str
    value_type: str
    reference_ranges: list[ReferenceRange]
    plausible_low: float | None
    plausible_high: float | None
    aliases: list[str]
    qualitative_values: list[str] = field(default_factory=list)
    compound_split: dict[str, Any] | None = None
    derived_from: dict[str, Any] | None = None

    def range_for(self, sex: str | None) -> ReferenceRange | None:
        """First range matching the patient's sex, else the ``ANY`` fallback.

        Sex is redacted in the supplied samples, so in practice this resolves
        to ``ANY`` - a deliberately widened band. Production traffic with sex
        present gets the tighter, more accurate band for free; see
        docs/assumptions.md D-4.
        """
        if not self.reference_ranges:
            return None
        s = (sex or "").strip().upper()[:1]
        if s in ("M", "F"):
            for r in self.reference_ranges:
                if r.sex.upper().startswith(s):
                    return r
        for r in self.reference_ranges:
            if r.sex.upper() == "ANY":
                return r
        return self.reference_ranges[0]

    def envelope(self) -> tuple[float | None, float | None]:
        """Hard physiological bounds, falling back to a widened reference
        range when the catalogue does not state them."""
        if self.plausible_low is not None or self.plausible_high is not None:
            return self.plausible_low, self.plausible_high
        r = self.range_for(None)
        if not r:
            return None, None
        lo = None if r.low is None else r.low * 0.1
        hi = None if r.high is None else r.high * 10.0
        return lo, hi


@dataclass(slots=True)
class UnitSpec:
    spelling: str
    dimension: str
    factor: float
    offset: float = 0.0


class Catalog:
    """Indexed view of ``test_catalog.yaml`` + ``units.yaml``.

    Built once per process and shared read-only by workers. All lookups are
    dict hits; the only linear scan is the fuzzy tier, which runs on the small
    fraction of names the dicts miss.
    """

    def __init__(self, catalog_doc: dict[str, Any], units_doc: dict[str, Any]) -> None:
        self._raw_catalog = catalog_doc
        self._raw_units = units_doc

        self.tests: dict[str, CanonicalTest] = {}
        # Primary index: alias -> single best canonical. Used when there is no
        # unit to disambiguate with.
        self.by_alias_exact: dict[str, str] = {}
        self.by_alias_normalised: dict[str, str] = {}
        self.by_alias_strict: dict[str, str] = {}
        # Candidate index: alias -> every canonical that claims it, in
        # catalogue order. Some names are genuinely ambiguous until you look at
        # the unit - "NEUTROPHILS" is a percentage or an absolute count - and
        # the resolver settles those with the row's unit dimension.
        self.by_alias_candidates: dict[str, list[str]] = {}

        defaults = catalog_doc.get("defaults", {}) or {}
        for entry in catalog_doc.get("canonical_tests", []) or []:
            plausible = entry.get("plausible") or {}
            test = CanonicalTest(
                canonical=entry["canonical"],
                loinc=entry.get("loinc"),
                panel=entry.get("panel"),
                dimension=entry["dimension"],
                canonical_unit=entry["canonical_unit"],
                value_type=entry.get("value_type", defaults.get("value_type", "numeric")),
                reference_ranges=[
                    ReferenceRange(r.get("sex", "ANY"), r.get("low"), r.get("high"))
                    for r in (entry.get("reference_ranges") or [])
                ],
                plausible_low=plausible.get("low"),
                plausible_high=plausible.get("high"),
                aliases=list(entry.get("aliases") or []),
                qualitative_values=list(entry.get("qualitative_values") or []),
                compound_split=entry.get("compound_split"),
                derived_from=entry.get("derived_from"),
            )
            self.tests[test.canonical] = test

            for alias in [test.canonical, *test.aliases]:
                self.by_alias_exact.setdefault(alias, test.canonical)
                self.by_alias_normalised.setdefault(normalise_name(alias), test.canonical)
                for key in {alias, normalise_name(alias)}:
                    bucket = self.by_alias_candidates.setdefault(key, [])
                    if test.canonical not in bucket:
                        bucket.append(test.canonical)
                # Strict keys collide more readily (token-sorted, qualifiers
                # dropped). First writer wins and we do not overwrite, so a
                # collision degrades to "resolved to the earlier test" rather
                # than to a silent flip-flop between runs.
                self.by_alias_strict.setdefault(normalise_name_strict(alias), test.canonical)

        self.non_test_patterns: list[tuple[re.Pattern[str], str]] = [
            (re.compile(p["pattern"], re.IGNORECASE), p["reason"])
            for p in catalog_doc.get("non_test_patterns", []) or []
        ]

        # Units
        self.units: dict[str, UnitSpec] = {}
        self.dimension_base: dict[str, str] = {}
        for dim, spec in (units_doc.get("dimensions") or {}).items():
            self.dimension_base[dim] = spec["base_unit"]
            for spelling, conv in (spec.get("units") or {}).items():
                if isinstance(conv, dict):
                    factor = float(conv.get("factor", 1.0))
                    offset = float(conv.get("offset", 0.0))
                else:
                    factor, offset = float(conv), 0.0
                self.units.setdefault(
                    normalise_unit(spelling),
                    UnitSpec(spelling, dim, factor, offset),
                )
        self.non_unit_tokens: frozenset[str] = frozenset(
            normalise_unit(t) for t in (units_doc.get("non_unit_tokens") or [])
        )

        self._validate()

    # -- integrity -------------------------------------------------------
    def _validate(self) -> None:
        """Fail at load time, not per row.

        A catalogue that declares a canonical unit outside its own dimension
        would silently produce unconverted values on one test and nobody would
        notice for a month. Config errors must be loud and immediate.
        """
        problems: list[str] = []
        for test in self.tests.values():
            if test.dimension not in self.dimension_base:
                problems.append(f"{test.canonical}: unknown dimension {test.dimension!r}")
                continue
            spec = self.units.get(normalise_unit(test.canonical_unit))
            if spec is None:
                problems.append(
                    f"{test.canonical}: canonical_unit {test.canonical_unit!r} is not a known unit"
                )
            elif spec.dimension != test.dimension:
                problems.append(
                    f"{test.canonical}: canonical_unit {test.canonical_unit!r} belongs to "
                    f"dimension {spec.dimension!r}, not {test.dimension!r}"
                )
            if test.compound_split:
                for comp in test.compound_split.get("components", []):
                    if comp not in self.tests:
                        problems.append(
                            f"{test.canonical}: compound component {comp!r} is not in the catalogue"
                        )
        if problems:
            raise ValueError(
                "test_catalog.yaml / units.yaml are inconsistent:\n  - "
                + "\n  - ".join(problems)
            )

    # -- lookups ---------------------------------------------------------
    def unit_spec(self, spelling: str) -> UnitSpec | None:
        return self.units.get(normalise_unit(spelling))

    def is_non_unit_token(self, spelling: str) -> bool:
        return normalise_unit(spelling) in self.non_unit_tokens

    def candidates_for(self, alias: str) -> list[str]:
        """Every canonical test that claims this alias, in catalogue order."""
        return self.by_alias_candidates.get(alias) or self.by_alias_candidates.get(
            normalise_name(alias), []
        )

    def disambiguate_by_unit(self, candidates: list[str], unit: str | None) -> str | None:
        """Pick the candidate whose dimension matches the row's unit.

        Returns ``None`` when the unit cannot settle it, so the caller falls
        back to the primary index rather than picking arbitrarily.
        """
        if len(candidates) < 2:
            return candidates[0] if candidates else None
        spec = self.unit_spec(unit or "")
        if spec is None:
            return None
        matches = [
            name for name in candidates
            if (test := self.tests.get(name)) and test.dimension == spec.dimension
        ]
        return matches[0] if len(matches) == 1 else None

    def match_non_test(self, name: str) -> str | None:
        for pattern, reason in self.non_test_patterns:
            if pattern.search(name or ""):
                return reason
        return None

    def convert(self, value: float, from_unit: str, test: CanonicalTest) -> tuple[float | None, str]:
        """Convert ``value`` from ``from_unit`` to ``test.canonical_unit``.

        Returns ``(converted, status)`` where status is one of ``converted``,
        ``identity``, ``unknown_unit``, ``dimension_mismatch``. The caller
        decides what to do about a mismatch - this function never guesses.
        """
        src = self.unit_spec(from_unit)
        if src is None:
            return None, "unknown_unit"
        if src.dimension != test.dimension:
            return None, "dimension_mismatch"
        dst = self.unit_spec(test.canonical_unit)
        if dst is None:                                     # pragma: no cover
            return None, "unknown_unit"
        base = (value - src.offset) * src.factor
        out = base / dst.factor + dst.offset
        status = "identity" if normalise_unit(from_unit) == normalise_unit(test.canonical_unit) else "converted"
        return out, status


# ---------------------------------------------------------------------------
# Clinic profiles
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ClinicProfile:
    clinic_id: str
    schema_version: str
    priority: int
    match: dict[str, Any]
    envelope: dict[str, Any]
    documents: dict[str, Any]
    date_formats: list[str]
    dedup: dict[str, Any]
    quality_expectations: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    def doc_spec(self, classifier: str) -> dict[str, Any] | None:
        return self.documents.get(classifier)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; ``override`` wins. Lists replace, they do not
    append - appending would make it impossible for a profile to *shorten* an
    inherited list, and silently growing lists are hard to reason about."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """The loaded configuration set."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.dir = Path(config_dir or os.environ.get("VERITAS_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"config directory not found: {self.dir}")

        self.pipeline: dict[str, Any] = self._load("pipeline.yaml")
        self.catalog = Catalog(self._load("test_catalog.yaml"), self._load("units.yaml"))
        self.medicines: dict[str, Any] = self._load("medicines.yaml")
        self.profiles: list[ClinicProfile] = self._load_profiles()
        self.fingerprint = self._fingerprint()

    # -- loading ---------------------------------------------------------
    def _load(self, name: str) -> dict[str, Any]:
        path = self.dir / name
        if not path.is_file():
            raise FileNotFoundError(f"missing config file: {path}")
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _load_profiles(self) -> list[ClinicProfile]:
        clinic_dir = self.dir / "clinics"
        raws: dict[str, dict[str, Any]] = {}
        for path in sorted(clinic_dir.glob("*.yaml")):
            with path.open("r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
            if "clinic_id" not in doc:
                raise ValueError(f"{path} has no clinic_id")
            raws[doc["clinic_id"]] = doc

        resolved: list[ClinicProfile] = []
        for clinic_id in raws:
            merged = self._resolve_inheritance(clinic_id, raws, set())
            resolved.append(
                ClinicProfile(
                    clinic_id=merged["clinic_id"],
                    schema_version=str(merged.get("schema_version", "unversioned")),
                    priority=int(merged.get("priority", 100)),
                    match=merged.get("match") or {},
                    envelope=merged.get("envelope") or {},
                    documents=merged.get("documents") or {},
                    date_formats=list(merged.get("date_formats") or []),
                    dedup=merged.get("dedup") or {},
                    quality_expectations=merged.get("quality_expectations") or {},
                    raw=merged,
                )
            )
        # Most specific first: lower priority number, then more match keys.
        resolved.sort(key=lambda p: (p.priority, -len(p.match)))
        return resolved

    def _resolve_inheritance(
        self, clinic_id: str, raws: dict[str, dict[str, Any]], seen: set[str]
    ) -> dict[str, Any]:
        if clinic_id in seen:
            raise ValueError(f"circular profile inheritance at {clinic_id!r}")
        seen.add(clinic_id)
        doc = raws[clinic_id]
        parent_id = doc.get("inherits")
        if not parent_id:
            return copy.deepcopy(doc)
        if parent_id not in raws:
            raise ValueError(f"{clinic_id!r} inherits unknown profile {parent_id!r}")
        parent = self._resolve_inheritance(parent_id, raws, seen)
        merged = _deep_merge(parent, doc)
        # Identity must come from the child, never the parent.
        merged["clinic_id"] = doc["clinic_id"]
        merged["schema_version"] = doc.get("schema_version", "unversioned")
        merged["match"] = doc.get("match", {})
        merged["priority"] = doc.get("priority", 100)
        return merged

    # -- selection -------------------------------------------------------
    def profile_for(self, meta: dict[str, Any]) -> ClinicProfile:
        """Pick the profile whose ``match`` block is satisfied by the file's
        envelope metadata. ``_default`` matches everything, so this never
        raises - an unknown source is processed, not rejected (FR-1.3)."""
        for profile in self.profiles:
            if all(str(meta.get(k, "")).strip() == str(v) for k, v in profile.match.items()):
                return profile
        return self.profiles[-1]

    # -- provenance ------------------------------------------------------
    def _fingerprint(self) -> str:
        """Stable hash of every config file, recorded on each run."""
        h = hashlib.sha256()
        for path in sorted(self.dir.rglob("*.yaml")):
            h.update(path.relative_to(self.dir).as_posix().encode())
            h.update(path.read_bytes())
        return h.hexdigest()[:16]

    # -- convenience -----------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.pipeline.get(name) or {}

    def describe(self) -> str:
        return json.dumps(
            {
                "config_dir": str(self.dir),
                "fingerprint": self.fingerprint,
                "canonical_tests": len(self.catalog.tests),
                "aliases_exact": len(self.catalog.by_alias_exact),
                "unit_spellings": len(self.catalog.units),
                "profiles": [p.clinic_id for p in self.profiles],
                "medicine_brands": len(self.medicines.get("brands") or []),
            },
            indent=2,
        )


_CACHED: Config | None = None


def load_config(config_dir: str | Path | None = None, *, reload: bool = False) -> Config:
    """Process-wide config, loaded once.

    Hot reload in the Cloud Run deployment is a new revision rather than an
    in-process watch: config changes are reviewed, versioned artefacts, and a
    revision roll gives a clean before/after boundary in the run ledger.
    """
    global _CACHED
    if _CACHED is None or reload or config_dir is not None:
        cfg = Config(config_dir)
        if config_dir is None:
            _CACHED = cfg
        return cfg
    return _CACHED
