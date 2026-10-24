# Non-functional requirements: notes and references

The brief asks for NFR-1 to NFR-5 to be addressed "via diagrams and
tech/concept notes describing the tools, technology and approach". This is
that document. Numbers quoted as *measured* come from
`python -m veritas bench`; the raw result is `bench/results.json`.

---

## NFR-1. Scale and performance

### NFR-1.1 Throughput: 200,000 files/day, 2x burst

**Measured** on an 8,000-file synthetic corpus (150 rows/file, 19 KiB mean,
defects injected at the observed rates), 4 worker processes on commodity
hardware. Raw result: `bench/results.json`.

| metric | processing only | including the local SQLite write |
|---|---:|---:|
| files/second | **66.1** | 37.8 |
| per worker | **16.5** | 9.5 |
| CPU-seconds per file | **0.061** | 0.106 |
| rows/second | **9,879** | 5,655 |

The left column is the one that sizes Cloud Run, because in production the
write is a **free, asynchronous BigQuery load job** rather than a synchronous
local insert. The right column is reported anyway, because quoting only the
favourable number would be dishonest - and because it is a real finding: with
the audit payload retained, a 25,000-file corpus produced a 5.2 GB SQLite file
and the write dominated the measurement entirely. That is what motivates
`audit.retain_raw_payload: false` at production volume (assumption T-11).

200,000 files/day is **2.31 files/second** steady, 4.62 at 2x burst. At 16.5
files/second per worker, **one Cloud Run instance covers both** with 7x
headroom. A full day's volume processes in **50 minutes** at the measured rate.
The capacity plan provisions min-instances 1 / max 10 for cold-start removal
and zone-failure tolerance, not for throughput.

This is the number that drives the Cloud Run versus Dataflow decision. A
Dataflow streaming job cannot scale below one always-on worker plus a shuffle
service; Cloud Run scales to zero between bursts. See
[`cost-model.md`](cost-model.md).

### NFR-1.2 Latency: p95 under 15 minutes, landing to queryable

Budget, per object:

| stage | budget | why |
|---|---|---|
| GCS finalise -> Pub/Sub delivery | < 5 s | notification latency, p99 |
| Pub/Sub -> Cloud Run push | < 5 s | push subscription, no polling |
| parse + standardise + validate | ~0.1 s | measured |
| micro-batch accumulation | <= 60 s | `runtime.batch_size`, tunable |
| BigQuery load job + MERGE | 10-90 s | load jobs are free; this is the cost |
| **total p95** | **< 3 min** | 5x inside the 15-minute SLO |

The headroom is deliberate: it absorbs a BigQuery load-job queue without
breaching the SLO. The micro-batch window is the only knob that trades latency
for cost, and at 60 seconds the cost is already negligible.

Measured as a log-based metric: `processed_at - ingested_at` per row, alerting
at `observability.lag_alert_seconds` (900).

### NFR-1.3 Horizontal scalability

The processing stage is **stateless and partitioned by file**, so adding
workers adds throughput linearly. Nothing is shared between workers except the
read-only config and the read-only learned-alias cache seeded at run start.

Two pieces of state are deliberately centralised in the parent process:
duplicate detection across workers, and the database write. Workers return
rows; the parent writes them. That keeps the idempotency argument simple (one
writer, deterministic keys, no distributed transaction) and the write is not
the bottleneck at this volume.

**Where it stops scaling:** around 2M files/day the single-writer MERGE becomes
the constraint, and the loader moves to partition-scoped MERGEs running per
clinic in parallel. The architecture does not otherwise change.

---

## NFR-2. Clinic onboarding

### NFR-2.1 Zero-code onboarding — *implemented and tested*

Everything clinic-specific is configuration:

| file | holds |
|---|---|
| `config/clinics/<clinic>.yaml` | envelope paths, field candidate lists, date formats, interpretation semantics, dedup strategy, quality SLOs |
| `config/test_catalog.yaml` | canonical tests, aliases, units, reference ranges, physiological envelopes, LOINC |
| `config/units.yaml` | dimensions and conversion factors |
| `config/medicines.yaml` | brand -> molecule |

Profiles **inherit** from `_default`, so a new clinic is typically a dozen
lines. `sample-data/edge-cases/unprofiled_clinic.json` uses entirely different
field spellings (`patientName`, `testName`, `reference_range`, `units`) and a
source system with no profile at all; it loads end to end under `_default`, and
`tests/test_pipeline.py::test_nfr_2_1_an_unprofiled_clinic_flows_end_to_end`
asserts it.

### NFR-2.2 Onboardable within one business day

The bottleneck in practice is not writing the YAML, it is *knowing what you
missed*. `python -m veritas onboard <file>` prints a field-coverage report:
every path in the clinic's JSON that the profile does **not** read.

That list is the onboarding checklist. Each entry is either mapped or
consciously dropped, and either way a human saw it - rather than discovering
six months later in a data-quality review that a field was silently ignored.

Day-one sequence: files land -> `_default` applies -> rows load tagged
`clinic_profile=_default` -> the UI shows unprofiled volume -> engineer runs
`onboard` -> commits a profile. At no point is the clinic blocked.

### NFR-2.3 Schema versioning

Each profile carries `schema_version`, written onto every row. A clinic that
changes format gets a second profile with a narrower `match:` block and a
higher priority; both versions process concurrently and rows are attributable
to the version that produced them.

Unmapped envelope metadata keys are recorded, not dropped, and raise a review
item - usually the first visible sign that a clinic changed its format.

---

## NFR-3. Reliability and resilience

### NFR-3.1 Fault tolerance — *implemented and tested*

**A failure is a row, not an exception.** Every stage catches, records a
dead-letter entry with a reason code from a closed vocabulary, and continues.
`runtime.fail_fast` exists for development and defaults to false.

Failure modes covered, each with its own reason code and its own test fixture:

| failure | reason code | fixture |
|---|---|---|
| truncated JSON | `malformed_json` | `broken_truncated.json` |
| wrong top-level type | `not_an_object` | `broken_not_an_object.json` |
| empty document array | `no_documents` | `broken_no_documents.json` |
| classifier with no shape | `unknown_classifier` | `broken_unknown_classifier.json` |
| source marked the record failed | `source_marked_failed` | synthetic |
| oversized file | `empty_or_oversized_file` | config ceiling |
| a row that is not a test | `panel_heading`, `header_row_leaked`, ... | the real samples |

`broken_unknown_classifier.json` is the interesting one: it carries an unknown
`radiology_report` *and* a valid `lab_report`. The unknown one is dead-lettered
and the valid one loads. Partial success is the correct behaviour.

### NFR-3.2 Idempotency — *implemented and tested*

Every primary key is a deterministic content hash of
`(document identity, canonical test, occurrence index)`. Loading is therefore a
MERGE that rewrites identical rows with identical values. There is no
read-before-write and no "have I seen this" bookkeeping, which is what makes
the property hold under parallel workers and after a crash mid-batch.

`python -m veritas verify` runs the corpus twice and compares row counts and a
content checksum. `tests/test_pipeline.py::IdempotencyTests` asserts it, and
`ParallelConsistencyTests` asserts that 1 worker and 3 workers produce
byte-identical output.

### NFR-3.3 Availability 99.5%, maintenance under 2h/month

Cloud Run is regional and multi-zone by default. Pub/Sub retains undelivered
messages for 7 days with exponential backoff, so a complete processing outage
of up to a week is a delay, not data loss. There is no in-place migration step
that would require a maintenance window: config changes ship as a new revision,
and schema changes are additive columns. A revision roll is zero-downtime and
gives a clean before/after boundary in the run ledger.

---

## NFR-4. Data quality and governance

### NFR-4.1 98% of test-name variants resolved within 30 days

Not a hope - a ratchet. Every novel spelling is reasoned about **once**:

```
dictionary miss -> fuzzy -> LLM (batched, capped) -> human review queue
                                 |                         |
                                 +--> learned_alias <-------+
                                           |
                                    seeds every later run
```

A spelling costs one decision and is then free forever, so the unresolved tail
decays monotonically. The rate is measured per source in `v_clinic_quality`
(`unmapped_rate`) against the SLO in each clinic profile, and the UI shows a
breach as a tag on that clinic's row.

The resolver's own counters (`exact`, `alias`, `fuzzy`, `fuzzy_affix`, `llm`,
`unmapped`) are written to `pipeline_run.resolver_stats`, so the 30-day trend
is a query, not an estimate.

### NFR-4.2 Lineage — *implemented and tested*

Twelve lineage columns on **every** fact row: `file_uri`, `file_sha256`,
`document_id`, `trace_id`, `correlation_id`, `source_system`,
`clinic_profile`, `schema_version`, `ingested_at`, `processed_at`, `run_id`,
`source_row_index` (plus `page_no` where known). `v_lineage` joins row ->
file ledger -> run in one hop.

`trace_id` and `correlation_id` are **adopted from the source**, not minted, so
a trace started in the clinic's gateway survives the whole journey. One query
in Cloud Logging returns everything that happened to one record:

```
jsonPayload.correlation_id="b1458a7fe2ddad8233334cabc2c416c1"
```

`pipeline_run.config_fingerprint` ties output to the configuration that
produced it - necessary because the same file reprocessed after a catalogue
change can legitimately resolve differently, and an analyst needs to see why.

A test asserts that no row can exist without lineage.

### NFR-4.3 PII handling — *implemented and tested*

Identifiers are replaced by keyed **HMAC-SHA256** tokens: deterministic (so
patients link across clinics and months), not reversible, and not confirmable
against a guessed name without the key. A plain SHA-256 would fail that last
test - the space of Indian patient names is small enough to enumerate.

The key lives in Secret Manager in production, an environment variable locally,
and the pipeline logs a loud warning when it falls back to the development key.
Redaction markers already present in the source map to `None` rather than being
tokenised - hashing the marker would create one very popular token that looks
like a single extremely busy patient.

A test asserts that no patient name reaches the warehouse.

---

## NFR-5. Observability

### NFR-5.1 Metrics and alerting

Every run writes a `pipeline_run` row: files, rows, flags, invalids, outliers,
duplicates, errors, review items, LLM calls and cache hits, duration, worker
count, config fingerprint, and the resolver's tier counters.

Alert policies (`infra/monitoring.tf`), both named in the brief:

| alert | condition | why it matters |
|---|---|---|
| error rate | file failures > 1% over 5 min | a clinic's feed broke |
| processing lag | `processed_at - ingested_at` p95 > 900 s | the SLO is at risk |
| unmapped-name spike | `unmapped_rate` > profile SLO | a clinic changed its catalogue |
| misalignment detected | any `report_misalignment_suspected` | an OCR pipeline is corrupting data |
| dead-letter spike | DLQ writes > 3x the 7-day baseline | something systemic |

The last two matter more than the first two. An error rate alert fires when
something is obviously broken; a misalignment alert fires when everything looks
fine and the data is wrong.

`GET /api/health` returns the current error rate against its threshold, for
Cloud Run's health check and for uptime monitoring.

### NFR-5.2 Structured logging with correlation IDs

Every log line is one JSON object carrying `run_id`, `trace_id`,
`correlation_id` and `file_sha256` where known - the shape Cloud Logging
indexes natively, so these become queryable fields rather than text. Context
is attached by a context manager, so a line emitted deep in row processing
still carries the file it came from.

Log level and format are config, overridable by environment variable, so a
worker can be turned up to DEBUG for one revision without a code change.
