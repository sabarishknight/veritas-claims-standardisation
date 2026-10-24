"""Envelope routing, duplicate suppression and demographics (FR-1.x, FR-2.5)."""

from __future__ import annotations

import json
import unittest

from veritas.ingest.dedup import (
    SeenRegistry,
    collapse_repeat_rows,
    content_hash,
    document_identity,
    row_id,
)
from veritas.ingest.envelope import (
    EnvelopeError,
    coverage_report,
    first_present,
    parse_json,
    resolve_many,
    resolve_path,
    unwrap,
)
from veritas.models import Flag
from veritas.standardise.demographics import is_redacted, parse_age, parse_date, parse_gender
from veritas.standardise.pii import Tokeniser

from .support import EDGE_CASES, INCOMING, config

MARKERS = tuple(config().section("pii")["redaction_markers"])


def load(path):
    return json.loads(path.read_text())


class PathResolutionTests(unittest.TestCase):
    DOC = {"a": {"b": [{"c": 1}, {"c": 2}]}, "top": "x"}

    def test_dotted_paths(self) -> None:
        self.assertEqual(resolve_path(self.DOC, "top"), "x")
        self.assertEqual(resolve_path(self.DOC, "a.b[0].c"), 1)

    def test_wildcard_returns_the_list(self) -> None:
        self.assertEqual(len(resolve_many(self.DOC, "a.b[*]")), 2)

    def test_missing_hops_return_none_rather_than_raising(self) -> None:
        self.assertIsNone(resolve_path(self.DOC, "a.nope.deeper"))
        self.assertEqual(resolve_many(self.DOC, "a.nope[*]"), [])

    def test_dot_means_the_payload_itself(self) -> None:
        self.assertIs(resolve_path(self.DOC, "."), self.DOC)

    def test_first_present_skips_empty_candidates(self) -> None:
        payload = {"a": "", "b": None, "c": "value"}
        self.assertEqual(first_present(payload, ["a", "b", "c"]), "value")


class EnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = config()
        self.default = self.config.profile_for({})

    def test_a_file_can_carry_several_documents_of_different_kinds(self) -> None:
        envelope = unwrap(load(INCOMING / "Sample_JSON_file5.json"), self.default)
        self.assertEqual([d.classifier for d in envelope.documents],
                         ["lab_report", "discharge_summary", "discharge_summary"])

    def test_metadata_is_pivoted_from_key_value_pairs(self) -> None:
        envelope = unwrap(load(INCOMING / "Sample_JSON_file1.json"), self.default)
        self.assertEqual(envelope.meta["source_system"], "FASTTRACK")
        self.assertTrue(envelope.meta["claim_no"])

    def test_unknown_metadata_keys_are_kept_and_reported(self) -> None:
        envelope = unwrap(load(EDGE_CASES / "unprofiled_clinic.json"), self.default)
        self.assertIn("facility_tier", envelope.unmapped_meta_keys)

    def test_profile_selection_is_driven_by_envelope_metadata(self) -> None:
        self.assertEqual(
            self.config.profile_for({"source_system": "FASTTRACK"}).clinic_id, "FASTTRACK")
        self.assertEqual(
            self.config.profile_for({"source_system": "ARTEMIS"}).clinic_id, "ARTEMIS")

    def test_an_unknown_source_is_processed_not_rejected(self) -> None:
        # FR-1.3: a new clinic must flow end to end on day zero.
        profile = self.config.profile_for({"source_system": "MERIDIAN_LABS"})
        self.assertEqual(profile.clinic_id, "_default")
        envelope = unwrap(load(EDGE_CASES / "unprofiled_clinic.json"), profile)
        self.assertEqual(len(envelope.documents), 1)

    def test_every_failure_mode_has_its_own_reason_code(self) -> None:
        cases = {
            "broken_truncated.json": "malformed_json",
            "broken_not_an_object.json": "not_an_object",
            "broken_no_documents.json": "no_documents",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                raw = (EDGE_CASES / filename).read_bytes()
                try:
                    unwrap(parse_json(raw, filename), self.default)
                except EnvelopeError as exc:
                    self.assertEqual(exc.reason, expected)
                else:
                    self.fail(f"{filename} should have raised")

    def test_empty_bytes_are_rejected_cleanly(self) -> None:
        with self.assertRaises(EnvelopeError):
            parse_json(b"", "empty")

    def test_coverage_report_names_unread_fields(self) -> None:
        # The onboarding checklist for a new clinic (NFR-2.2).
        report = coverage_report(load(INCOMING / "Sample_JSON_file5.json"), self.default)
        self.assertGreater(report["coverage"], 0.5)
        self.assertIsInstance(report["unread"], list)


class DeduplicationTests(unittest.TestCase):
    """Identity comes from the content, because the source's identifiers lie."""

    def setUp(self) -> None:
        self.excludes = config().section("dedup")["content_hash_excludes"]

    def payloads(self, *filenames):
        out = []
        for name in filenames:
            doc = load(INCOMING / name)
            out.append(doc["data"]["responseDetails"][0]["data"])
        return out

    def test_the_same_report_under_different_ids_hashes_the_same(self) -> None:
        one, three = self.payloads("Sample_JSON_file1.json", "Sample_JSON_file3.json")
        self.assertEqual(content_hash(one, self.excludes),
                         content_hash(three, self.excludes))

    def test_those_two_files_really_do_carry_different_identifiers(self) -> None:
        a = load(INCOMING / "Sample_JSON_file1.json")["data"]
        b = load(INCOMING / "Sample_JSON_file3.json")["data"]
        self.assertNotEqual(a["documentId"], b["documentId"])
        self.assertNotEqual(a["correlationId"], b["correlationId"])

    def test_identity_based_dedup_would_miss_it(self) -> None:
        a = document_identity(strategy="document_id", payload={}, header={},
                              record_type="discharge_summary", document_id="DOC-A",
                              source_row_index=0)
        b = document_identity(strategy="document_id", payload={}, header={},
                              record_type="discharge_summary", document_id="DOC-B",
                              source_row_index=0)
        self.assertNotEqual(a, b)

    def test_different_reports_hash_differently(self) -> None:
        one, two = self.payloads("Sample_JSON_file1.json", "Sample_JSON_file2.json")
        self.assertNotEqual(content_hash(one, self.excludes),
                            content_hash(two, self.excludes))

    def test_key_order_does_not_change_the_hash(self) -> None:
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))

    def test_a_corrected_resubmission_is_not_suppressed(self) -> None:
        # Excluding identifiers but including clinical fields is what makes a
        # genuine correction produce a different hash.
        base = {"diagnosis": "Enteric fever", "claim_no": "X"}
        corrected = {"diagnosis": "Enteric fever with dehydration", "claim_no": "Y"}
        self.assertNotEqual(content_hash(base, self.excludes),
                            content_hash(corrected, self.excludes))

    def test_repeat_rows_within_a_report_collapse(self) -> None:
        payload = load(INCOMING / "Sample_JSON_file4.json")
        rows = payload["data"]["responseDetails"][0]["data"]["report_details"]
        collapsed = collapse_repeat_rows(rows)
        self.assertLess(len(collapsed.kept), len(rows))
        self.assertGreater(collapsed.collapsed, 0)

    def test_a_genuine_serial_draw_survives_collapsing(self) -> None:
        # Two draws of the same test with different values is a falling
        # haemoglobin - exactly the signal an analyst wants.
        rows = [{"test_name": "HAEMOGLOBIN", "result": "13.1", "range": "13-17", "unit": "g/dL"},
                {"test_name": "HAEMOGLOBIN", "result": "11.4", "range": "13-17", "unit": "g/dL"}]
        self.assertEqual(len(collapse_repeat_rows(rows).kept), 2)

    def test_row_ids_are_deterministic_and_distinct_per_occurrence(self) -> None:
        self.assertEqual(row_id("doc", "HAEMOGLOBIN", 0), row_id("doc", "HAEMOGLOBIN", 0))
        self.assertNotEqual(row_id("doc", "HAEMOGLOBIN", 0), row_id("doc", "HAEMOGLOBIN", 1))

    def test_registry_reports_where_a_duplicate_was_first_seen(self) -> None:
        registry = SeenRegistry()
        self.assertIsNone(registry.check("h1", "fileA"))
        self.assertEqual(registry.check("h1", "fileB"), "fileA")
        self.assertEqual(registry.duplicates, 1)


class DemographicsTests(unittest.TestCase):
    def test_compact_age_notation(self) -> None:
        self.assertAlmostEqual(parse_age("33Y11M26D").years, 33.988, places=2)

    def test_paediatric_fraction_notation(self) -> None:
        self.assertEqual(parse_age("6/12").years, 0.5)

    def test_implausible_ages_are_refused(self) -> None:
        self.assertIsNone(parse_age("999").years)
        self.assertIn(Flag.AGE_UNPARSEABLE, parse_age("999").flags)

    def test_gender_spellings(self) -> None:
        self.assertEqual(parse_gender("Male")[0], "M")
        self.assertEqual(parse_gender("FEMALE")[0], "F")

    def test_unknown_sex_is_none_not_a_default(self) -> None:
        # Sex selects the reference range; defaulting it would silently apply
        # male ranges to female patients.
        self.assertIsNone(parse_gender("unknown")[0])
        self.assertIsNone(parse_gender("[GENDER REDACTED]", MARKERS)[0])

    def test_date_formats_from_the_corpus(self) -> None:
        formats = config().profile_for({"source_system": "FASTTRACK"}).date_formats
        for raw, expected in (("09-10-2025", "2025-10-09"), ("07-Oct-2025", "2025-10-07"),
                              ("08/Oct/2025", "2025-10-08")):
            with self.subTest(raw=raw):
                self.assertEqual(parse_date(raw, formats, MARKERS)[0], expected)

    def test_a_placeholder_is_not_coerced_into_a_date(self) -> None:
        iso, original, flags = parse_date("DD/MM/YYYY", ["%d-%m-%Y"], MARKERS)
        self.assertIsNone(iso)
        self.assertEqual(original, "DD/MM/YYYY")
        self.assertIn(Flag.PLACEHOLDER_DATE, flags)

    def test_a_bill_number_in_a_date_column_is_not_a_date(self) -> None:
        iso, _, flags = parse_date("LAB10945", ["%d-%m-%Y"], MARKERS)
        self.assertIsNone(iso)
        self.assertIn(Flag.DATE_UNPARSEABLE, flags)

    def test_redaction_markers_are_recognised(self) -> None:
        self.assertTrue(is_redacted("[PATIENT NAME REDACTED]", MARKERS))
        self.assertFalse(is_redacted("Priya Raghunathan", MARKERS))


class TokenisationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tokeniser = Tokeniser(
            mode="tokenise", fields=["patient_name", "uhid"],
            keep_plaintext=["hospital_name"], key=b"test-key",
            redaction_markers=MARKERS)

    def test_tokens_are_deterministic_so_patients_link(self) -> None:
        a, _ = self.tokeniser.process("patient_name", "Priya Raghunathan")
        b, _ = self.tokeniser.process("patient_name", "priya raghunathan")
        self.assertEqual(a, b)

    def test_different_patients_get_different_tokens(self) -> None:
        a, _ = self.tokeniser.process("patient_name", "Priya Raghunathan")
        b, _ = self.tokeniser.process("patient_name", "Arun Mehta")
        self.assertNotEqual(a, b)

    def test_the_plaintext_is_not_retained(self) -> None:
        token, plaintext = self.tokeniser.process("patient_name", "Priya Raghunathan")
        self.assertIsNotNone(token)
        self.assertIsNone(plaintext)

    def test_a_different_key_gives_a_different_token(self) -> None:
        other = Tokeniser(mode="tokenise", fields=["patient_name"], key=b"other-key")
        a, _ = self.tokeniser.process("patient_name", "Priya Raghunathan")
        b, _ = other.process("patient_name", "Priya Raghunathan")
        self.assertNotEqual(a, b)

    def test_already_redacted_values_are_dropped_not_tokenised(self) -> None:
        # Hashing the marker would create one very popular token that looks
        # like a single extremely busy patient.
        token, plaintext = self.tokeniser.process("patient_name", "[PATIENT NAME REDACTED]")
        self.assertIsNone(token)
        self.assertIsNone(plaintext)

    def test_allow_listed_fields_stay_readable(self) -> None:
        _, plaintext = self.tokeniser.process("hospital_name", "Lakeview Multispeciality")
        self.assertEqual(plaintext, "Lakeview Multispeciality")


if __name__ == "__main__":
    unittest.main()
