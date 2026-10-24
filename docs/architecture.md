# Solution architecture

Veritas Claims standardisation pipeline. Two pages, as asked. The
requirement-by-requirement detail is in [`nfr-notes.md`](nfr-notes.md), the
reasoning under uncertainty is in [`assumptions.md`](assumptions.md), and the
arithmetic behind the cost claims is in [`cost-model.md`](cost-model.md).

![Solution architecture](diagrams/architecture.svg)

---

## 1. The problem this design is actually solving

The brief describes a normalisation problem: many clinics, many spellings, one
canonical schema. The sample data contains a harder one.

In `Sample_JSON_file2.json` a row named `aemoglobin` carries a reference range
of `4000-10000 cell/cu.mm` and a result of `9700`. The name is a
character-cropped haemoglobin; the values are a total white cell count. The OCR
dropped a row and shifted the name column against the value columns. The same
pattern appears in a second file from a different source system.

A fuzzy matcher resolves that name correctly, at 0.97 confidence, and loads a
white cell count into a haemoglobin column. Nothing downstream can tell: the
test exists, the value is a number, the row is complete. Automated adjudication
then acts on it.

So the design goal is not "map strings to strings". It is **never let an
unverifiable value reach a claim decision**, while keeping the 97% of rows that
are fine flowing without friction. Everything below follows from that. The full
evidence is in [`data-profile.md`](data-profile.md).

---

## 2. Layers

### Ingestion (FR-1)

Clinics write JSON to a GCS bucket partitioned
`clinic_id=<src>/dt=<YYYY-MM-DD>/`. Object-finalise notifications go to Pub/Sub,
which push-delivers to a Cloud Run service - one message, one object. The same
code runs over a local directory, which is how the prototype, the tests and the
benchmark execute.

Identity is established at the boundary: the file's SHA-256 is computed once,
before anything can mutate the payload, and becomes both the idempotency key
and the lineage anchor.

**Why Pub/Sub push to Cloud Run rather than Dataflow.** 200,000 files/day is
2.3 files/second, bursting to 4.6. The measured per-file cost is 0.096 CPU
seconds, so **one worker covers steady state and the 2x burst**
([`cost-model.md`](cost-model.md)). A Dataflow streaming job cannot scale below
one always-on worker and carries a shuffle service; Cloud Run scales to zero
between bursts and costs roughly $15/month of compute for this volume. Dataflow
would be the right answer at 50x the volume or if the work needed windowed
joins across files. It does not.

### Processing (FR-2, FR-3)

Per file: parse envelope -> route documents by `classifier` -> suppress
duplicates -> standardise rows -> check coherence -> classify -> write.

The envelope is read **schema-on-read**: field *locations* are config paths
(`data.responseDetails[*]`) and field *names* are ordered candidate lists
(`[patient_name, patientName, name]`). A clinic that nests one level deeper or
spells a field differently is a config edit. A clinic with no profile at all
falls back to `_default`, flows end to end on day zero, and shows up in the UI
as unprofiled volume.

Test names resolve through five tiers, cheapest and most certain first: exact
-> alias -> normalised -> fuzzy -> LLM, then the review queue. Every row records
*how* it was resolved and with what confidence, so an analyst can see that a
number came from a 0.88 fuzzy match with two near-ties and exclude it.

The fuzzy tier is hand-written rather than a library because the dominant
defect is an OCR *crop* - `aemoglobin`, `atelet Count`, `eutrophils` - and edit
distance alone has no notion that losing an affix is far more likely than
changing a middle character. Affix matches are reported as their own method, so
a clinic whose scanner is degrading becomes measurable.

### Validation: the coherence engine

Each canonical test declares a unit *dimension* and a plausible value envelope.
Two checks follow, and they are the reason the misalignment is catchable:

1. **Dimension** - `cell/cu.mm` measures a count; haemoglobin is a mass
   concentration. Mismatch, row quarantined.
2. **Range magnitude** - `4000-10000` against haemoglobin's envelope of `1-25`
   is three orders out.

At report level, if enough rows fail *and* those rows cohere with the test
named one position away, the report is quarantined and the offset proposed.
**The pipeline never realigns automatically**, because a wrong realignment
writes a plausible-looking wrong value that is undetectable downstream. The
proposal goes to a human.

Classification separates three questions that are easy to conflate: is the
value *readable* (Invalid), *possible* (Outlier), *normal* (Within/Above/Below
Range). A haemoglobin of 0.1 is bad data, not a critically anaemic patient;
routing it to the clinical queue wastes a reviewer and hides a broken feed.

The source's own verdict is never used - 24 of 59 checkable rows in the samples
disagree with the arithmetic - but it is retained as evidence, and
disagreements become a per-clinic metric.

### Storage (FR-4)

A narrow model: one row per test observation, one per document, one per
medication, plus dead letters, the review queue, the file ledger, run metrics
and learned aliases. BigQuery in production, partitioned on ingest date and
clustered on `(source_system, test_name_canonical, test_analytics)`; SQLite
locally, from the same column definitions so the two cannot drift.

The assignment's 79-column target shape is delivered as a **view** over that
model, matching the supplied CSV column-for-column, and FR-2.2's
five-columns-per-test presentation as a second view generated from the
catalogue. Both required outputs exist exactly as specified; neither dictates
the storage underneath. The wide shape is a fine landing table and a poor
analytics table - a 193-row report cannot be represented without repeating 40
patient columns 193 times, and adding a test would mean adding columns.

**Idempotency is a property of the key, not a procedure.** Every primary key is
a deterministic content hash, so loading is a MERGE that rewrites identical
rows. No read-before-write, no distributed lock, correct under parallel workers
and after a crash mid-batch. `python -m veritas verify` runs the corpus twice
and compares counts and a content checksum.

Writes use BigQuery **load jobs**, which are free, rather than the Storage
Write API, which is billed per GiB. The latency budget is 15 minutes, so
minutes of batching cost nothing.

### Configuration (NFR-2)

Four files carry everything clinic- and domain-specific: the canonical test
catalogue (names, aliases, units, reference ranges, physiological envelopes,
LOINC codes), the unit dimension graph, per-clinic profiles with inheritance,
and the medicine reference. Profiles state only what differs from `_default`.

A config fingerprint is recorded on every run, because output that cannot be
tied to the configuration that produced it is not auditable: the same file
reprocessed after a catalogue change can legitimately resolve differently.

### Error handling (NFR-3)

**A failure is a row, not an exception.** Anything that cannot be read, routed
or standardised becomes a dead-letter record with a reason code from a closed
vocabulary, and the run continues. One clinic's malformed upload must never
stop the other 499. Payloads are copied to
`gs://<bucket>/_deadletter/<reason>/<date>/<sha256>.json` - reason first,
because the question on call is "what is this new `malformed_json` spike", not
"what failed on Tuesday". Replay is a normal run against that prefix, and is
idempotent for free.

Dead letters and the review queue are deliberately separate stores: an error is
something the pipeline *could not* do; a review item is something it *could* do
but should not do unsupervised. Mixing them makes the queue unusable.

### UI (FR-5)

A read-only Cloud Run service behind IAP, reading the same views. Dashboard,
flagged-row queue with the original cell values beside each reason, per-source
quality against configured SLOs, duplicate audit, dead letters, and a record
inspector showing the clinic's raw JSON beside the standardised output. Locally
it is `python -m veritas serve` with no build step and no network fetch.

---

## 3. Where GenAI belongs, and where it does not

The tempting design hands every row to a model. It would work, and it would be
wrong three times over: it replaces an exact, reproducible dictionary lookup
with a probabilistic one; it costs roughly **$3,300/day** at 30M rows/day
against about **$0.17/day** for the tail-only approach; and a per-row network
call cannot hold a 15-minute p95 without a fleet sized for the API rather than
the work.

So GenAI is scoped to what deterministic code genuinely cannot do: naming a
test from a spelling never seen before, and recovering values a clinic buried
inside a name. Both are rare, and both are **cacheable** - every accepted
answer is promoted into the alias dictionary, so a novel spelling is paid for
once and is free forever. That ratchet is the mechanism behind NFR-4.1's
"98% of variants within 30 days": not a hope, a decaying tail.

Without credentials the pipeline runs a deterministic local fallback instead,
labelled `stub`, so a reviewer gets identical output on every run and nothing
from the fallback can be mistaken for a model decision.

---

## 4. Trade-offs taken deliberately

| Decision | Alternative | Why |
|---|---|---|
| Cloud Run + Pub/Sub | Dataflow streaming | 2.3 files/s needs one worker; Dataflow's floor is an always-on worker plus shuffle. ~10x cheaper here. |
| Micro-batch (minutes) | True streaming | The SLO is 15 minutes. Batching is free; per-record writes are billed. |
| Schema-on-read envelope | Schema-on-write contracts | A new clinic must not need a deploy. Cost: a profile typo is a silent miss, mitigated by the onboarding coverage report. |
| Narrow fact table + views | The 79-column table as storage | Adding a test must not add columns. Cost: one join, paid in SQL once. |
| Declared physiological envelopes | Statistical outlier detection | A per-batch rule makes the same value an outlier on a quiet day and normal on a busy one, and cannot be explained to a clinician. |
| Flag and quarantine | Auto-correct the misalignment | A wrong realignment is undetectable downstream. Refusing to be clever is correct when the cost of being wrong is a mis-adjudicated claim. |
| GenAI on the tail | GenAI per row | Four orders of magnitude of cost, for no accuracy gain where a dictionary already answers. |
| SQLite in the prototype | DuckDB | DuckDB is the better engine; SQLite is in the standard library, so the reviewer runs `make demo` with nothing installed. Swapping it is a sink implementation. |

## 5. What would change at 10x

Nothing structural. Worker count rises linearly (the stage is stateless and
partitioned by file). At roughly 2M files/day the single-writer MERGE becomes
the bottleneck and the loader would move to partition-scoped MERGEs running
per clinic. The coherence engine is per report and never becomes a bottleneck.
The learned-alias table is the only shared mutable state, and it is
append-mostly and small.
