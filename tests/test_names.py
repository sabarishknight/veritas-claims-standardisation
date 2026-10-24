"""Test-name resolution (FR-2.1)."""

from __future__ import annotations

import unittest

from veritas.models import Method
from veritas.standardise.names import NameResolver

from .support import catalog, config


def resolver(**overrides) -> NameResolver:
    cfg = config().section("resolver")
    return NameResolver(
        catalog(),
        accept_threshold=overrides.get("accept", cfg["fuzzy_accept_threshold"]),
        review_threshold=overrides.get("review", cfg["fuzzy_review_threshold"]),
        affix_tolerance=overrides.get("affix", cfg["affix_tolerance"]),
    )


class ExactAndAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r = resolver()

    def test_canonical_name_resolves_exactly(self) -> None:
        result = self.r.resolve("HAEMOGLOBIN")
        self.assertEqual(result.canonical, "HAEMOGLOBIN")
        self.assertIs(result.method, Method.EXACT)
        self.assertEqual(result.confidence, 1.0)

    def test_known_alias_resolves_with_full_confidence(self) -> None:
        for alias in ("Hb", "HGB", "Hemoglobin"):
            with self.subTest(alias=alias):
                result = self.r.resolve(alias)
                self.assertEqual(result.canonical, "HAEMOGLOBIN")
                self.assertEqual(result.confidence, 1.0)

    def test_specimen_qualifier_is_ignored(self) -> None:
        for spelling in ("CREATININE, SERUM", "Serum Creatinine"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.r.resolve(spelling).canonical, "SERUM CREATININE")


class OcrCropTests(unittest.TestCase):
    """The dominant failure mode in the corpus: a character lost off an edge."""

    def setUp(self) -> None:
        self.r = resolver()

    def test_left_cropped_names_resolve_via_the_affix_route(self) -> None:
        cases = {
            "aemoglobin": "HAEMOGLOBIN",
            "atelet Count": "PLATELET COUNT",
            "eutrophils": "NEUTROPHILS PERCENT",
            "mphocytes": "LYMPHOCYTES PERCENT",
            "sinophils": "EOSINOPHILS PERCENT",
            "onocytes": "MONOCYTES PERCENT",
            "sophils": "BASOPHILS PERCENT",
            "ematocrit HCT": "HEMATOCRIT",
            "DW-CV": "RDW-CV",
        }
        for spelling, expected in cases.items():
            with self.subTest(spelling=spelling):
                result = self.r.resolve(spelling, "%")
                self.assertEqual(result.canonical, expected)
                self.assertGreaterEqual(result.confidence, 0.86)

    def test_affix_route_is_reported_distinctly(self) -> None:
        # Reported as its own method so a clinic's scanner degrading becomes
        # measurable rather than invisible.
        self.assertIs(self.r.resolve("aemoglobin").method, Method.FUZZY_AFFIX)


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r = resolver()

    def test_non_tests_are_identified_not_matched(self) -> None:
        cases = {
            "test_name": "header_row_leaked",
            "Comment": "narrative_comment",
            "USG WHOLE ABDOMEN": "imaging_narrative",
            "COMPLETE BLOOD COUNT (CBC) , WHOLE BLOOD EDTA": "panel_heading",
            "RBC INDICES": "panel_heading",
        }
        for spelling, reason in cases.items():
            with self.subTest(spelling=spelling):
                result = self.r.resolve(spelling)
                self.assertIsNone(result.canonical)
                self.assertIs(result.method, Method.NON_TEST)
                self.assertEqual(result.note, reason)

    def test_short_fragments_are_not_guessed(self) -> None:
        for fragment in ("ET", "LCR", "x"):
            with self.subTest(fragment=fragment):
                self.assertIsNone(self.r.resolve(fragment).canonical)

    def test_unrelated_text_does_not_match(self) -> None:
        self.assertIsNone(self.r.resolve("lac/cmm").canonical)
        self.assertIsNone(self.r.resolve("zzzz quuux widget").canonical)

    def test_close_tie_between_different_tests_is_refused(self) -> None:
        """A coin toss must never write a clinical value."""
        r = resolver(accept=0.1, review=0.1)      # force everything into fuzzy
        result = r.resolve("SALMONELLA TYPHI O` , SERUM")
        self.assertIsNone(result.canonical)
        self.assertIn("ambiguous", result.note)


class UnitDisambiguationTests(unittest.TestCase):
    """The same name is two different tests depending on the unit."""

    def setUp(self) -> None:
        self.r = resolver()

    def test_percentage_versus_absolute_count(self) -> None:
        self.assertEqual(
            self.r.resolve("NEUTROPHILS", "%").canonical, "NEUTROPHILS PERCENT")
        self.assertEqual(
            self.r.resolve("NEUTROPHILS", "cells/cu.mm").canonical,
            "ABSOLUTE NEUTROPHIL COUNT")

    def test_without_a_unit_the_common_form_wins(self) -> None:
        result = self.r.resolve("NEUTROPHILS", "")
        self.assertEqual(result.canonical, "NEUTROPHILS PERCENT")
        self.assertIn("primary", result.note)

    def test_disambiguation_also_applies_to_cropped_names(self) -> None:
        self.assertEqual(
            self.r.resolve("eutrophils", "cells/cu.mm").canonical,
            "ABSOLUTE NEUTROPHIL COUNT")


class LearnedAliasTests(unittest.TestCase):
    def test_promotion_makes_a_spelling_free_forever(self) -> None:
        r = resolver()
        self.assertIsNone(r.resolve("zzqq marker").canonical)
        r.add_learned_alias("zzqq marker", "PLATELET COUNT")
        result = r.resolve("zzqq marker")
        self.assertEqual(result.canonical, "PLATELET COUNT")
        self.assertIs(result.method, Method.LEARNED_ALIAS)

    def test_cannot_learn_an_alias_to_an_unknown_test(self) -> None:
        with self.assertRaises(ValueError):
            resolver().add_learned_alias("x", "NOT A REAL TEST")


class DeterminismTests(unittest.TestCase):
    def test_same_input_always_gives_the_same_answer(self) -> None:
        a, b = resolver(), resolver()
        for spelling in ("aemoglobin", "Hb", "NEUTROPHILS", "lac/cmm", "Comment"):
            with self.subTest(spelling=spelling):
                self.assertEqual(a.resolve(spelling, "%").canonical,
                                 b.resolve(spelling, "%").canonical)


if __name__ == "__main__":
    unittest.main()
