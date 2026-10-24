"""Classification, outlier detection and the coherence engine (FR-3.x)."""

from __future__ import annotations

import json
import unittest

from veritas.models import Analytics, Flag, ParsedRange
from veritas.standardise.names import NameResolver
from veritas.standardise.numeric import parse_result
from veritas.standardise.ranges import parse_range
from veritas.validate.coherence import CoherenceEngine
from veritas.validate.rules import classify, harmonise, is_outlier

from .support import INCOMING, catalog, config, test


def classify_row(canonical: str | None, raw_result: str, raw_range: str,
                 unit: str, *, sex: str | None = None,
                 source_interpretation: str | None = None):
    t = test(canonical) if canonical else None
    result = parse_result(raw_result, test=t, range_original=raw_range)
    rng = parse_range(raw_range)
    out = harmonise(catalog(), result, rng, unit, t, canonical or "")
    result.value = out.value
    return classify(
        result,
        ParsedRange(low=out.range_low, high=out.range_high,
                    text=rng.text, original=rng.original, operator=rng.operator),
        t, sex=sex,
        existing_flags=tuple(result.flags) + tuple(rng.flags) + tuple(out.flags),
        source_interpretation=source_interpretation,
    )


class ThreeDistinctQuestionsTests(unittest.TestCase):
    """Readable? Possible? Normal? Collapsing these is the expensive mistake."""

    def test_normal_value_is_within_range(self) -> None:
        self.assertIs(classify_row("HAEMOGLOBIN", "14.1", "13-17", "g/dL").analytics,
                      Analytics.WITHIN_RANGE)

    def test_abnormal_values_are_above_or_below(self) -> None:
        self.assertIs(classify_row("HAEMOGLOBIN", "12.0", "13-17", "g/dL").analytics,
                      Analytics.BELOW_RANGE)
        self.assertIs(classify_row("HAEMOGLOBIN", "18.4", "13-17", "g/dL").analytics,
                      Analytics.ABOVE_RANGE)

    def test_impossible_values_are_outliers_not_merely_abnormal(self) -> None:
        # A haemoglobin of 0.1 is bad data; routing it to the clinical queue as
        # "below range" wastes a reviewer and hides a broken feed.
        for raw in ("0.1", "999"):
            with self.subTest(raw=raw):
                out = classify_row("HAEMOGLOBIN", raw, "13-17", "g/dL")
                self.assertIs(out.analytics, Analytics.OUTLIER)
                self.assertIn(Flag.OUTLIER_IMPLAUSIBLE, out.flags)

    def test_a_genuinely_critical_value_is_not_dismissed_as_an_outlier(self) -> None:
        out = classify_row("HAEMOGLOBIN", "4.1", "13-17", "g/dL")
        self.assertIs(out.analytics, Analytics.BELOW_RANGE)

    def test_unreadable_rows_are_invalid(self) -> None:
        for result_raw, range_raw, unit in (("1.5-4.5", "1.5-4.5", "lac/cmm"),
                                            ("OTE", "OTE", "%"),
                                            ("9700", "4000-10000", "cell/cu.mm")):
            with self.subTest(result=result_raw):
                self.assertIs(
                    classify_row("HAEMOGLOBIN", result_raw, range_raw, unit).analytics,
                    Analytics.INVALID)

    def test_qualitative_results_are_not_failures(self) -> None:
        out = classify_row("WIDAL TEST", "POSITIVE", "Less than 1:80", "")
        self.assertIs(out.analytics, Analytics.NOT_APPLICABLE)


class RangeSourceTests(unittest.TestCase):
    def test_the_labs_own_range_is_preferred(self) -> None:
        self.assertEqual(
            classify_row("SODIUM", "132", "130-150", "mmol/L").range_source, "source")

    def test_catalogue_range_is_the_fallback_and_is_declared(self) -> None:
        out = classify_row("SODIUM", "132", "", "mmol/L")
        self.assertEqual(out.range_source, "catalogue")
        self.assertIs(out.analytics, Analytics.BELOW_RANGE)
        self.assertIn("catalogue", out.note)

    def test_without_any_range_the_pipeline_abstains(self) -> None:
        # Asserting "Within Range" with nothing to compare against would be a
        # fabrication.
        out = classify_row(None, "42", "", "")
        self.assertIs(out.analytics, Analytics.NOT_APPLICABLE)


class SourceVerdictTests(unittest.TestCase):
    """The lab's own interpretation is evidence, never the answer."""

    def test_disagreement_is_recorded(self) -> None:
        out = classify_row("RDW-CV", "14.2", "11.6-14.0", "%",
                           source_interpretation="normal")
        self.assertIs(out.analytics, Analytics.ABOVE_RANGE)
        self.assertIn(Flag.SOURCE_INTERPRETATION_DISAGREES, out.flags)

    def test_agreement_is_not_flagged(self) -> None:
        out = classify_row("SODIUM", "140", "135-145", "mmol/L",
                           source_interpretation="normal")
        self.assertNotIn(Flag.SOURCE_INTERPRETATION_DISAGREES, out.flags)

    def test_the_source_verdict_never_overrides_the_arithmetic(self) -> None:
        out = classify_row("ALKALINE PHOSPHATASE", "124", "38-126", "U/L",
                           source_interpretation="high")
        self.assertIs(out.analytics, Analytics.WITHIN_RANGE)


class OutlierEnvelopeTests(unittest.TestCase):
    def test_envelope_is_declared_not_inferred(self) -> None:
        # A per-batch statistical rule would make the same value an outlier on
        # a quiet day and normal on a busy one.
        self.assertTrue(is_outlier(0.05, test("HAEMOGLOBIN")))
        self.assertFalse(is_outlier(14.0, test("HAEMOGLOBIN")))

    def test_no_test_means_no_outlier_claim(self) -> None:
        self.assertFalse(is_outlier(99999.0, None))


class CoherenceRowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CoherenceEngine(catalog(), config().section("coherence"))

    def test_wrong_dimension_is_incoherent(self) -> None:
        verdict = self.engine.check_row(
            0, test("HAEMOGLOBIN"), "cell/cu.mm", parse_range("4000-10000"))
        self.assertTrue(verdict.evaluable)
        self.assertFalse(verdict.coherent)
        self.assertIn(Flag.UNIT_DIMENSION_MISMATCH, verdict.flags)

    def test_implausible_range_is_incoherent_even_with_a_valid_dimension(self) -> None:
        verdict = self.engine.check_row(
            0, test("WHITE BLOOD CELL COUNT"), "mil/cu.mm", parse_range("4.50-5.50"))
        self.assertFalse(verdict.coherent)
        self.assertIn(Flag.RANGE_IMPLAUSIBLE_FOR_TEST, verdict.flags)

    def test_a_correct_row_is_coherent(self) -> None:
        verdict = self.engine.check_row(
            0, test("HAEMOGLOBIN"), "g/dL", parse_range("13-17"))
        self.assertTrue(verdict.coherent)

    def test_a_row_with_no_evidence_is_not_judged(self) -> None:
        # No recognised unit and no parsed range: judging it would manufacture
        # a finding.
        verdict = self.engine.check_row(0, test("HAEMOGLOBIN"), "", parse_range(""))
        self.assertFalse(verdict.evaluable)
        self.assertTrue(verdict.coherent)


class MisalignmentTests(unittest.TestCase):
    """The defect that motivates the whole coherence engine."""

    def setUp(self) -> None:
        self.engine = CoherenceEngine(catalog(), config().section("coherence"))
        self.resolver = NameResolver(
            catalog(), accept_threshold=0.86, review_threshold=0.70, affix_tolerance=3)

    def analyse(self, rows):
        tests = [catalog().tests.get(
            self.resolver.resolve(r["test_name"], r.get("unit")).canonical) for r in rows]
        units = [r.get("unit", "") for r in rows]
        ranges = [parse_range(r.get("range", "")) for r in rows]
        verdicts = [self.engine.check_row(i, tests[i], units[i], ranges[i])
                    for i in range(len(rows))]
        return self.engine.detect_misalignment(tests, units, ranges, verdicts)

    def test_a_clean_report_is_not_accused(self) -> None:
        rows = [
            {"test_name": "HAEMOGLOBIN", "range": "13-17", "unit": "g/dL"},
            {"test_name": "TOTAL WBC COUNT", "range": "4000-10000", "unit": "cells/cu.mm"},
            {"test_name": "RBC COUNT", "range": "4.5-5.5", "unit": "mil/cu.mm"},
            {"test_name": "PLATELET COUNT", "range": "150000-410000", "unit": "cells/cu.mm"},
            {"test_name": "SODIUM", "range": "135-145", "unit": "mmol/L"},
        ]
        self.assertFalse(self.analyse(rows).suspected)

    def test_a_one_row_shift_is_detected_with_its_offset(self) -> None:
        rows = [
            {"test_name": "aemoglobin", "range": "4000-10000", "unit": "cell/cu.mm"},
            {"test_name": "otal WBC Count", "range": "4.50-5.50", "unit": "mil/cu.mm"},
            {"test_name": "RBC Coun", "range": "36-46", "unit": "%"},
            {"test_name": "aematocrit", "range": "83-101", "unit": "FL"},
            {"test_name": "ean Cell Volume (MCV)", "range": "27-32", "unit": "pg"},
            {"test_name": "MCH", "range": "31.5-34.5", "unit": "gm/dL"},
            {"test_name": "MCHC", "range": "150000-410000", "unit": "cells/cu.mm"},
        ]
        verdict = self.analyse(rows)
        self.assertTrue(verdict.suspected)
        self.assertEqual(verdict.offset, 1)
        self.assertEqual(verdict.explained_fraction, 1.0)
        self.assertTrue(verdict.evidence)

    def test_the_supplied_sample_is_detected(self) -> None:
        payload = json.loads((INCOMING / "Sample_JSON_file2.json").read_text())
        rows = payload["data"]["responseDetails"][0]["data"]["report_details"]
        verdict = self.analyse(rows)
        self.assertTrue(verdict.suspected)
        self.assertEqual(verdict.offset, 1)

    def test_a_few_bad_rows_are_not_a_shift(self) -> None:
        rows = [
            {"test_name": "HAEMOGLOBIN", "range": "13-17", "unit": "g/dL"},
            {"test_name": "SODIUM", "range": "135-145", "unit": "mmol/L"},
            {"test_name": "POTASSIUM", "range": "3.5-5.1", "unit": "mmol/L"},
            {"test_name": "PLATELET COUNT", "range": "150000-410000", "unit": "cells/cu.mm"},
            {"test_name": "CHLORIDE", "range": "98-107", "unit": "%"},      # one bad unit
        ]
        self.assertFalse(self.analyse(rows).suspected)

    def test_the_pipeline_never_realigns_automatically(self) -> None:
        # A wrong realignment writes a plausible-looking wrong value into a
        # claim decision, which is undetectable downstream.
        self.assertFalse(self.engine.auto_correct)


if __name__ == "__main__":
    unittest.main()
