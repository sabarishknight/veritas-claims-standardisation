# Assumptions

The brief says this document is a core part of the evaluation, and that a
candidate who states clear assumptions scores higher than one who makes the
same assumptions silently. So every judgement call made under ambiguity is
here, with what it would take to remove it.

Each entry: **what was assumed**, **why**, **what it costs if wrong**, and
**what production would do**.

---

## Business assumptions

### B-1. A wrong value is more expensive than a missing one
**Assumed.** In claims adjudication, a plausible-looking wrong lab value is
worse than a null, because a null stops a rule and a wrong number silently
satisfies one.

**Consequence.** The pipeline abstains wherever it cannot be sure: no value is
invented, no misalignment is auto-corrected, an ambiguous name resolves to
nothing rather than to a coin toss. This is the single assumption that shapes
the most code.

**If wrong** - if coverage mattered more than correctness - the thresholds in
`config/pipeline.yaml` move and the misalignment detector gains an
`auto_correct: true` mode that already exists as a config key. It is off by
default on purpose.

### B-2. Volume is steady-ish, not spiky within the day
**Assumed.** 200k files/day arrive roughly evenly, with the 2x burst meaning a
busy day rather than 200k files in one hour.

**Why it matters.** It is the difference between one worker and forty. Clinic
batch uploads at end of day would invalidate it.

**Production:** read the actual arrival distribution from GCS object-create
timestamps over a fortnight before sizing. Cloud Run autoscaling absorbs the
error either way; only the cost estimate moves.

### B-3. Standardised output feeds automated adjudication, not just reporting
**Assumed,** from "automated adjudication rules fail on non-numeric result
fields" in the brief.

**Consequence.** Rows that cannot be trusted are withheld from the delivered
view rather than loaded with a warning column, because a downstream rule will
not read the warning column.

### B-4. "500+ clinics" means a long tail of small ones
**Assumed.** A handful of large labs and hundreds of small ones, so onboarding
cost per clinic dominates.

**Consequence.** Zero-code onboarding is treated as the primary scaling
constraint, not throughput. Throughput turns out to need one worker; onboarding
500 clinics by writing code would need a team.

### B-5. Out of scope for a take-home
Master patient index and cross-clinic patient linkage beyond a deterministic
token; claim adjudication itself; FHIR/HL7 interchange; a UI write path
(the review queue displays decisions but accepting one is not implemented -
`learned_alias` is the table it would write to); real-time alerting into
PagerDuty.

---

## Technical assumptions

### T-1. GCS object-finalise notifications are reliable enough to be the trigger
**Assumed,** with a scheduled reconciliation sweep as the backstop: a job lists
the day's prefix and compares against `file_ledger` to catch any missed
notification. Because identity is the content hash, reprocessing a file that
*was* handled is a no-op, so the sweep can be aggressive.

### T-2. One file is small enough to process in memory
**Assumed.** The largest sample is 77 KB; the benchmark mean is 19 KB. The
ceiling is `runtime.max_file_bytes` (20 MiB), above which a file is
dead-lettered unread rather than taking out a worker.

**If wrong:** a streaming JSON parser (`ijson`) at the row level. Not worth the
dependency at the observed sizes.

### T-3. A missing unit means the value is already canonical
**Assumed** when the unit column is blank and no unit is embedded in the
result. 96 rows in the sample corpus take this path.

**Cost if wrong:** a value in the wrong scale, classified against the right
range. This is the weakest assumption in the pipeline.

**Mitigation now:** the row carries `unit_missing`, and the coherence engine
still checks the value against the test's physiological envelope, so a
wrong-scale value usually surfaces as an Outlier.

**Production:** a per-clinic, per-test default unit in the profile, learned
from the rows where that clinic *did* send a unit.

### T-4. SQLite for the prototype, BigQuery for production
**Assumed.** SQLite is in the standard library, so `make demo` runs with
nothing installed; BigQuery is the production target, and both DDLs are
generated from one column definition so they cannot drift.

**Trade-off:** DuckDB would be the better local analytical engine. It is one
dependency away, and the sink interface is the only thing that changes.

### T-5. Python, with PyYAML as the only runtime dependency
**Assumed.** Reviewer friction is a real cost, and the work is I/O- and
dict-bound rather than numeric, so NumPy/Polars would add weight without
changing the profile. The measured 0.096 CPU-seconds per 150-row file is
comfortably inside budget.

**If throughput became the constraint:** the standardisation stage is pure
functions over dicts and would vectorise into Arrow with no architectural
change.

### T-6. Cloud Run over Dataflow
**Assumed,** justified by measurement rather than preference - see
[`cost-model.md`](cost-model.md). Dataflow becomes right if the work needs
windowed joins across files or volume rises ~50x.

### T-7. LOINC codes in the catalogue are indicative, not authoritative
**Assumed and flagged.** They were hand-assigned for interoperability
plumbing and are not validated against an official LOINC release.

**Production:** load the official LOINC distribution and validate every code
in CI. Until then, downstream systems should not treat `loinc_code` as
authoritative. This is called out because a wrong LOINC code is exactly the
kind of error that propagates silently into a payor integration.

### T-8. Reference ranges are adult values
**Assumed.** Paediatric and pregnancy-specific ranges are out of scope, and the
sample corpus includes at least one child (a 16 kg patient in
`Sample_JSON_file5.json`), so **some out-of-range flags on that record are
false positives**.

**Production:** age- and sex-banded ranges in the catalogue, selected per row.
The data model already carries `age_years` and `gender`, and
`CanonicalTest.range_for(sex)` already dispatches on sex - the bands are the
missing part, not the plumbing.

### T-9. The medicine reference is illustrative
**Assumed.** 21 brands hand-built from the sample corpus. Brand-to-molecule is
a regulated mapping; entries inferred from context carry `verify: true` and
their rows are flagged `brand_unverified`.

**Production:** a licensed drug master (CIMS India, MedEx, or the payor's
formulary), with the `verify` flag reserved for genuinely new entries awaiting
pharmacist sign-off.

### T-10. Flags are a comma-separated string, not a normalised table
**Assumed** for the prototype, so a row is self-describing when an analyst
selects it directly. The UI counts them in Python rather than SQL.

**Production:** `ARRAY<STRING>` in BigQuery, which supports `UNNEST` and makes
flag counts a GROUP BY. The reason codes are already a closed enum, so the
migration is mechanical.

### T-11. The raw payload is retained inside the warehouse
**Assumed** for the prototype, because the record inspector must show the raw
JSON beside the standardised output without a GCS round trip, and an auditor
who cannot see input beside output does not really have an audit trail.

**Cost:** roughly doubles storage for data that is already durable in GCS.

**Production:** set `audit.retain_raw_payload: false` and have the UI fetch
from `file_uri` on demand. The config key exists.

---

## Data assumptions

### D-1. The supplied 79-column schema is a landing shape, not a model
**Assumed,** because it contains four spellings of the patient's age
(`age`, `age_text`, `age_years`, `basic_info_age`), three of the medicine name,
and both `course_during_hospitalisation` and `course_during_hospitalization`.
That is what a landing table looks like after a few years of accretion.

**Decision:** deliver it *exactly*, as a view (`v_ideal_output_schema`, verified
column-for-column against the supplied CSV by a test), over a narrow model that
can take 500 more clinics. Every duplicate spelling is populated from the one
canonical field.

### D-2. `source_system` identifies the clinic, not `ConsumerClientId`
**Assumed.** `source_system` takes two values across the corpus
(`FASTTRACK`, `ARTEMIS`) while `ConsumerClientId` is unique per file, so it
reads as a request identifier rather than a tenant.

**If wrong,** profile selection keys on the wrong field. It is a one-line
change in `match:` and the pipeline would keep working meanwhile, because an
unmatched source falls back to `_default`.

### D-3. Transport fields may be excluded from the duplicate hash
**Assumed:** `traceId`, `correlationId`, `documentId`, `claim_no`, `nt_code`
are transport, and two payloads identical apart from these are the same report.

**Evidence:** files 1 and 3 are exactly that.

**Risk:** if a clinic legitimately submits the same report under two claims -
say for two policies - this suppresses the second. Mitigated by
`v_duplicates`, which lists every suppression with the record it merged into,
so the behaviour is visible rather than silent. The strategy is per clinic.

### D-4. Patient sex is unavailable, so sex-specific ranges fall back
**Observed.** Every identifier in the samples is redacted, including sex, so
`range_for(sex)` resolves to the widened `ANY` band.

**Consequence:** slightly lower classification accuracy on the ~12 tests with
sex-specific ranges. Production traffic with sex present gets the tighter band
for free.

### D-5. The misalignment is an OCR artefact, not a semantic convention
**Assumed,** from the evidence in [`data-profile.md`](data-profile.md) F-1: the
names form the standard CBC panel in printed order, the values form the same
panel one step later, and the names are character-cropped at the left edge.

**This is the assumption I would most want a domain expert to confirm**, which
is precisely why the pipeline flags and proposes rather than realigning. If the
reading is right, a human accepts the offset in one click. If it is wrong,
nothing has been corrupted.

### D-6. Qualitative results are findings, not failures
**Assumed.** `POSITIVE` for a Widal test is the answer; it simply has no
numeric band. Classified `Not Applicable`, not `Invalid`.

### D-7. `Not Applicable` is a legitimate sixth classification
**Added** to FR-3.3's five values. When no reference range exists from either
the source or the catalogue, asserting "Within Range" would be a fabrication.

**Trade-off:** downstream consumers expecting exactly five values must handle
a sixth. Flagged here rather than silently emitting a wrong "Within Range".

### D-8. Repeat rows with identical values are OCR artefacts; with different
values they are serial draws
**Assumed.** `result` is in the collapse key, so a falling haemoglobin survives
and a six-times-repeated identical panel does not.

### D-9. Panel headings are not tests
**Assumed.** `COMPLETE BLOOD COUNT (CBC)`, `RBC INDICES`,
`LIVER FUNCTION TEST (LFT)` are panel names. Some carry a value in the sample
data - which is itself the misalignment, since the value belongs to the
panel's first analyte - so they are dead-lettered with reason `panel_heading`
rather than attributed to the heading.

### D-10. What a production run would need to know before go-live
- The real arrival distribution, for sizing (B-2).
- Per-clinic default units, to retire T-3.
- An official LOINC release (T-7) and a licensed drug master (T-9).
- Age- and sex-banded reference ranges (T-8).
- Whether `claim_no` may legitimately differ for the same report (D-3).
- A domain expert's confirmation of the misalignment reading (D-5).

---

## Scope exclusions

| Excluded | Why | What including it would take |
|---|---|---|
| Real GCS/Pub/Sub wiring in the prototype | A reviewer must be able to run it with no cloud account | Terraform in `infra/` applies it; the source interface already abstracts it |
| Streaming (sub-minute) latency | The SLO is 15 minutes; streaming costs more and buys nothing | Swap the micro-batch for per-message processing; same code |
| A write path in the review UI | Displaying decisions proves the design; persisting them is CRUD | A POST handler writing `learned_alias` and `review_item.status` |
| Authentication on the UI | It is a localhost prototype | IAP in front of the Cloud Run service - in `infra/` |
| Full LOINC / drug master | Licensing, and neither changes the architecture | Load the distributions into the existing config shape |
| Paediatric reference ranges | Needs clinical sign-off | Age bands in `test_catalog.yaml`; dispatch already exists |
| Horizontal autoscaling proof | Cannot be demonstrated on one machine | Measured per-worker rate x instances; the stage is stateless |
| A normalised flag table | Prototype readability (T-10) | `ARRAY<STRING>` and a schema migration |
