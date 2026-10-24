"""Tiered test-name resolution (FR-2.1).

Five tiers, cheapest and most certain first. A name descends only when the
tier above cannot answer, and every answer records *how* it was reached:

    T0  exact / alias        confidence 1.00   method=exact | alias
    T1  normalised string    confidence 0.97   method=normalised
        token-sorted, specimen qualifiers dropped
                             confidence 0.93   method=normalised
    T2  fuzzy                confidence=score  method=fuzzy | fuzzy_affix
    T3  learned alias        confidence 0.99   method=learned_alias
        (an LLM or human decision promoted into the dictionary)
    T4  unresolved           confidence 0.00   method=unmapped -> review queue

Writing the method and the confidence to every output row
(``normalization_method``, ``normalization_confidence`` - both present in the
target schema) is what makes the mapping auditable. An analyst who distrusts a
number can see it was a 0.88 fuzzy match with two near-ties, and a data
scientist can exclude everything below 1.0 from a model without asking anyone.

Why a hand-written matcher
--------------------------
The dominant failure in this corpus is an OCR left-crop: ``HAEMOGLOBIN`` loses
its first character and arrives as ``aemoglobin``; ``Platelet Count`` arrives
as ``atelet Count``; ``Neutrophils`` as ``eutrophils``. A general-purpose
fuzzy library scores those on edit distance alone and has no notion that
*losing an affix* is a far more likely accident than *changing a middle
character*. So the matcher adds an explicit affix-tolerant comparison, and
reports it as a distinct method so the pattern is measurable per source - a
clinic whose affix-match rate is climbing has a scanner problem, and that is a
conversation worth having before the data gets worse.

Performance
-----------
Candidate generation is a trigram inverted index, so an unresolved name is
compared against ~20 plausible aliases rather than all 322. The expensive path
only runs on names the dictionaries miss, which is a small and *shrinking*
fraction: every LLM or human decision is promoted into T0/T3, so the tail
decays toward zero as a source matures (NFR-4.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..config import Catalog, normalise_name, normalise_name_strict
from ..models import Method, NameResolution

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_FUZZY_LENGTH = 4          # below this, a fuzzy hit is a coin toss
_AMBIGUITY_MARGIN = 0.02       # two different tests this close = ambiguous
_SHORTLIST = 25                # candidates scored precisely per unresolved name
_TRIGRAM_PAD = "  "


def _trigrams(text: str) -> set[str]:
    s = _TRIGRAM_PAD + text + _TRIGRAM_PAD
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text))


def _token_score(query: frozenset[str], candidate: frozenset[str]) -> float:
    """Set similarity with a subset bonus.

    ``ALBUMIN`` vs ``ALBUMIN, SERUM`` is a subset, not a near-miss: the source
    simply said less. Plain Jaccard would score that 0.5 and reject it.
    """
    if not query or not candidate:
        return 0.0
    inter = len(query & candidate)
    if not inter:
        return 0.0
    if query <= candidate or candidate <= query:
        return 0.90 + 0.10 * (inter / max(len(query), len(candidate)))
    return inter / len(query | candidate)


def _affix_score(query: str, candidate: str, tolerance: int) -> float:
    """Score for the hypothesis "characters were lost from an end".

    Scales with how much survived, so a one-character crop of a long name
    (``aemoglobin``) scores near-certain while a short fragment of a long name
    does not clear the acceptance threshold on this route alone.
    """
    if not query or not candidate or query == candidate:
        return 0.0
    short, long_ = (query, candidate) if len(query) <= len(candidate) else (candidate, query)
    missing = len(long_) - len(short)
    if missing == 0 or missing > tolerance:
        return 0.0
    if not (long_.endswith(short) or long_.startswith(short)):
        return 0.0
    kept = len(short) / len(long_)
    return min(0.99, 0.80 + 0.19 * kept)


@dataclass(slots=True)
class ResolverStats:
    exact: int = 0
    alias: int = 0
    normalised: int = 0
    fuzzy: int = 0
    fuzzy_affix: int = 0
    learned: int = 0
    llm: int = 0
    unmapped: int = 0
    non_test: int = 0
    ambiguous: int = 0

    def record(self, method: Method) -> None:
        attr = {
            Method.EXACT: "exact",
            Method.ALIAS: "alias",
            Method.NORMALISED: "normalised",
            Method.FUZZY: "fuzzy",
            Method.FUZZY_AFFIX: "fuzzy_affix",
            Method.LEARNED_ALIAS: "learned",
            Method.LLM: "llm",
            Method.UNMAPPED: "unmapped",
            Method.NON_TEST: "non_test",
        }.get(method)
        if attr:
            setattr(self, attr, getattr(self, attr) + 1)

    def as_dict(self) -> dict[str, int]:
        return {
            "exact": self.exact, "alias": self.alias, "normalised": self.normalised,
            "fuzzy": self.fuzzy, "fuzzy_affix": self.fuzzy_affix,
            "learned_alias": self.learned, "llm": self.llm,
            "unmapped": self.unmapped, "non_test": self.non_test,
            "ambiguous": self.ambiguous,
        }

    @property
    def resolved_rate(self) -> float:
        total = (self.exact + self.alias + self.normalised + self.fuzzy
                 + self.fuzzy_affix + self.learned + self.llm + self.unmapped)
        return 0.0 if not total else 1.0 - (self.unmapped / total)


class NameResolver:
    """Resolves a source test name to a canonical test.

    Stateless with respect to input: the same name always resolves the same
    way for a given catalogue and learned-alias set, which is what makes the
    pipeline reproducible and the output diffable between runs.
    """

    def __init__(
        self,
        catalog: Catalog,
        *,
        accept_threshold: float = 0.86,
        review_threshold: float = 0.70,
        affix_tolerance: int = 3,
        max_candidates: int = 5,
        learned_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.accept_threshold = accept_threshold
        self.review_threshold = review_threshold
        self.affix_tolerance = affix_tolerance
        self.max_candidates = max_candidates
        self.learned: dict[str, str] = dict(learned_aliases or {})
        self.stats = ResolverStats()
        self._cache: dict[str, NameResolution] = {}
        self._build_index()

    # -- index ----------------------------------------------------------
    def _build_index(self) -> None:
        """Trigram inverted index over every alias spelling."""
        self._alias_norms: list[tuple[str, str, frozenset[str]]] = []
        self._trigram_index: dict[str, list[int]] = {}
        # Built from the *candidate* index so that both forms of a shared alias
        # ("NEUTROPHILS" as a percentage and as an absolute count) are visible
        # to the fuzzy tier. Indexing only the primary would make a cropped
        # "eutrophils" with a count unit unreachable for the absolute form.
        for alias, canonicals in self.catalog.by_alias_candidates.items():
            norm = normalise_name(alias)
            if not norm:
                continue
            for canonical in canonicals:
                idx = len(self._alias_norms)
                self._alias_norms.append((norm, canonical, _tokens(norm)))
                for tri in _trigrams(norm):
                    self._trigram_index.setdefault(tri, []).append(idx)

    def add_learned_alias(self, source_name: str, canonical: str) -> None:
        """Promote a decision into the dictionary (NFR-4.1).

        This is the mechanism behind "98% of variants resolved within 30 days":
        each novel spelling is reasoned about once - by the fuzzy matcher, an
        LLM, or a human in the review queue - and then it is free forever.
        """
        if canonical not in self.catalog.tests:
            raise ValueError(f"cannot learn alias to unknown test {canonical!r}")
        self.learned[normalise_name(source_name)] = canonical
        self._cache.pop(source_name, None)

    # -- resolution -----------------------------------------------------
    def resolve(self, raw_name: str, unit_hint: str | None = None) -> NameResolution:
        """Resolve a name, optionally using the row's unit to disambiguate.

        ``unit_hint`` matters for names that two tests legitimately share:
        "NEUTROPHILS" in ``%`` is a proportion, "NEUTROPHILS" in
        ``cells/cu.mm`` is an absolute count. They have different reference
        ranges, so choosing between them by unit is not a nicety - without it
        one of the two is always classified against the wrong band.
        """
        cache_key = raw_name if unit_hint is None else f"{raw_name}\x00{unit_hint}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            self.stats.record(cached.method)
            return cached
        result = self._resolve_uncached(raw_name, unit_hint)
        if len(self._cache) < 50_000:      # bounded: one clinic's long tail
            self._cache[cache_key] = result
        self.stats.record(result.method)
        return result

    def _resolve_uncached(self, raw_name: str, unit_hint: str | None = None) -> NameResolution:
        name = (raw_name or "").strip()
        if not name:
            return NameResolution(None, raw_name, Method.NON_TEST, 0.0,
                                  note="empty test name")

        # Rows that are not tests at all - header leakage, section headings,
        # narrative. Checked before resolution so they never reach the review
        # queue as "unmapped tests": an analyst cannot action a page footer.
        reason = self.catalog.match_non_test(name)
        if reason:
            return NameResolution(None, raw_name, Method.NON_TEST, 0.0, note=reason)

        # T0 exact. Where an alias is claimed by more than one test, the unit
        # decides; where it cannot, the catalogue's primary wins.
        candidates = self.catalog.candidates_for(name)
        if len(candidates) > 1:
            chosen = self.catalog.disambiguate_by_unit(candidates, unit_hint)
            if chosen:
                return NameResolution(
                    chosen, raw_name,
                    Method.EXACT if name == chosen else Method.ALIAS, 1.0,
                    [(c, 1.0) for c in candidates],
                    note=f"disambiguated by unit {unit_hint!r}",
                )
        canonical = self.catalog.by_alias_exact.get(name)
        if canonical:
            method = Method.EXACT if name == canonical else Method.ALIAS
            return NameResolution(
                canonical, raw_name, method, 1.0,
                [(c, 1.0) for c in candidates] if len(candidates) > 1 else [],
                note=("unit did not disambiguate; used catalogue primary"
                      if len(candidates) > 1 else ""),
            )

        norm = normalise_name(name)

        # T3 learned, checked before the generic normaliser so a promoted
        # decision always wins over a weaker inference.
        learned = self.learned.get(norm)
        if learned:
            return NameResolution(learned, raw_name, Method.LEARNED_ALIAS, 0.99,
                                  note="promoted alias")

        # T1 normalised
        norm_candidates = self.catalog.candidates_for(norm)
        if len(norm_candidates) > 1:
            chosen = self.catalog.disambiguate_by_unit(norm_candidates, unit_hint)
            if chosen:
                return NameResolution(
                    chosen, raw_name, Method.NORMALISED, 0.97,
                    [(c, 0.97) for c in norm_candidates],
                    note=f"disambiguated by unit {unit_hint!r}",
                )
        canonical = self.catalog.by_alias_normalised.get(norm)
        if canonical:
            return NameResolution(canonical, raw_name, Method.NORMALISED, 0.97)
        canonical = self.catalog.by_alias_strict.get(normalise_name_strict(name))
        if canonical:
            return NameResolution(canonical, raw_name, Method.NORMALISED, 0.93,
                                  note="token-sorted match")

        # T2 fuzzy
        if len(norm) < _MIN_FUZZY_LENGTH:
            return NameResolution(None, raw_name, Method.UNMAPPED, 0.0,
                                  note=f"too short to fuzzy match ({len(norm)} chars)")
        ranked = self._fuzzy_rank(norm, unit_hint)
        if not ranked:
            return NameResolution(None, raw_name, Method.UNMAPPED, 0.0,
                                  note="no candidate shared a trigram")

        best_score, best_canonical, best_method, best_alias = ranked[0]
        candidates = [(c, round(s, 4)) for s, c, _, _ in ranked[: self.max_candidates]]

        # Ambiguity guard. Two *different* tests within a hair of each other is
        # not a 0.9 match, it is a coin toss - and a coin toss that writes a
        # clinical value is unacceptable. Demote to review.
        #
        # Only meaningful above the review threshold: below it the match is
        # rejected anyway, and reporting "ambiguous" there would hide the more
        # useful reason ("nothing came close").
        runner_up = next((r for r in ranked[1:] if r[1] != best_canonical), None)
        if (best_score >= self.review_threshold and runner_up
                and (best_score - runner_up[0]) < _AMBIGUITY_MARGIN):
            tied = [r[1] for r in ranked if abs(r[0] - best_score) < _AMBIGUITY_MARGIN]
            shared = self.catalog.candidates_for(best_alias)

            # Case 1: the tie is between two reporting forms of the same
            # measurement (percentage vs absolute count). The unit settles it.
            if len(shared) > 1 and all(t in shared for t in tied):
                chosen = self.catalog.disambiguate_by_unit(tied, unit_hint)
                if chosen:
                    return NameResolution(
                        chosen, raw_name, best_method, round(best_score, 4), candidates,
                        note=f"disambiguated by unit {unit_hint!r}",
                    )
                # No unit to go on. Default to catalogue order rather than
                # discarding the row: the two forms differ in reference range,
                # not in identity, and the primary form is the common one.
                primary = next((c for c in shared if c in tied), tied[0])
                return NameResolution(
                    primary, raw_name, best_method,
                    round(best_score * 0.9, 4), candidates,
                    note="reporting form ambiguous without a unit; used catalogue primary",
                )

            # Case 2: genuinely different tests, equally close. A coin toss
            # that writes a clinical value is not acceptable.
            self.stats.ambiguous += 1
            return NameResolution(
                None, raw_name, Method.UNMAPPED, 0.0, candidates,
                note=(f"ambiguous: {best_canonical} ({best_score:.3f}) vs "
                      f"{runner_up[1]} ({runner_up[0]:.3f})"),
            )

        if best_score >= self.review_threshold:
            return NameResolution(
                best_canonical, raw_name, best_method, round(best_score, 4), candidates,
                note=("" if best_score >= self.accept_threshold
                      else f"below accept threshold {self.accept_threshold}"),
            )
        return NameResolution(
            None, raw_name, Method.UNMAPPED, 0.0, candidates,
            note=f"best score {best_score:.3f} below review threshold {self.review_threshold}",
        )

    def _fuzzy_rank(
        self, norm: str, unit_hint: str | None = None
    ) -> list[tuple[float, str, Method, str]]:
        """Blocked candidate generation, then precise scoring.

        A unit hint does not change any score; it breaks ties. Two tests that
        share an alias score identically by construction, so without the hint
        the ambiguity guard would reject both - correct but unhelpful when the
        unit settles it unambiguously.
        """
        query_tris = _trigrams(norm)
        overlap: dict[int, int] = {}
        for tri in query_tris:
            for idx in self._trigram_index.get(tri, ()):
                overlap[idx] = overlap.get(idx, 0) + 1
        if not overlap:
            return []

        # The second sort key is load-bearing, not cosmetic. Many aliases tie
        # on trigram overlap, and without a stable tiebreak the shortlist cut
        # falls differently depending on set iteration order - which varies
        # with PYTHONHASHSEED. That made the same input resolve to different
        # tests in different processes: a silent reproducibility defect, since
        # both answers look perfectly reasonable on their own.
        #
        # `_alias_norms` is built in catalogue order over insertion-ordered
        # dicts, so its index is a deterministic tiebreak.
        shortlist = sorted(overlap.items(), key=lambda kv: (-kv[1], kv[0]))[:_SHORTLIST]

        query_tokens = _tokens(norm)
        scored: dict[str, tuple[float, Method, str]] = {}
        for idx, _ in shortlist:
            cand_norm, canonical, cand_tokens = self._alias_norms[idx]
            matcher = SequenceMatcher(None, norm, cand_norm)
            if matcher.real_quick_ratio() < self.review_threshold:
                continue                      # cheap rejection before the O(n*m)
            seq = matcher.ratio()
            tok = _token_score(query_tokens, cand_tokens)
            affix = _affix_score(norm, cand_norm, self.affix_tolerance)
            best = max(seq, tok, affix)
            method = Method.FUZZY_AFFIX if affix == best and affix > 0 else Method.FUZZY
            prior = scored.get(canonical)
            if prior is None or best > prior[0]:
                scored[canonical] = (best, method, cand_norm)
        hint_spec = self.catalog.unit_spec(unit_hint or "") if unit_hint else None
        hint_dimension = hint_spec.dimension if hint_spec else None

        def sort_key(item: tuple[float, str, Method, str]) -> tuple:
            score, canonical, _, _alias = item
            test = self.catalog.tests.get(canonical)
            dimension_match = bool(
                hint_dimension and test and test.dimension == hint_dimension
            )
            return (-score, not dimension_match, canonical)

        return sorted(
            (
                (score, canonical, method, alias)
                for canonical, (score, method, alias) in scored.items()
            ),
            key=sort_key,
        )

    # -- bulk helpers ---------------------------------------------------
    def unresolved_names(self, names: Iterable[str]) -> list[str]:
        """Names that will reach T4, in first-seen order.

        The LLM tier consumes this: it is the *only* input GenAI ever sees, and
        keeping it a distinct, inspectable list is what bounds the cost.
        """
        out: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = normalise_name(name)
            if not key or key in seen:
                continue
            seen.add(key)
            if self.resolve(name).canonical is None and \
               self.catalog.match_non_test(name) is None:
                out.append(name)
        return out
