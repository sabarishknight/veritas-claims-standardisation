"""Synthetic corpus generator and throughput benchmark.

Why this exists
---------------
NFR-1.1 asks for 200,000 files a day with a 2x burst, and NFR-1.2 for a
15-minute p95. Those are answerable with arithmetic only if the per-file cost
is measured rather than guessed, so this module generates a corpus at scale,
runs the real pipeline over it, and turns the measurement into the two numbers
that actually matter: how many workers the SLO needs, and what they cost.

The generated files carry the same defects as the real corpus - OCR-cropped
names, mixed units, packed cells, duplicate panels, the occasional malformed
file - so the benchmark exercises the expensive paths (fuzzy matching,
coherence checks) rather than a happy path that would flatter the result.

``python -m veritas bench --files 200000 --workers 8``
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .load.sqlite_sink import SqliteSink
from .observe.logging import configure, get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------
# Cost model inputs. Published Google Cloud list prices, asia-south1, at the
# time of writing. Stated here rather than buried so a reviewer can substitute
# their own committed-use or regional rates and re-run the arithmetic.
# --------------------------------------------------------------------------
CLOUD_RUN_VCPU_SECOND_USD = 0.000024
CLOUD_RUN_GIB_SECOND_USD = 0.0000025
CLOUD_RUN_REQUEST_USD = 0.0000004
PUBSUB_PER_TIB_USD = 40.0
GCS_CLASS_A_PER_1000_USD = 0.005
GCS_STORAGE_GIB_MONTH_USD = 0.023
BIGQUERY_ACTIVE_STORAGE_GIB_MONTH_USD = 0.02
WORKER_MEMORY_GIB = 1.0

_TESTS = [
    ("HAEMOGLOBIN", "13-17", "g/dL", (9.0, 18.0)),
    ("TOTAL WBC COUNT", "4000-10000", "cells/cu.mm", (3000, 15000)),
    ("RBC COUNT", "4.5-5.5", "mil/cu.mm", (3.5, 6.2)),
    ("PCV", "40-50", "%", (30.0, 55.0)),
    ("MCV", "83-101", "fL", (70.0, 110.0)),
    ("MCH", "27-32", "pg", (22.0, 36.0)),
    ("MCHC", "31.5-34.5", "gm/dL", (29.0, 37.0)),
    ("PLATELET COUNT", "150000-410000", "cells/cu.mm", (80000, 450000)),
    ("NEUTROPHILS", "40-70", "%", (30.0, 85.0)),
    ("LYMPHOCYTES", "20-40", "%", (10.0, 55.0)),
    ("SODIUM", "135-145", "mmol/L", (125.0, 150.0)),
    ("POTASSIUM", "3.5-5.1", "mmol/L", (2.8, 6.2)),
    ("CHLORIDE", "98-107", "mmol/L", (90.0, 115.0)),
    ("Serum Creatinine", "0.7-1.3", "mg/dL", (0.4, 3.5)),
    ("UREA", "19-43", "mg/dL", (10.0, 90.0)),
    ("URIC ACID", "3.5-7.2", "mg/dL", (2.0, 9.0)),
    ("CALCIUM", "8.6-10.3", "mg/dL", (7.5, 11.5)),
    ("ALBUMIN", "3.5-5.2", "g/dL", (2.5, 5.5)),
    ("GLOBULIN", "2.0-3.5", "g/dL", (1.5, 4.5)),
    ("ALT (SGPT)", "<50", "U/L", (10.0, 180.0)),
    ("AST (SGOT)", "17-59", "U/L", (12.0, 200.0)),
    ("ALKALINE PHOSPHATASE", "38-126", "U/L", (30.0, 220.0)),
    ("BILIRUBIN TOTAL", "0-1.2", "mg/dL", (0.1, 4.0)),
    ("CRP", "0-6", "mg/L", (0.5, 120.0)),
]

# Defects injected at the observed rates, so the benchmark exercises the
# expensive code paths rather than a happy path.
_CROPS = {"HAEMOGLOBIN": "aemoglobin", "PLATELET COUNT": "atelet Count",
          "NEUTROPHILS": "eutrophils", "LYMPHOCYTES": "mphocytes",
          "TOTAL WBC COUNT": "tal WBC Count"}
_UNIT_VARIANTS = {"g/dL": ["g/dl", "gm/dl", "gm/di"],
                  "cells/cu.mm": ["cell/cu.mm", "cells/cumm", "Cells/cu.mm"],
                  "mil/cu.mm": ["Million/cu.mm", "mil/cu.cm"],
                  "fL": ["FL", "fl", "FI"], "U/L": ["U/l", "UIL"]}


class NullSink:
    """Counts rows without persisting them.

    The capacity question - how many Cloud Run workers does 200k files/day
    need - is a question about *processing* cost, because in production the
    write is a free BigQuery load job that happens asynchronously in batches.
    Measuring processing against a local SQLite file answers a different
    question: it measures SQLite. With the audit payload retained, the local
    database reached 5.2 GB and 3.7M rows on a 25k-file corpus, and the write
    dominated the measurement.

    So the benchmark reports both, labelled, and the capacity projection uses
    the processing figure. `--sink sqlite` measures the full local path for
    comparison.
    """

    def __init__(self) -> None:
        self.documents = self.rows = self.medications = self.errors = 0

    def initialise(self, canonical_tests) -> None: ...
    def rebuild_views(self, canonical_tests) -> None: ...
    def load_learned_aliases(self) -> dict: return {}
    def known_content_hashes(self) -> dict: return {}
    def seen_file_hashes(self) -> set: return set()
    def commit(self) -> None: ...
    def close(self) -> None: ...
    def write_run(self, summary, **kw) -> None: ...
    def write_learned_aliases(self, rows) -> int:  # noqa: ARG002
        return 0

    def write_documents(self, documents) -> int:
        count = len(list(documents))
        self.documents += count
        return count

    def write_lab_results(self, results) -> int:
        count = len(list(results))
        self.rows += count
        return count

    def write_medications(self, medications) -> int:
        count = len(list(medications))
        self.medications += count
        return count

    def write_errors(self, errors) -> int:
        count = len(list(errors))
        self.errors += count
        return count

    def write_review_items(self, items) -> int: return len(list(items))
    def write_file_ledger(self, entries) -> int: return len(list(entries))


@dataclass
class BenchmarkResult:
    files: int
    rows_per_file: int
    workers: int
    generate_seconds: float
    run_seconds: float
    files_per_second: float
    rows_per_second: float
    rows_emitted: int
    documents_emitted: int
    errors_emitted: int
    bytes_total: int
    mean_file_bytes: float
    cpu_seconds_per_file: float
    sink: str
    projection: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row(rng: random.Random, index: int) -> dict[str, Any]:
    name, ref, unit, (low, high) = _TESTS[index % len(_TESTS)]
    value = round(rng.uniform(low, high), 2)

    roll = rng.random()
    if roll < 0.04 and name in _CROPS:
        name = _CROPS[name]                       # OCR left-crop
    if roll > 0.93 and unit in _UNIT_VARIANTS:
        unit = rng.choice(_UNIT_VARIANTS[unit])   # unit spelling drift
    result: Any = value
    if roll > 0.96:
        result = f"{value} {unit}"                # inline unit
    elif roll > 0.94:
        result = f"{value:,}".replace(".0", "")   # thousands comma
    elif 0.015 < roll < 0.025:
        result = ref                              # range leaked into result
    elif roll < 0.008:
        result = "OTE"                            # OCR debris
    return {"page_no": index // 20, "range": ref, "result": str(result),
            "test_analytics": rng.choice(["normal", "low", "high", ""]),
            "test_name": name, "unit": unit}


def generate_corpus(root: Path, files: int, rows_per_file: int = 150,
                    seed: int = 20261024) -> tuple[int, float]:
    """Write ``files`` synthetic envelopes under ``root``.

    Laid out as ``clinic_id=<src>/dt=<date>/<n>.json`` to mirror the GCS
    partitioning the architecture assumes, so the discovery path is exercised
    too.
    """
    rng = random.Random(seed)
    started = time.perf_counter()
    root.mkdir(parents=True, exist_ok=True)
    total_bytes = 0

    for i in range(files):
        source = "FASTTRACK" if i % 3 else "ARTEMIS"
        day = f"2026-10-{(i % 28) + 1:02d}"
        folder = root / f"clinic_id={source}" / f"dt={day}"
        if i % 500 == 0 or not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)

        rows = [_row(rng, j + i) for j in range(rows_per_file)]
        # Duplicate panels across pages, as multi-page OCR produces.
        if rng.random() < 0.25:
            rows.extend(rows[: rows_per_file // 4])

        payload = {
            "traceId": f"{i:032x}",
            "statusCode": "200",
            "message": ["Success"],
            "data": {
                "responseDetails": [
                    {"classifier": "lab_report", "status": "success",
                     "errorMessage": None, "fileId": None,
                     "data": {
                         "basic_info": {
                             "uhid": f"UH{i:08d}", "age": f"{rng.randint(1, 92)} Years",
                             "bill_date": f"{(i % 28) + 1:02d}-10-2026",
                             "gender": rng.choice(["M", "F", "Male", "Female"]),
                             "lab_or_hospital_name": f"Synthetic Lab {i % 500}",
                             "patient_name": f"Patient {i}",
                             "reports_date": f"{(i % 28) + 1:02d}-10-2026",
                         },
                         "report_details": rows,
                     }},
                    {"classifier": "discharge_summary", "status": "success",
                     "errorMessage": None, "fileId": None,
                     "data": {
                         "admissionDate": f"{(i % 28) + 1:02d}-10-2026",
                         "dischargeDate": f"{(i % 28) + 2:02d}-10-2026",
                         "age": f"{rng.randint(1, 92)} Years",
                         "gender": rng.choice(["M", "F"]),
                         "patientName": f"Patient {i}",
                         "doctorName": f"Dr. Synthetic {i % 90}",
                         "hospitalName": f"Synthetic Hospital {i % 500}",
                         "hospitalAddress": "Synthetic Road",
                         "briefHistory": "Synthetic admission for benchmarking.",
                         "diagnosis": rng.choice(
                             ["Acute febrile illness", "Enteric fever",
                              "Acute gastroenteritis", "Dengue fever"]),
                         "generalExaminations": "Temp 100.4F, PR 92/min, BP 120/80 mmHg",
                         "recommendations": "Review in one week.",
                         "ward": "GENERAL", "postDischargeAdvice": None,
                         "courseDuringHospitalisation": [],
                         "dischargeMedications": [
                             {"medicine": m, "dose": "1 TAB",
                              "frequency": "12 HOURLY", "type": None}
                             for m in rng.sample(
                                 ["TAB. PAN 40", "Tab. Dolo 650", "CAP. PAN D",
                                  "TAB CEFTUM 500 MG", "INJ. MONOCEF 1 GM",
                                  "SYP DUPHALAC", "Inj. Emeset", "TAB. AZITHRAL 500 mg"],
                                 k=rng.randint(2, 5))
                         ],
                         "medicineInjectionsInvestigation": ["INJ PANTOP 40 MG IV OD"],
                     }},
                ],
                "correlationId": f"{i:032x}",
                "documentId": f"DOCBENCH{i:024d}",
                "metaDetails": [
                    {"key": "DestinationIdentifier", "value": "stg-datalake"},
                    {"key": "claim_no", "value": f"CLAIM-{i:09d}"},
                    {"key": "nt_code", "value": "NT-BENCH"},
                    {"key": "source_system", "value": source},
                    {"key": "ConsumerClientId", "value": f"{i:032x}"},
                ],
                "status": True, "message": None,
            },
        }

        text = json.dumps(payload, separators=(",", ":"))
        # One malformed file per thousand, so the DLQ path is measured too.
        if i % 1000 == 999:
            text = text[: len(text) // 2]
        path = folder / f"{i:09d}.json"
        path.write_text(text, encoding="utf-8")
        total_bytes += len(text)

    return total_bytes, time.perf_counter() - started


def project(files_per_second: float, workers: int, mean_file_bytes: float,
            daily_files: int = 200_000) -> dict[str, Any]:
    """Turn a measured rate into capacity and cost.

    The question NFR-1.1 and NFR-1.2 actually ask is "how many workers, and
    what do they cost", so that is what this returns rather than a raw rate.
    """
    per_worker = files_per_second / workers if workers else files_per_second
    cpu_seconds_per_file = 1.0 / per_worker if per_worker else float("inf")

    steady_rate = daily_files / 86_400.0
    burst_rate = steady_rate * 2
    workers_steady = max(1, -(-steady_rate // per_worker)) if per_worker else None
    workers_burst = max(1, -(-burst_rate // per_worker)) if per_worker else None

    # Latency: queue wait is near zero when provisioned for the arrival rate,
    # so end-to-end is dominated by the batching window plus per-file work.
    seconds_for_daily_volume = daily_files / files_per_second if files_per_second else None

    cpu_seconds_per_day = daily_files * cpu_seconds_per_file
    compute_usd_per_month = (
        cpu_seconds_per_day * 30
        * (CLOUD_RUN_VCPU_SECOND_USD + WORKER_MEMORY_GIB * CLOUD_RUN_GIB_SECOND_USD)
    )
    request_usd_per_month = daily_files * 30 * CLOUD_RUN_REQUEST_USD
    gcs_ops_usd_per_month = daily_files * 30 / 1000 * GCS_CLASS_A_PER_1000_USD
    daily_gib = daily_files * mean_file_bytes / (1024 ** 3)
    pubsub_usd_per_month = daily_gib * 30 / 1024 * PUBSUB_PER_TIB_USD
    gcs_storage_usd_per_month = daily_gib * 30 * GCS_STORAGE_GIB_MONTH_USD
    bq_storage_usd_per_month = daily_gib * 30 * 0.6 * BIGQUERY_ACTIVE_STORAGE_GIB_MONTH_USD

    total = (compute_usd_per_month + request_usd_per_month + gcs_ops_usd_per_month
             + pubsub_usd_per_month + gcs_storage_usd_per_month + bq_storage_usd_per_month)

    return {
        "daily_files": daily_files,
        "measured_files_per_second_total": round(files_per_second, 1),
        "measured_files_per_second_per_worker": round(per_worker, 1),
        "cpu_seconds_per_file": round(cpu_seconds_per_file, 5),
        "steady_arrival_files_per_second": round(steady_rate, 2),
        "workers_needed_steady": int(workers_steady) if workers_steady else None,
        "workers_needed_2x_burst": int(workers_burst) if workers_burst else None,
        "seconds_to_process_a_full_day_at_measured_rate":
            round(seconds_for_daily_volume, 1) if seconds_for_daily_volume else None,
        "daily_ingest_gib": round(daily_gib, 2),
        "monthly_usd": {
            "cloud_run_compute": round(compute_usd_per_month, 2),
            "cloud_run_requests": round(request_usd_per_month, 2),
            "gcs_operations": round(gcs_ops_usd_per_month, 2),
            "pubsub": round(pubsub_usd_per_month, 2),
            "gcs_storage": round(gcs_storage_usd_per_month, 2),
            "bigquery_storage": round(bq_storage_usd_per_month, 2),
            "total": round(total, 2),
        },
        "price_assumptions_usd": {
            "cloud_run_vcpu_second": CLOUD_RUN_VCPU_SECOND_USD,
            "cloud_run_gib_second": CLOUD_RUN_GIB_SECOND_USD,
            "worker_memory_gib": WORKER_MEMORY_GIB,
            "pubsub_per_tib": PUBSUB_PER_TIB_USD,
            "note": "Google Cloud list prices, asia-south1, at time of writing. "
                    "BigQuery load jobs are free, which is why the pipeline uses "
                    "load jobs rather than the Storage Write API.",
        },
    }


def run_benchmark(args: Any) -> int:
    """``python -m veritas bench``"""
    from .pipeline import Pipeline

    configure(level="ERROR", fmt="text")
    config = load_config(getattr(args, "config_dir", None))
    files = int(args.files)
    rows_per_file = int(args.rows_per_file)
    workers = int(args.workers) or (__import__("os").cpu_count() or 1)

    workdir = Path(tempfile.mkdtemp(prefix="veritas-bench-"))
    corpus = workdir / "corpus"
    db = workdir / "bench.db"

    print(f"generating {files:,} files x ~{rows_per_file} rows ...", flush=True)
    total_bytes, generate_seconds = generate_corpus(corpus, files, rows_per_file)
    mean_bytes = total_bytes / files if files else 0
    print(f"  {total_bytes / 1024 ** 2:,.0f} MiB in {generate_seconds:,.1f}s "
          f"(mean {mean_bytes / 1024:,.1f} KiB/file)", flush=True)

    sink_kind = str(getattr(args, "sink", "none"))
    print(f"running the pipeline with {workers} worker(s), sink={sink_kind} ...", flush=True)
    sink = SqliteSink(db) if sink_kind == "sqlite" else NullSink()
    try:
        started = time.perf_counter()
        summary = Pipeline(config, sink, config_dir=getattr(args, "config_dir", None)).run(
            str(corpus), workers=workers)
        elapsed = time.perf_counter() - started
    finally:
        sink.close()

    files_per_second = summary.files_processed / elapsed if elapsed else 0.0
    rows_per_second = summary.rows_emitted / elapsed if elapsed else 0.0

    result = BenchmarkResult(
        files=files, rows_per_file=rows_per_file, workers=workers,
        generate_seconds=round(generate_seconds, 2),
        run_seconds=round(elapsed, 2),
        files_per_second=round(files_per_second, 1),
        rows_per_second=round(rows_per_second, 1),
        rows_emitted=summary.rows_emitted,
        documents_emitted=summary.documents_emitted,
        errors_emitted=summary.errors_emitted,
        bytes_total=total_bytes,
        mean_file_bytes=round(mean_bytes, 1),
        cpu_seconds_per_file=round(workers / files_per_second, 5) if files_per_second else 0.0,
        sink=sink_kind,
        projection=project(files_per_second, workers, mean_bytes),
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")

    _print_report(result)
    print(f"\nwritten to {out}")
    if not getattr(args, "keep_data", False):
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"corpus kept at {workdir}")
    return 0


def _print_report(result: BenchmarkResult) -> None:
    p = result.projection
    money = p["monthly_usd"]
    print()
    print("=" * 74)
    print("THROUGHPUT")
    print("=" * 74)
    print(f"  files processed        {result.files:,} in {result.run_seconds:,.1f}s")
    print(f"  workers                {result.workers}")
    print(f"  sink                   {result.sink}"
          + ("   (processing cost only; production writes are BigQuery load jobs)"
             if result.sink == "none" else "   (includes the local SQLite write)"))
    print(f"  files/second           {result.files_per_second:,.1f} "
          f"({p['measured_files_per_second_per_worker']:,.1f} per worker)")
    print(f"  rows/second            {result.rows_per_second:,.0f}")
    print(f"  CPU-seconds per file   {result.cpu_seconds_per_file:.4f}")
    print()
    print("=" * 74)
    print("CAPACITY FOR 200,000 FILES/DAY  (NFR-1.1, NFR-1.2)")
    print("=" * 74)
    print(f"  arrival rate           {p['steady_arrival_files_per_second']:.2f} files/s "
          f"steady, {p['steady_arrival_files_per_second'] * 2:.2f} at 2x burst")
    print(f"  workers needed         {p['workers_needed_steady']} steady, "
          f"{p['workers_needed_2x_burst']} at 2x burst")
    print(f"  a full day's volume    processes in "
          f"{p['seconds_to_process_a_full_day_at_measured_rate'] / 60:,.1f} minutes "
          f"at the measured rate")
    print(f"  daily ingest           {p['daily_ingest_gib']:,.1f} GiB")
    print()
    print("=" * 74)
    print("ESTIMATED MONTHLY COST (USD, list price)")
    print("=" * 74)
    for key in ("cloud_run_compute", "cloud_run_requests", "gcs_operations",
                "pubsub", "gcs_storage", "bigquery_storage"):
        print(f"  {key:24} {money[key]:>10,.2f}")
    print(f"  {'TOTAL':24} {money['total']:>10,.2f}")
    print()
    print("  BigQuery load jobs are free; the pipeline uses load jobs plus MERGE")
    print("  rather than the Storage Write API, which is billed per GiB.")
