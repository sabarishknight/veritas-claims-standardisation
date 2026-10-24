"""Schema contracts: the target output shape and config integrity."""

from __future__ import annotations

import csv
import unittest

from veritas.config import Catalog
from veritas.load import schema

from .support import ROOT, catalog, config

TARGET_SCHEMA_CSV = ROOT / "docs" / "ideal-output-schema.csv"


class TargetSchemaTests(unittest.TestCase):
    """The assignment's 79-column shape, delivered exactly."""

    @unittest.skipUnless(TARGET_SCHEMA_CSV.exists(),
                         "reference schema CSV not bundled")
    def test_view_columns_match_the_supplied_schema_exactly(self) -> None:
        with TARGET_SCHEMA_CSV.open(newline="", encoding="utf-8") as handle:
            expected = [row["column_name"] for row in csv.DictReader(handle)]
        actual = [name for name, _ in schema.IDEAL_OUTPUT_COLUMNS]
        self.assertEqual(actual, expected,
                         "v_ideal_output_schema must match the target column list "
                         "in name and order")

    def test_the_view_covers_both_record_kinds(self) -> None:
        sql = schema.ideal_output_view()
        self.assertIn("UNION ALL", sql)
        self.assertIn("FROM lab_result", sql)
        self.assertIn("FROM medication", sql)

    def test_duplicates_are_excluded_from_the_delivered_view(self) -> None:
        self.assertIn("is_duplicate = 0", schema.ideal_output_view())


class WideViewTests(unittest.TestCase):
    """FR-2.2: exactly five columns per defined test."""

    def test_five_columns_per_test(self) -> None:
        names = ["HAEMOGLOBIN", "PLATELET COUNT"]
        sql = schema.test_wide_view(names)
        for canonical in names:
            safe = "".join(c if c.isalnum() else "_" for c in canonical).replace("__", "_")
            for suffix in ("", "_Result", "_Range", "_Unit", "_Analytics"):
                with self.subTest(column=safe + suffix):
                    self.assertIn(f"AS {safe}{suffix}", sql)

    def test_the_view_is_generated_from_the_catalogue(self) -> None:
        # Adding a test to config must add its five columns without a migration.
        sql = schema.test_wide_view(catalog().tests.keys())
        self.assertIn("AS HAEMOGLOBIN_Analytics", sql)
        self.assertIn("AS ABSOLUTE_NEUTROPHIL_COUNT_Result", sql)


class DdlTests(unittest.TestCase):
    def test_both_dialects_render_from_one_definition(self) -> None:
        sqlite = "\n".join(schema.sqlite_ddl())
        bigquery = "\n".join(schema.bigquery_ddl("project.dataset"))
        for table in schema.ALL_TABLES:
            with self.subTest(table=table.name):
                self.assertIn(table.name, sqlite)
                self.assertIn(table.name, bigquery)

    def test_fact_tables_are_partitioned_and_clustered(self) -> None:
        bigquery = "\n".join(schema.bigquery_ddl("project.dataset"))
        self.assertIn("PARTITION BY DATE(ingested_at)", bigquery)
        self.assertIn("CLUSTER BY source_system, test_name_canonical", bigquery)

    def test_every_table_has_exactly_one_primary_key(self) -> None:
        # Idempotency is a property of the key; a table without one cannot
        # MERGE.
        for table in schema.ALL_TABLES:
            with self.subTest(table=table.name):
                self.assertEqual(len(table.primary_key), 1)

    def test_lineage_columns_are_on_every_fact_table(self) -> None:
        required = {"file_uri", "file_sha256", "trace_id", "correlation_id",
                    "source_system", "ingested_at", "run_id"}
        for table in (schema.CLINICAL_DOCUMENT, schema.LAB_RESULT, schema.MEDICATION):
            with self.subTest(table=table.name):
                names = {c.name for c in table.columns}
                self.assertTrue(required <= names)


class ConfigIntegrityTests(unittest.TestCase):
    """Config errors must be loud at load time, not silent per row."""

    def test_the_shipped_catalogue_is_internally_consistent(self) -> None:
        self.assertGreater(len(catalog().tests), 50)

    def test_a_canonical_unit_outside_its_dimension_is_rejected(self) -> None:
        broken = {
            "canonical_tests": [{
                "canonical": "BROKEN", "dimension": "mass_concentration",
                "canonical_unit": "%", "aliases": [],
            }],
        }
        units = {"dimensions": {
            "mass_concentration": {"base_unit": "g/dL", "units": {"g/dL": 1.0}},
            "fraction": {"base_unit": "%", "units": {"%": 1.0}},
        }}
        with self.assertRaises(ValueError) as caught:
            Catalog(broken, units)
        self.assertIn("dimension", str(caught.exception))

    def test_an_unknown_dimension_is_rejected(self) -> None:
        broken = {"canonical_tests": [{
            "canonical": "BROKEN", "dimension": "nonsense",
            "canonical_unit": "g/dL", "aliases": [],
        }]}
        units = {"dimensions": {
            "mass_concentration": {"base_unit": "g/dL", "units": {"g/dL": 1.0}}}}
        with self.assertRaises(ValueError):
            Catalog(broken, units)

    def test_profile_inheritance_resolves(self) -> None:
        fasttrack = config().profile_for({"source_system": "FASTTRACK"})
        # Inherited from _default:
        self.assertIn("lab_report", fasttrack.documents)
        self.assertTrue(fasttrack.date_formats)
        # Overridden locally:
        self.assertEqual(
            fasttrack.documents["lab_report"]["source_interpretation_semantics"], "mixed")

    def test_the_config_fingerprint_is_stable(self) -> None:
        self.assertEqual(config().fingerprint, config().fingerprint)
        self.assertEqual(len(config().fingerprint), 16)

    def test_every_catalogue_test_declares_an_envelope_or_a_range(self) -> None:
        for name, entry in catalog().tests.items():
            with self.subTest(test=name):
                low, high = entry.envelope()
                has_bounds = low is not None or high is not None
                self.assertTrue(has_bounds or entry.value_type != "numeric",
                                f"{name} is numeric but has no plausible envelope")


if __name__ == "__main__":
    unittest.main()
