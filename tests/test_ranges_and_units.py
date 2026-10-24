"""Reference-range parsing and unit harmonisation (FR-2.4, FR-3.1)."""

from __future__ import annotations

import unittest

from veritas.models import Flag
from veritas.standardise.numeric import parse_result
from veritas.standardise.ranges import parse_range, range_matches_envelope
from veritas.validate.rules import harmonise, resolve_unit

from .support import catalog, config, test

TOLERANCE = config().section("coherence")["range_envelope_tolerance"]


class RangeGrammarTests(unittest.TestCase):
    def test_every_numeric_grammar_in_the_corpus(self) -> None:
        cases = {
            "4000-10000": (4000.0, 10000.0),
            "8.0 - 23.0": (8.0, 23.0),
            "01-04": (1.0, 4.0),                  # leading zeros
            "1.005 TO 1.030": (1.005, 1.03),      # worded separator
            "0.0 -< 1.0": (0.0, 1.0),             # mixed interval/operator
            "36.7 - 37.2ºF": (36.7, 37.2),   # unit glued on
            "<50": (None, 50.0),
            "> 90": (90.0, None),
            "150000-410000": (150000.0, 410000.0),
        }
        for raw, (low, high) in cases.items():
            with self.subTest(raw=raw):
                parsed = parse_range(raw)
                self.assertEqual((parsed.low, parsed.high), (low, high))

    def test_inverted_bounds_are_corrected(self) -> None:
        # Written the wrong way round, an interval would classify every value
        # as out of range.
        parsed = parse_range("17-13")
        self.assertEqual((parsed.low, parsed.high), (13.0, 17.0))

    def test_unreadable_ranges_are_preserved_and_flagged(self) -> None:
        for raw in ("Less than 1:80", "= 1 COI - Equivocal", "range",
                    "12-16, 35-45, 4.5-5.5", "1.31", "OTE"):
            with self.subTest(raw=raw):
                parsed = parse_range(raw)
                self.assertIsNone(parsed.low)
                self.assertIsNone(parsed.high)
                self.assertIn(Flag.RANGE_UNPARSEABLE, parsed.flags)
                self.assertEqual(parsed.text, raw)      # never discarded

    def test_a_bare_number_is_not_read_as_a_bound(self) -> None:
        # Usually the neighbouring column bleeding in.
        self.assertIsNone(parse_range("1.31").high)

    def test_missing_is_distinguished_from_unreadable(self) -> None:
        self.assertIn(Flag.RANGE_MISSING, parse_range("").flags)


class EnvelopePlausibilityTests(unittest.TestCase):
    """Could this reference range belong to this analyte at all?"""

    def check(self, canonical: str, raw: str) -> bool:
        return range_matches_envelope(parse_range(raw), test(canonical).envelope(), TOLERANCE)

    def test_a_wbc_range_is_implausible_for_haemoglobin(self) -> None:
        self.assertFalse(self.check("HAEMOGLOBIN", "4000-10000"))

    def test_an_rbc_range_is_implausible_for_a_wbc_count(self) -> None:
        self.assertFalse(self.check("WHITE BLOOD CELL COUNT", "4.50-5.50"))

    def test_correct_ranges_pass(self) -> None:
        for canonical, raw in (("HAEMOGLOBIN", "13-17"),
                               ("WHITE BLOOD CELL COUNT", "4000-10000"),
                               ("MEAN CORPUSCULAR VOLUME", "83-101"),
                               ("PLATELET COUNT", "150000-410000"),
                               ("SODIUM", "135-145"),
                               ("BILIRUBIN TOTAL", "0-1.2"),
                               ("ALANINE AMINOTRANSFERASE", "<50")):
            with self.subTest(canonical=canonical):
                self.assertTrue(self.check(canonical, raw))

    def test_labs_may_differ_without_being_wrong(self) -> None:
        # A lab using a somewhat wider band is normal, not a defect.
        self.assertTrue(self.check("HAEMOGLOBIN", "11-19"))


class UnitResolutionTests(unittest.TestCase):
    def test_known_spellings_map_to_the_canonical_unit(self) -> None:
        hb = test("HAEMOGLOBIN")
        for spelling in ("g/dL", "gm/dl", "gm/di", "G/DL"):
            with self.subTest(spelling=spelling):
                unit, flags, _ = resolve_unit(catalog(), spelling, hb, "Haemoglobin")
                self.assertEqual(unit, "g/dL")
                self.assertEqual(flags, [])

    def test_a_different_dimension_is_a_mismatch_not_a_conversion(self) -> None:
        unit, flags, note = resolve_unit(
            catalog(), "cell/cu.mm", test("HAEMOGLOBIN"), "aemoglobin")
        self.assertIsNone(unit)
        self.assertIn(Flag.UNIT_DIMENSION_MISMATCH, flags)
        self.assertIn("count_per_volume", note)

    def test_the_test_name_in_the_unit_column_invalidates_the_row(self) -> None:
        _, flags, _ = resolve_unit(
            catalog(), "atelet Count", test("PLATELET COUNT"), "atelet Count")
        self.assertIn(Flag.UNIT_EQUALS_TEST_NAME, flags)

    def test_known_non_units_are_named_as_such(self) -> None:
        for spelling in ("NIL", "unit", "Expertise. Empowering you."):
            with self.subTest(spelling=spelling):
                _, flags, _ = resolve_unit(catalog(), spelling, test("HAEMOGLOBIN"), "Hb")
                self.assertIn(Flag.UNIT_NOT_A_UNIT, flags)

    def test_an_empty_unit_is_missing_not_dimensionless(self) -> None:
        # Treating blank as dimensionless once produced 64 false findings in a
        # single sample file.
        self.assertIsNone(catalog().unit_spec(""))
        _, flags, _ = resolve_unit(catalog(), "", test("HAEMOGLOBIN"), "Hb")
        self.assertIn(Flag.UNIT_MISSING, flags)


class HarmonisationTests(unittest.TestCase):
    """Value and range must be converted together, before classification."""

    def harmonise(self, canonical: str, raw_result: str, raw_range: str, unit: str):
        t = test(canonical)
        result = parse_result(raw_result, test=t, range_original=raw_range)
        rng = parse_range(raw_range)
        return harmonise(catalog(), result, rng, unit, t, canonical)

    def test_affine_conversion(self) -> None:
        out = self.harmonise("BODY TEMPERATURE", "98.6", "", "degree F")
        self.assertEqual(out.value, 37.0)
        self.assertEqual(out.unit_canonical, "degC")

    def test_lakh_convention(self) -> None:
        out = self.harmonise("PLATELET COUNT", "1.90", "1.5-4.1", "lakhs/cumm")
        self.assertEqual(out.value, 190000.0)
        self.assertEqual(out.range_low, 150000.0)
        self.assertEqual(out.range_high, 410000.0)

    def test_the_range_is_converted_with_the_same_factor_as_the_value(self) -> None:
        # Converting one without the other misclassifies every row, and both
        # numbers still look reasonable on their own.
        out = self.harmonise("HAEMOGLOBIN", "120", "130-170", "mg/dL")
        self.assertEqual(out.value, 0.12)
        self.assertEqual((out.range_low, out.range_high), (0.13, 0.17))

    def test_cu_mm_versus_cu_cm(self) -> None:
        # The worked example named in FR-2.4.
        a = self.harmonise("RED BLOOD CELL COUNT", "4.74", "4.5-5.5", "mil/cu.mm")
        b = self.harmonise("RED BLOOD CELL COUNT", "4.74", "4.5-5.5", "mil/cu.cm")
        self.assertEqual(a.value, b.value)

    def test_every_spelling_of_one_quantity_converges(self) -> None:
        values = {
            self.harmonise("HAEMOGLOBIN", "13.7", "13-17", spelling).value
            for spelling in ("g/dL", "gm/dl", "gm/di")
        }
        self.assertEqual(values, {13.7})

    def test_mismatched_dimension_is_not_converted(self) -> None:
        out = self.harmonise("HAEMOGLOBIN", "9700", "4000-10000", "cell/cu.mm")
        self.assertIn(Flag.UNIT_DIMENSION_MISMATCH, out.flags)
        self.assertEqual(out.value, 9700.0)       # left untouched, not coerced

    def test_inline_unit_is_used_when_the_column_is_empty(self) -> None:
        out = self.harmonise("HAEMOGLOBIN", "13.7 g/dl", "", "")
        self.assertEqual(out.unit_canonical, "g/dL")
        self.assertNotIn(Flag.UNIT_MISSING, out.flags)


if __name__ == "__main__":
    unittest.main()
