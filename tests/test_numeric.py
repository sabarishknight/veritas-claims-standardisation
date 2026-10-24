"""Result-cell parsing (FR-2.3)."""

from __future__ import annotations

import unittest

from veritas.models import Flag
from veritas.standardise.numeric import (
    extract_embedded_values,
    parse_number,
    parse_result,
    round_like,
)

from .support import config, test

GARBAGE = tuple(config().section("validation")["ocr_garbage_tokens"])
MARKERS = {"H": "high", "L": "low", "*": "flagged"}


def parse(raw, **kw):
    return parse_result(raw, garbage_tokens=GARBAGE, prefix_markers=MARKERS, **kw)


class PlainNumbersTests(unittest.TestCase):
    def test_integers_decimals_and_zero_padding(self) -> None:
        for raw, expected in (("9700", 9700.0), ("5.45", 5.45), ("04", 4.0),
                              ("00", 0.0), ("0.90", 0.9)):
            with self.subTest(raw=raw):
                self.assertEqual(parse(raw).value, expected)

    def test_indian_thousands_comma_is_not_a_decimal(self) -> None:
        result = parse("4,290 cells/cu.mm")
        self.assertEqual(result.value, 4290.0)
        self.assertEqual(result.inline_unit, "cells/cu.mm")
        self.assertNotIn(Flag.MULTI_VALUE_PACKED, result.flags)

    def test_inline_unit_is_captured_not_discarded(self) -> None:
        result = parse("13.7 g/dl")
        self.assertEqual(result.value, 13.7)
        self.assertEqual(result.inline_unit, "g/dl")

    def test_operator_qualified_values_are_usable(self) -> None:
        result = parse("<0.5")
        self.assertEqual(result.value, 0.5)
        self.assertEqual(result.text, "<0.5")


class SourceAnnotationTests(unittest.TestCase):
    def test_high_low_prefix_is_stripped_and_kept_as_evidence(self) -> None:
        for raw, marker, value in (("H 0.7 %", "high", 0.7), ("L 23.5 %", "low", 23.5)):
            with self.subTest(raw=raw):
                result = parse(raw)
                self.assertEqual(result.value, value)
                self.assertEqual(result.prefix_marker, marker)
                self.assertIn(Flag.RESULT_PREFIX_MARKER, result.flags)


class RefusalTests(unittest.TestCase):
    """Cases where inventing a number would be worse than returning none."""

    def test_result_repeating_the_range_is_not_a_value(self) -> None:
        result = parse("1.5-4.5", range_original="1.5-4.5")
        self.assertIsNone(result.value)
        self.assertIn(Flag.RESULT_EQUALS_RANGE, result.flags)

    def test_ocr_debris_is_recognised(self) -> None:
        result = parse("OTE")
        self.assertIsNone(result.value)
        self.assertIn(Flag.OCR_GARBAGE_TOKEN, result.flags)

    def test_a_whole_panel_in_one_cell_is_not_split(self) -> None:
        for raw in ("12.0 g/dL, 36.10, 13-17, 40-50, 85.5 fL",
                    "Neutrophils: 2230.8 cells/cu.mm, Lymphocytes: 1716 cells/cu.mm"):
            with self.subTest(raw=raw[:30]):
                result = parse(raw)
                self.assertIsNone(result.value)
                self.assertIn(Flag.MULTI_VALUE_PACKED, result.flags)

    def test_narrative_is_kept_as_text(self) -> None:
        result = parse("NO GROWTH AFTER 48 HOURS OF INCUBATION AT 37 DEGREES")
        self.assertIsNone(result.value)
        self.assertIn(Flag.RESULT_NON_NUMERIC, result.flags)

    def test_an_interval_where_a_value_belongs_is_refused(self) -> None:
        result = parse("0-2 Hyaline Cast")
        self.assertIsNone(result.value)

    def test_empty_is_missing_not_zero(self) -> None:
        result = parse("")
        self.assertIsNone(result.value)
        self.assertIn(Flag.RESULT_MISSING, result.flags)


class QualitativeTests(unittest.TestCase):
    def test_qualitative_values_are_canonicalised(self) -> None:
        result = parse("POSITIVE", test=test("WIDAL TEST"))
        self.assertIsNone(result.value)
        self.assertEqual(result.text, "POSITIVE")
        self.assertNotIn(Flag.RESULT_NON_NUMERIC, result.flags)

    def test_qualitative_where_numeric_expected_is_flagged(self) -> None:
        result = parse("NEGATIVE", test=test("C-REACTIVE PROTEIN"))
        self.assertIn(Flag.RESULT_NON_NUMERIC, result.flags)


class CompoundTests(unittest.TestCase):
    """A slash means "separator" only where the catalogue says so."""

    def test_blood_pressure_splits_into_two_measurements(self) -> None:
        result = parse("100/60 mmHg", test=test("BLOOD PRESSURE"))
        self.assertIn(Flag.COMPOUND_SPLIT, result.flags)
        self.assertEqual(
            result.components,
            [("BLOOD PRESSURE SYSTOLIC", 100.0), ("BLOOD PRESSURE DIASTOLIC", 60.0)])

    def test_a_rate_is_not_split_on_its_unit_slash(self) -> None:
        result = parse("114/min", test=test("PULSE RATE"))
        self.assertEqual(result.value, 114.0)
        self.assertEqual(result.components, [])


class PrecisionTests(unittest.TestCase):
    def test_output_precision_mirrors_the_source(self) -> None:
        # A lab reports precision deliberately; silently changing it changes
        # what an analyst believes about the measurement.
        self.assertEqual(round_like(37.00000000296, "98.6"), 37.0)
        self.assertEqual(round_like(0.1200000001, "120"), 0.12)

    def test_parse_number_rejects_non_numbers(self) -> None:
        for raw in ("", "abc", "NEGATIVE", None):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_number(raw or ""))


class EmbeddedValueTests(unittest.TestCase):
    """Values a clinic buried inside the test name."""

    def test_separator_form(self) -> None:
        found = dict(extract_embedded_values("LFT ( SGOT - 38, SGPT -14, ALP - 127)"))
        self.assertEqual(found, {"SGOT": 38.0, "SGPT": 14.0, "ALP": 127.0})

    def test_parenthesised_form(self) -> None:
        self.assertEqual(extract_embedded_values("Na + (127)"), [("Na", 127.0)])

    def test_a_plain_test_name_yields_nothing(self) -> None:
        self.assertEqual(extract_embedded_values("HAEMOGLOBIN"), [])


if __name__ == "__main__":
    unittest.main()
