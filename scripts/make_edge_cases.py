#!/usr/bin/env python3
"""Generate the edge-case corpus in sample-data/edge-cases/.

The five supplied files are real traffic and carry real defects, but they do
not cover every branch the pipeline has to handle, and a prototype that only
works on the happy path of one sample is not evidence of anything. These files
are written by a script rather than by hand so they are reproducible and so the
intent of each one is documented next to the data it produces.

Every file names the requirement it exercises. All identities are synthetic.

Run:  python3 scripts/make_edge_cases.py
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "sample-data" / "edge-cases"


def envelope(source_system: str, documents: list[tuple[str, object]],
             *, claim: str | None = None, extra_meta: list[dict] | None = None) -> dict:
    meta = [
        {"key": "DestinationIdentifier", "value": "stg-datalake"},
        {"key": "claim_no", "value": claim or f"CLAIM-{uuid.uuid4().hex[:8].upper()}"},
        {"key": "nt_code", "value": "NT-SYNTH"},
        {"key": "source_system", "value": source_system},
        {"key": "ConsumerClientId", "value": str(uuid.uuid4())},
    ]
    meta.extend(extra_meta or [])
    return {
        "traceId": uuid.uuid4().hex,
        "statusCode": "200",
        "message": ["Success"],
        "data": {
            "responseDetails": [
                {"classifier": classifier, "data": payload,
                 "status": "success", "errorMessage": None, "fileId": None}
                for classifier, payload in documents
            ],
            "correlationId": uuid.uuid4().hex,
            "documentId": f"DOC{uuid.uuid4().hex[:26].upper()}",
            "metaDetails": meta,
            "status": True,
            "message": None,
        },
    }


def lab(rows: list[dict], **basic) -> tuple[str, dict]:
    info = {
        "uhid": "SYN-0001", "age": "41 Years", "bill_date": "12-10-2025",
        "gender": "M", "lab_or_hospital_name": "Synthetic Diagnostics",
        "patient_name": "Test Patient", "reports_date": "12-10-2025",
    }
    info.update(basic)
    return "lab_report", {"basic_info": info, "report_details": rows}


def row(name, result, rng="", unit="", page=0, analytics=""):
    return {"page_no": page, "range": rng, "result": result,
            "test_analytics": analytics, "test_name": name, "unit": unit}


FILES: dict[str, tuple[str, object]] = {}


# ---------------------------------------------------------------- FR-2.4 ----
# The same four analytes reported in every unit spelling the corpus contains,
# including the OCR corruptions (FI for fL, UIL for U/L, gm/di for gm/dL) and
# the Indian lakh convention. All of them must converge on one canonical unit
# and one comparable number.
FILES["units_zoo.json"] = envelope("FASTTRACK", [lab([
    row("Haemoglobin", "13.7", "13-17", "g/dL"),
    row("Haemoglobin", "13.7", "13-17", "gm/dl"),
    row("Haemoglobin", "13.7", "13-17", "gm/di"),         # OCR: l -> i
    row("Haemoglobin", "137", "130-170", "mg/dL"),        # 100x, same quantity
    row("RBC COUNT", "4.74", "4.5-5.5", "mil/cu.mm"),
    row("RBC COUNT", "4.74", "4.5-5.5", "mil/cu.cm"),     # FR-2.4 worked example
    row("RBC COUNT", "4.74", "4.5-5.5", "Million/cu.mm"),
    row("PLATELET COUNT", "190000", "150000-410000", "cells/cu.mm"),
    row("PLATELET COUNT", "1.90", "1.5-4.1", "lakhs/cumm"),   # 1 lakh = 10^5
    row("PLATELET COUNT", "1.90", "1.5-4.1", "lac/cmm"),
    row("MPV", "10.5", "8.1-13.9", "FI"),                 # OCR: fL -> FI
    row("ALKALINE PHOSPHATASE", "96", "38-126", "UIL"),   # OCR: U/L -> UIL
    row("Temp", "98.6", "", "degree F"),                  # affine conversion
    row("Temp", "37.0", "", "degC"),
])], claim="CLAIM-UNITS-ZOO")


# ---------------------------------------------------------------- FR-3.2 ----
# Physiologically impossible values next to genuinely critical ones. The point
# of the file is that these must NOT be classified the same way: 0.1 is broken
# data, 4.1 is a patient who needs a transfusion.
FILES["outliers_and_criticals.json"] = envelope("FASTTRACK", [lab([
    row("Haemoglobin", "0.1", "13-17", "g/dL"),           # impossible -> Outlier
    row("Haemoglobin", "999", "13-17", "g/dL"),           # impossible -> Outlier
    row("Haemoglobin", "4.1", "13-17", "g/dL"),           # critical, real
    row("Haemoglobin", "21.5", "13-17", "g/dL"),          # high, real
    row("PLATELET COUNT", "0", "150000-410000", "cells/cu.mm"),
    row("PLATELET COUNT", "11000", "150000-410000", "cells/cu.mm"),
    row("POTASSIUM", "7.9", "3.5-5.1", "mmol/L"),         # critical, real
    row("POTASSIUM", "0.2", "3.5-5.1", "mmol/L"),         # impossible
    row("SODIUM", "132", "135-145", "mmol/L"),
    row("Serum Creatinine", "14.2", "0.7-1.3", "mg/dL"),  # severe, real
])], claim="CLAIM-OUTLIERS")


# ------------------------------------------------- FR-1.3 / NFR-2.1 ---------
# A clinic nobody has written a profile for, using its own field spellings and
# nesting. It must flow end to end under `_default` at reduced confidence, not
# be rejected, and it must show up in the ops UI as unprofiled volume.
FILES["unprofiled_clinic.json"] = envelope("MERIDIAN_LABS", [
    ("lab_report", {
        "basic_info": {
            "patientName": "Unprofiled Patient",      # camelCase, not snake
            "patient_age": "58 Years",
            "sex": "Female",
            "mrn": "MER-99881",
            "labName": "Meridian Reference Laboratory",
            "reportDate": "2025-10-14",
            "billDate": "2025-10-14",
        },
        "report_details": [
            {"page": 1, "testName": "Haemoglobin", "value": "11.2",
             "reference_range": "12-15", "units": "g/dL", "flag": "low"},
            {"page": 1, "testName": "Total Leucocyte Count", "value": "12400",
             "reference_range": "4000-11000", "units": "cells/cu.mm", "flag": "high"},
            {"page": 1, "testName": "Serum Creatinine", "value": "0.9",
             "reference_range": "0.6-1.1", "units": "mg/dL", "flag": "normal"},
        ],
    })
], claim="CLAIM-UNPROFILED", extra_meta=[{"key": "facility_tier", "value": "TIER2"}])


# ---------------------------------------------------------------- NFR-4.3 ----
# Synthetic but realistic identities, because the supplied files are already
# redacted and tokenisation has to be exercised against something.
FILES["pii_synthetic.json"] = envelope("FASTTRACK", [
    ("discharge_summary", {
        "admissionDate": "02-10-2025", "dischargeDate": "06-10-2025",
        "age": "33Y11M26D", "gender": "Female",
        "patientName": "Priya Raghunathan",
        "doctorName": "Dr. A. Venkatesan",
        "hospitalName": "Lakeview Multispeciality",
        "hospitalAddress": "48 Nungambakkam High Road, Chennai 600034",
        "briefHistory": "Admitted with fever and dysuria for four days.",
        "diagnosis": "Acute pyelonephritis",
        "generalExaminations": "Temp 101.2F, PR 96/min, BP 110/70 mmHg",
        "recommendations": "Review in one week. Complete the antibiotic course.",
        "ward": "SEMI PRIVATE", "postDischargeAdvice": None,
        "courseDuringHospitalisation": [],
        "dischargeMedications": [
            {"medicine": "TAB. CEFTAS- 400", "dose": "400 mg",
             "frequency": "12 HOURLY", "type": None},
            {"medicine": "Tab. Dolo 650", "dose": "650 mg", "frequency": "SOS", "type": None},
            {"medicine": "Cap. Pan D", "dose": "1 CAP", "frequency": "BD", "type": None},
            {"medicine": "SYRINGE 5ML", "dose": "", "frequency": "", "type": None},
            {"medicine": "N/A", "dose": "N/A", "frequency": "N/A", "type": None},
        ],
        "medicineInjectionsInvestigation": ["INJ. MONOCEF 1 GM IV 12 HRLY",
                                            "INJ PANTOP 40 MG IV OD"],
    })
], claim="CLAIM-PII-SYNTH")


# ---------------------------------------------------------------- FR-2.5 ----
# Every age and date spelling the corpus produces, plus the placeholders that
# must not be coerced into dates.
FILES["demographics_formats.json"] = envelope("ARTEMIS", [
    ("discharge_summary", {
        "admissionDate": "26-08-2025", "dischargeDate": "28-Aug-2025",
        "age": "6/12", "gender": "M",            # paediatric: six months
        "patientName": "Synthetic Infant",
        "doctorName": "Dr. Synthetic",
        "hospitalName": "Paediatric Centre", "hospitalAddress": "Synthetic Road",
        "briefHistory": "Fever for two days.", "diagnosis": "Viral fever",
        "generalExaminations": "Weight 7.4 kg", "recommendations": "Review SOS",
        "ward": "DAY CARE", "postDischargeAdvice": None,
        "courseDuringHospitalisation": [], "dischargeMedications": [],
        "medicineInjectionsInvestigation": [],
    }),
    ("lab_report", {
        "basic_info": {
            "uhid": "SYN-AGE-2", "age": "33Y11M26D", "bill_date": "LAB55213",
            "gender": "FEMALE", "lab_or_hospital_name": "Synthetic Labs",
            "patient_name": "Synthetic Adult", "reports_date": "DD/MM/YYYY",
        },
        "report_details": [
            row("test_name", "result", "range", "unit", 0, "low/normal/high"),
            row("Haemoglobin", "H 15.9", "12-15", "g/dL", 1),
            row("Lymphocyte", "L 14.2", "20-40", "%", 1),
            row("BP", "148/94 mmHg", "", "mmHg", 1),
            row("Pulse", "104/min", "60-100", "/min", 1),
            row("SPO2", "94% On Room Air", "95-100", "%", 1),
        ],
    }),
], claim="CLAIM-DEMOGRAPHICS")


# ---------------------------------------------------------------- FR-3.3 ----
FILES["qualitative_results.json"] = envelope("FASTTRACK", [lab([
    row("WIDAL TEST", "POSITIVE", "Less than 1:80", "", 0),
    row("DENGUE NS 1", "NEGATIVE", "= 1 COI - Equivocal", "", 0),
    row("TYPHI DOT IGG & IGM", "", "", "", 0),
    row("CRP-QUANTITATIVE", "48.2", "0-6", "mg/L", 0),
    row("CRP-QUANTITATIVE", "POSITIVE", "0-6", "mg/L", 0),   # qualitative where numeric expected
    row("KETONE BODIES", "NEGATIVE", "NEGATIVE", "", 1),
    row("PROTEIN (ALBUMIN)", "NIL", "NIL", "NIL", 1),
    row("MALARIA ANTIGEN", "NOT DETECTED", "", "NOT DETECTED", 1),
    row("pH", "6.0", "4.6-8.0", "", 1),
    row("SP. GRAVITY", "1.015", "1.005 TO 1.030", "", 1),
])], claim="CLAIM-QUALITATIVE")


# ---------------------------------------------------------------- FR-1.2 ----
# The same clinical payload as sample file 1, resubmitted with new identifiers.
# Content-hash dedup must suppress it; an identity-based deduplicator would not.
_sample_one = json.loads(
    (Path(__file__).resolve().parents[1] / "sample-data" / "incoming"
     / "Sample_JSON_file1.json").read_text()
)
_resubmission = envelope(
    "FASTTRACK",
    [("discharge_summary", _sample_one["data"]["responseDetails"][0]["data"])],
    claim="CLAIM-RESUBMITTED-999",
)
FILES["duplicate_resubmission.json"] = _resubmission


# ----------------------------------------------------- NFR-3.1 (DLQ) --------
# Four distinct failure modes. Each must become one dead-letter row with a
# precise reason, and none of them may stop the run.
FILES["broken_truncated.json"] = "RAW:" + json.dumps(
    envelope("FASTTRACK", [lab([row("Haemoglobin", "13.1", "13-17", "g/dL")])])
)[:220]

FILES["broken_not_an_object.json"] = "RAW:" + json.dumps(
    ["this", "is", "an", "array", "not", "an", "envelope"]
)

FILES["broken_no_documents.json"] = {
    "traceId": uuid.uuid4().hex, "statusCode": "200", "message": ["Success"],
    "data": {"responseDetails": [], "correlationId": uuid.uuid4().hex,
             "documentId": "DOC-EMPTY", "metaDetails": [
                 {"key": "source_system", "value": "FASTTRACK"}],
             "status": True, "message": None},
}

FILES["broken_unknown_classifier.json"] = envelope("FASTTRACK", [
    ("radiology_report", {"impression": "No acute abnormality.", "modality": "CT"}),
    ("lab_report", {"basic_info": {"patient_name": "Partial Success",
                                   "reports_date": "15-10-2025"},
                    "report_details": [row("SODIUM", "141", "135-145", "mmol/L")]}),
])


# ------------------------------------------------- the misalignment case ----
# A faithful reconstruction of the defect found in Sample_JSON_file2, with a
# known unambiguous answer so the detector can be regression-tested.
#
# Construction: take a standard CBC panel in its printed order, then shift the
# NAME column one row ahead of the value columns, as an OCR row-drop does, and
# crop a character from the left edge of some names, as a page-edge crop does.
# Row i therefore carries name[i] beside the values of name[i+1].
#
#   printed panel          row  name as received        values as received
#   Haemoglobin    13-17    0   aemoglobin              4000-10000 cell/cu.mm
#   Total WBC      4000-10000
#                           1   otal WBC Count          4.50-5.50 mil/cu.mm
#   RBC            4.50-5.50
#                           2   RBC Coun                36-46 %
#   Haematocrit    36-46     3   aematocrit              83-101 FL
#   MCV            83-101    4   ean Cell Volume (MCV)   27-32 pg
#   MCH            27-32     5   MCH                     31.5-34.5 gm/dL
#   MCHC           31.5-34.5 6   MCHC                    150000-410000 cells/cu.mm
#
# Expected: every row individually incoherent, and a +1 shift explaining all
# six testable rows.
FILES["ocr_misaligned_cbc.json"] = envelope("FASTTRACK", [lab([
    row("aemoglobin", "9700", "4000-10000", "cell/cu.mm", 0, "normal"),
    row("otal WBC Count", "5.45", "4.50-5.50", "mil/cu.mm", 0, "normal"),
    row("RBC Coun", "44.9", "36-46", "%", 0, "normal"),
    row("aematocrit", "82", "83-101", "FL", 0, "normal"),
    row("ean Cell Volume (MCV)", "28.5", "27-32", "pg", 0, "normal"),
    row("MCH", "34", "31.5-34.5", "gm/dL", 0, "normal"),
    row("MCHC", "250000", "150000-410000", "cells/cu.mm", 0, "normal"),
])], claim="CLAIM-MISALIGNED")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, payload in FILES.items():
        path = OUT / name
        if isinstance(payload, str) and payload.startswith("RAW:"):
            path.write_text(payload[4:], encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"  {path.relative_to(OUT.parents[1])}")
    print(f"\n{len(FILES)} edge-case files written to {OUT}")


if __name__ == "__main__":
    main()
