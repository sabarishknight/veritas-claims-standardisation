# Cost model

Two decisions in this design are justified by cost rather than by taste. Both
are stated here with their arithmetic, so a reviewer can check them rather than
take them on trust.

All prices are Google Cloud / Gemini **list prices at the time of writing**,
for `asia-south1` where regional. They move. The *ratios* do not, and the
ratios are what the decisions rest on.

Everything below is reproducible:

```bash
make cost                       # the GenAI comparison
make bench FILES=25000          # the infrastructure measurement
```

---

## 1. Where GenAI belongs

### The tempting design

Hand every row to a model: "here is a test name, a result and a unit - give me
the canonical test and whether it is abnormal." It would work. It is still the
wrong decision, for three reasons in descending order of importance.

**It replaces determinism with probability for no gain.** 97% of rows in the
sample corpus resolve by dictionary lookup: exactly, reproducibly, auditably,
identically on every run. Swapping an exact lookup for a probabilistic one adds
no accuracy where the dictionary already answers, and removes reproducibility -
and a claims decision that cannot be reproduced cannot be defended to a
regulator.

**It costs four orders of magnitude more.**

| | LLM on every row | LLM on the unmapped tail |
|---|---:|---:|
| units priced | 30,000,000 rows/day | ~2,000 novel names/day |
| input tokens/day | ~6,000,000,000 | ~80,000 |
| output tokens/day | ~600,000,000 | ~60,000 |
| **cost/day** | **~$3,300** | **~$0.17** |
| **cost/year** | **~$1,200,000** | **~$64** |

Derivation: 200,000 files/day x ~150 rows = 30M rows/day. At ~200 input and ~20
output tokens per row, priced at $0.30/M input and $2.50/M output for Gemini
2.5 Flash. The tail figure uses the measured batch prompt: ~40 input and ~30
output tokens per novel name.

**It cannot hold the latency SLO.** A per-row network call at 30M rows/day
needs a fleet sized for the API's round-trip time rather than for the work.
The 15-minute p95 (NFR-1.2) would require either enormous concurrency or
accepting that the LLM is the pipeline's critical path.

### The design taken

GenAI is scoped to what deterministic code genuinely cannot do:

1. naming a test from a spelling never seen before, and
2. recovering values a clinic buried inside a test name.

Both are rare, and - the part that matters - both are **cacheable**.

```
spelling arrives ──▶ dictionary?  ──yes──▶ free, forever
       │ no
       ▼
     fuzzy?          ──yes──▶ free, forever  (and promoted to the dictionary)
       │ no
       ▼
   LLM (batched)     ──yes──▶ paid ONCE     (and promoted to the dictionary)
       │ no
       ▼
  human review       ──────▶  paid once in attention (and promoted)
```

Every accepted answer is written to `learned_alias` and seeds every later run.
A novel spelling is therefore a **one-off** cost, not a recurring one, and the
tail decays as a source matures.

### Why that decay is the NFR-4.1 mechanism

NFR-4.1 asks for 98% of variants resolved within 30 days of a clinic going
live. The ratchet above is how:

| day | novel names | cumulative LLM cost |
|---:|---:|---:|
| 1 | ~2,000 | ~$0.17 |
| 7 | ~300 | ~$0.35 |
| 30 | ~50 | ~$0.70 |
| 365 | ~10/day | **under $5 for the year** |

The curve is a property of the design, not an optimisation bolted on: because
every decision is promoted into config, yesterday's hard case is today's dict
hit. The actual rate is visible per source in `v_clinic_quality.unmapped_rate`
and the tier counters in `pipeline_run.resolver_stats`, so the claim is
measurable rather than asserted.

### Guard rails

- `llm.max_calls_per_run` caps spend per run. Names beyond the budget go to the
  human queue, which is where they belonged anyway.
- `llm.min_confidence_to_accept` rejects low-confidence answers.
- The model may only choose from the catalogue; a name it invents is rejected
  at the parse boundary, so a hallucination cannot create a test.
- Without credentials the pipeline uses a deterministic local fallback labelled
  `stub`, so output is identical on every run and nothing from the fallback can
  be mistaken for a model decision.

---

## 2. Cloud Run versus Dataflow

### The arithmetic that settles it

200,000 files/day is **2.31 files/second**. The 2x burst is 4.62/second. That
is the entire premise of the ingestion design, and it is easy to miss because
"200,000" sounds large and "2.3 per second" does not.

Measured per-file cost (`make bench`): **0.061 CPU-seconds** for a 150-row
file including JSON parse, name resolution with fuzzy matching, unit
harmonisation, coherence checks and classification. One vCPU therefore handles
**16.5 files/second**, or 9,879 rows/second.

**One worker covers the steady state and the 2x burst with roughly 7x
headroom.** A full day's 200,000 files processes in 50 minutes.

### What each option costs at that volume

| | Cloud Run + Pub/Sub | Dataflow streaming |
|---|---:|---:|
| minimum footprint | scales to zero | >= 1 always-on worker + shuffle |
| compute, 200k files/day | ~$10/month | ~$150-250/month |
| operational surface | a container | a pipeline, a runner, a shuffle service |
| scaling unit | an instance | a worker |

Dataflow is the right answer when the work needs windowed joins across files,
exactly-once streaming semantics the sink cannot provide, or roughly 50x this
volume. None of those apply: each file is independent, idempotency comes from
deterministic keys rather than from the runner, and the volume is small.

Choosing Dataflow here would be choosing a bigger operational surface and ~10x
the cost for a capability the problem does not need.

### Full monthly estimate at 200,000 files/day

From `bench/results.json`, at 19 KiB mean file size:

| component | USD/month | note |
|---|---:|---|
| Cloud Run compute | 9.63 | 0.061 CPU-s/file x 6M files/month |
| Cloud Run requests | 2.40 | one request per object |
| GCS class-A operations | 30.00 | one write + one read per object |
| Pub/Sub | 4.28 | ~3.6 GiB/day |
| GCS storage | 2.52 | growing; lifecycle to Nearline at 90 days |
| BigQuery storage | 1.31 | growing |
| **total** | **~$50** | plus BigQuery query cost, which depends on use |

Worth noticing which line is largest: **GCS operations, not compute.** At this
volume the pipeline costs more to *address* than to *run*, which is a useful
corrective to an instinct to optimise the processing code. If this bill ever
needed reducing, batching small objects at the clinic edge would beat any
amount of tuning in the standardiser.

**BigQuery load jobs are free.** The pipeline writes via load jobs plus a MERGE
rather than the Storage Write API, which is billed per GiB. At 30M rows/day
that choice alone is the difference between nothing and a five-figure annual
line item - and it costs only the batching latency the 15-minute SLO already
allows.

The honest caveat: BigQuery *query* cost is not included because it depends
entirely on how analysts use the warehouse. Partitioning on ingest date and
clustering on `(source_system, test_name_canonical, test_analytics)` is what
keeps the common queries - "today's flagged rows for clinic X" - scanning one
partition instead of the whole table.

---

## 3. What this costs to get wrong

Worth stating, because it is the number that justifies the engineering effort
in the coherence engine rather than the infrastructure choices above.

A white cell count of 9,700 loaded into a haemoglobin column does not look
wrong. It is a number, in a real test, on a complete row. An adjudication rule
reading "haemoglobin < 7 -> approve transfusion claim" will not fire; a rule
reading "haemoglobin > 20 -> query" will. Either way the decision is made on a
value that was never measured.

In the five supplied sample files, **one of the two lab reports is affected**,
and the pattern recurs in a second file from a different source system. If that
rate held across the feed, the pipeline would be writing unverifiable values
into a material share of claims every day - silently, and with no downstream
signal that anything was wrong.

The coherence engine costs microseconds per row. That is the trade.
