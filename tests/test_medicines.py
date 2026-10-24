"""Brand-to-generic medicine resolution (FR-2.6)."""

from __future__ import annotations

import unittest

from veritas.models import Flag
from veritas.standardise.medicines import MedicineResolver

from .support import config


class MedicineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.r = MedicineResolver(config().medicines)

    def test_dosage_form_and_strength_are_split_off(self) -> None:
        result = self.r.resolve("TAB CEFTUM 500 MG")
        self.assertEqual(result.generic, "Cefuroxime")
        self.assertEqual(result.dosage_form, "tablet")
        self.assertEqual(result.strength, "500 mg")

    def test_one_molecule_across_three_spellings(self) -> None:
        # This is what makes drug-utilisation analysis possible at all.
        generics = {self.r.resolve(x).generic for x in
                    ("Tab. Pan 40", "CAP. PAN 40", "INJ PANTOP 40 MG IV 12 HRLY")}
        self.assertEqual(generics, {"Pantoprazole"})

    def test_administration_instructions_are_stripped(self) -> None:
        result = self.r.resolve("INJ. MONOCEF 1 GR/ IV 12 HRLY")
        self.assertEqual(result.generic, "Ceftriaxone")

    def test_combinations_stay_combinations(self) -> None:
        # Collapsing PAN D to pantoprazole would hide a duplicate-therapy signal.
        result = self.r.resolve("Cap. Pan D")
        self.assertEqual(result.generic, "Pantoprazole + Domperidone")
        self.assertTrue(result.is_combination)

    def test_consumables_are_not_drugs(self) -> None:
        for item in ("IV Fluids", "SYRINGE 10ML", "IVSET INTRAFLOW", "N/A", "IV FIXER"):
            with self.subTest(item=item):
                result = self.r.resolve(item)
                self.assertIsNone(result.generic)
                self.assertEqual(result.skip_reason, "not_a_medicine")

    def test_unknown_brands_are_reported_not_guessed(self) -> None:
        result = self.r.resolve("TAB. ZZQQBRAND 10")
        self.assertIsNone(result.generic)
        self.assertEqual(result.skip_reason, "brand_not_in_reference")

    def test_uncertain_mappings_are_labelled(self) -> None:
        # Resolved, but excluded from clinical analytics until signed off.
        result = self.r.resolve("Tab. miso")
        self.assertEqual(result.generic, "Misoprostol")
        self.assertIn(Flag.BRAND_UNVERIFIED, result.flags)

    def test_confident_mappings_are_not_labelled(self) -> None:
        self.assertEqual(self.r.resolve("TAB DOLO 650").flags, ())

    def test_every_sample_medication_resolves_or_is_explained(self) -> None:
        import json

        from .support import INCOMING
        unexplained = []
        for path in sorted(INCOMING.glob("*.json")):
            payload = json.loads(path.read_text())
            for record in payload["data"]["responseDetails"]:
                for med in (record["data"] or {}).get("dischargeMedications", []) or []:
                    name = med.get("medicine")
                    if not name:
                        continue
                    result = self.r.resolve(name)
                    if result.generic is None and result.skip_reason is None:
                        unexplained.append(name)
        self.assertEqual(unexplained, [],
                         "every medication must resolve or carry a skip reason")


if __name__ == "__main__":
    unittest.main()
