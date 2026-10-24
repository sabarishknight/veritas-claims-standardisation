"""End-to-end pipeline behaviour, including the NFR guarantees.

These are the tests that matter most, because every unit test above can pass
while the assembled pipeline still does the wrong thing. They run the real
pipeline over the real sample corpus and assert on what lands in the warehouse.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from veritas.load.sqlite_sink import SqliteSink
from veritas.observe.logging import configure
from veritas.pipeline import Pipeline

from .support import ROOT, config

# Worker processes re-read the log level from the environment, so quieting the
# parent alone would still leave the parallel test noisy.
os.environ.setdefault("VERITAS_LOG_LEVEL", "CRITICAL")
configure(level="CRITICAL", fmt="text")


class PipelineHarness(unittest.TestCase):
    """Runs the pipeline once per class over the whole sample corpus."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="veritas-test-"))
        cls.db = cls.tmp / "test.db"
        cls.config = config()
        cls.sink = SqliteSink(cls.db)
        cls.summary = Pipeline(cls.config, cls.sink,
                               config_dir=str(ROOT / "config")).run(
            str(ROOT / "sample-data"), workers=1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sink.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rows(self, sql: str, params=()):
        return self.sink.query(sql, params)

    def one(self, sql: str, params=()):
        return self.sink.scalar(sql, params)


class EndToEndTests(PipelineHarness):
    def test_the_whole_corpus_produces_rows(self) -> None:
        self.assertGreater(self.summary.rows_emitted, 100)
        self.assertGreater(self.summary.documents_emitted, 5)
        self.assertGreater(self.summary.medications_emitted, 10)

    def test_a_malformed_file_does_not_stop_the_run(self) -> None:
        # NFR-3.1: one clinic's bad upload must not stop the other 499.
        self.assertGreater(self.summary.files_failed, 0)
        self.assertGreater(self.summary.files_processed, 10)

    def test_every_failure_is_recorded_with_a_reason(self) -> None:
        reasons = {r["reason"] for r in self.rows(
            "SELECT DISTINCT reason FROM processing_error")}
        self.assertIn("malformed_json", reasons)
        self.assertTrue(all(r for r in reasons), "a blank reason is not actionable")

    def test_no_row_is_written_without_lineage(self) -> None:
        # NFR-4.2: an output record that cannot be traced back is not auditable.
        orphans = self.one(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE file_sha256 IS NULL OR file_sha256 = '' "
            "   OR run_id IS NULL OR source_system IS NULL")
        self.assertEqual(orphans, 0)

    def test_every_row_belongs_to_a_document(self) -> None:
        orphans = self.one(
            "SELECT COUNT(*) FROM lab_result r "
            "LEFT JOIN clinical_document d ON d.id = r.document_pk "
            "WHERE d.id IS NULL")
        self.assertEqual(orphans, 0)

    def test_classification_values_are_from_the_closed_set(self) -> None:
        allowed = {"Within Range", "Above Range", "Below Range",
                   "Outlier", "Invalid", "Not Applicable"}
        seen = {r["test_analytics"] for r in self.rows(
            "SELECT DISTINCT test_analytics FROM lab_result")}
        self.assertTrue(seen <= allowed, f"unexpected values: {seen - allowed}")


class DuplicateSuppressionTests(PipelineHarness):
    def test_the_resubmitted_discharge_summary_is_suppressed(self) -> None:
        self.assertGreaterEqual(self.summary.documents_duplicate, 2)

    def test_suppressed_documents_contribute_no_rows(self) -> None:
        leaked = self.one(
            "SELECT COUNT(*) FROM lab_result r "
            "JOIN clinical_document d ON d.id = r.document_pk "
            "WHERE d.is_duplicate = 1")
        self.assertEqual(leaked, 0)

    def test_suppression_is_visible_for_audit(self) -> None:
        # A silent deduplicator is one nobody trusts.
        self.assertGreater(len(self.rows("SELECT * FROM v_duplicates")), 0)


class CoherenceIntegrationTests(PipelineHarness):
    def test_the_misaligned_report_is_quarantined(self) -> None:
        items = self.rows(
            "SELECT * FROM review_item WHERE kind = 'misaligned_report'")
        self.assertGreaterEqual(len(items), 1)
        self.assertIn("shifted", items[0]["evidence"])
        self.assertIn("not applied automatically", items[0]["proposal"])

    def test_rows_in_a_misaligned_report_are_flagged(self) -> None:
        flagged = self.one(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE flags LIKE '%report_misalignment_suspected%'")
        self.assertGreater(flagged, 0)

    def test_a_wbc_value_is_never_loaded_as_a_haemoglobin(self) -> None:
        """The defect this whole design exists to prevent."""
        bad = self.rows(
            "SELECT result_value, unit_original FROM lab_result "
            "WHERE test_name_canonical = 'HAEMOGLOBIN' "
            "  AND result_value IS NOT NULL "
            "  AND test_analytics NOT IN ('Invalid', 'Outlier') "
            "  AND result_value > 100")
        self.assertEqual(bad, [], "a count-scale value was loaded as a haemoglobin")

    def test_dimension_mismatches_are_detected_and_invalidated(self) -> None:
        mismatched = self.rows(
            "SELECT test_analytics FROM lab_result "
            "WHERE flags LIKE '%unit_dimension_mismatch%'")
        self.assertGreater(len(mismatched), 0)
        self.assertTrue(all(r["test_analytics"] == "Invalid" for r in mismatched))


class RequirementCoverageTests(PipelineHarness):
    """One assertion per functional requirement, against real output."""

    def test_fr_2_1_names_are_normalised_with_recorded_provenance(self) -> None:
        methods = {r["normalization_method"] for r in self.rows(
            "SELECT DISTINCT normalization_method FROM lab_result")}
        self.assertTrue({"exact", "alias", "fuzzy_affix"} <= methods)
        self.assertEqual(self.one(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE test_name_canonical IS NOT NULL AND normalization_confidence IS NULL"), 0)

    def test_fr_2_3_mixed_text_and_numeric_results_are_converted(self) -> None:
        converted = self.one(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE result_value IS NOT NULL AND result_text_original LIKE '%,%'")
        self.assertGreater(converted, 0)

    def test_fr_2_4_units_are_harmonised_with_value_conversion(self) -> None:
        rows = self.rows(
            "SELECT result_text_original, unit_original, result_value, unit_canonical "
            "FROM lab_result WHERE flags LIKE '%unit_converted%'")
        self.assertGreater(len(rows), 0)
        lakh = self.rows(
            "SELECT result_value FROM lab_result "
            "WHERE unit_original LIKE '%lakh%' AND result_value IS NOT NULL")
        self.assertTrue(any(r["result_value"] and r["result_value"] > 1000 for r in lakh),
                        "a lakh-scale platelet count should convert to cells/cu.mm")

    def test_fr_2_5_demographics_are_normalised(self) -> None:
        self.assertGreater(self.one(
            "SELECT COUNT(*) FROM clinical_document WHERE age_years IS NOT NULL"), 0)
        self.assertGreater(self.one(
            "SELECT COUNT(*) FROM clinical_document "
            "WHERE admission_date LIKE '____-__-__'"), 0)

    def test_fr_2_6_brands_resolve_to_molecules(self) -> None:
        self.assertGreater(self.one(
            "SELECT COUNT(DISTINCT medicine_generic) FROM medication "
            "WHERE medicine_generic IS NOT NULL"), 5)

    def test_fr_3_2_outliers_are_separate_from_out_of_range(self) -> None:
        self.assertGreater(self.summary.rows_outlier, 0)
        self.assertGreater(self.summary.rows_out_of_range, 0)

    def test_fr_3_4_non_numeric_where_numeric_expected_is_flagged(self) -> None:
        self.assertGreater(self.one(
            "SELECT COUNT(*) FROM lab_result WHERE flags LIKE '%result_non_numeric%'"), 0)

    def test_fr_4_1_the_target_view_is_populated(self) -> None:
        rows = self.rows("SELECT * FROM v_ideal_output_schema LIMIT 5")
        self.assertTrue(rows)
        self.assertEqual(len(rows[0]), 78)

    def test_fr_4_3_the_raw_payload_is_retained_for_audit(self) -> None:
        self.assertGreater(self.one(
            "SELECT COUNT(*) FROM clinical_document WHERE raw_payload IS NOT NULL"), 0)

    def test_fr_5_the_operational_views_answer(self) -> None:
        for view in ("v_clinic_quality", "v_flagged_rows", "v_duplicates",
                     "v_lineage", "v_panel_completeness", "v_test_wide"):
            with self.subTest(view=view):
                self.sink.query(f"SELECT * FROM {view} LIMIT 1")

    def test_nfr_2_1_an_unprofiled_clinic_flows_end_to_end(self) -> None:
        rows = self.rows(
            "SELECT * FROM lab_result WHERE source_system = 'MERIDIAN_LABS'")
        self.assertGreater(len(rows), 0, "a clinic with no profile must still load")
        self.assertTrue(all(r["clinic_profile"] == "_default" for r in rows))

    def test_nfr_4_3_patient_names_are_tokenised_not_stored(self) -> None:
        leaked = self.one(
            "SELECT COUNT(*) FROM clinical_document WHERE patient_name IS NOT NULL")
        self.assertEqual(leaked, 0, "a patient name reached the warehouse")
        self.assertGreater(self.one(
            "SELECT COUNT(*) FROM clinical_document WHERE patient_token IS NOT NULL"), 0)


class IdempotencyTests(unittest.TestCase):
    """NFR-3.2, asserted by running the whole pipeline twice."""

    def test_reprocessing_the_same_input_changes_nothing(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="veritas-idem-"))
        try:
            sink = SqliteSink(tmp / "idem.db")
            cfg = config()
            source = str(ROOT / "sample-data")

            Pipeline(cfg, sink, config_dir=str(ROOT / "config")).run(source, workers=1)
            counts_one = self._counts(sink)
            checksum_one = sink.checksum()

            Pipeline(cfg, sink, config_dir=str(ROOT / "config")).run(source, workers=1)
            counts_two = self._counts(sink)
            checksum_two = sink.checksum()

            self.assertEqual(counts_one, counts_two,
                             "a second run must not add or remove rows")
            self.assertEqual(checksum_one, checksum_two,
                             "a second run must not change any value")
            sink.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _counts(sink: SqliteSink) -> dict[str, int]:
        return {t: sink.scalar(f"SELECT COUNT(*) FROM {t}")
                for t in ("clinical_document", "lab_result", "medication",
                          "processing_error", "file_ledger")}


class ParallelConsistencyTests(unittest.TestCase):
    """Workers must not change the answer, only the wall clock."""

    def test_multi_worker_output_matches_single_worker(self) -> None:
        results = {}
        for workers in (1, 3):
            tmp = Path(tempfile.mkdtemp(prefix=f"veritas-w{workers}-"))
            try:
                sink = SqliteSink(tmp / "w.db")
                Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                    str(ROOT / "sample-data"), workers=workers)
                results[workers] = (
                    sink.scalar("SELECT COUNT(*) FROM lab_result"),
                    sink.scalar("SELECT COUNT(*) FROM clinical_document WHERE is_duplicate=1"),
                    sink.checksum(),
                )
                sink.close()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(results[1], results[3],
                         "parallel execution changed the output")


if __name__ == "__main__":
    unittest.main()
