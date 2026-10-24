# Data profile: what is actually in the sample corpus

Findings from the five supplied files, before any design decisions were made.
Every claim below is reproducible with `python3 scripts/profile_samples.py`,
which prints the evidence that follows.

This document exists because the five files are not a clean dataset with
cosmetic inconsistencies. They contain at least one defect that silently
produces **clinically wrong values** in the warehouse, and the architecture is
shaped around catching it.

---

## F-1. Lab rows are misaligned: test names sit one row away from their values

**Severity: critical. This is the finding that shaped the design.**

`Sample_JSON_file2.json`, first rows of the CBC panel as received:

| row | `test_name` | `range` | `unit` | `result` |
|----:|---|---|---|---|
| 0 | `aemoglobin` | `4000-10000` | `cell/cu.mm` | `9700` |
| 1 | `tal WBC Count` | `4.50-5.50` | `mil/cu.mm` | `5.45` |
| 2 | `BC INDICES` | `36-46` | `%` | `44.9` |
| 3 | `ematocrit HCT` | `83-101` | `FL` | `82` |
| 4 | `lean Corp Volume MCV` | `31.5-34.5` | `gm/dL` | `34` |

Read row 0 on its own. The name is haemoglobin. The value is `9700` with a
reference range of `4000-10000` in `cell/cu.mm`. Haemoglobin is a mass
concentration around 13-17 g/dL; `4000-10000 cell/cu.mm` is a **total white
cell count**. Row 1 names the WBC count but carries `4.50-5.50 mil/cu.mm`,
which is a **red cell count**. Row 3 names the haematocrit but carries
`83-101 FL`, which is an **MCV**.

The names are the standard CBC panel in its printed order. The values are the
same panel starting one position later. Two defects are stacked:

1. **A row drop shifted the name column** one position relative to the values.
2. **Each name lost one or two characters from its left edge** - `aemoglobin`,
   `tal WBC Count`, `atelet Count`, `eutrophils`, `mphocytes`, `sinophils`,
   `onocytes`, `sophils`, `DW-CV`. Consistent with a page-edge crop in OCR.

The same pattern appears in `Sample_JSON_file5.json`, from a **different source
system** (`ARTEMIS`), so it is a systemic OCR failure mode rather than one bad
file.

### Why this is the dangerous one

A fuzzy matcher resolves `aemoglobin` to `HAEMOGLOBIN` with 0.97 confidence,
and it is **right about the name**. A pipeline that resolves the name and loads
the value produces a warehouse in which thousands of patients have a
haemoglobin of 9700. Automated adjudication then acts on it. Nothing about the
output looks wrong: the test exists, the value is a number, the row is
complete.

String matching cannot catch this, because the string matching is correct.

### What the pipeline does

Two independent checks, both driven by the catalogue
(`validate/coherence.py`):

- **Unit dimension.** `cell/cu.mm` measures a count; haemoglobin is a mass
  concentration. Mismatch, row quarantined.
- **Range magnitude.** `4000-10000` against haemoglobin's physiological
  envelope of `1-25` is three orders of magnitude out.

Then at report level: if enough rows fail *and* those rows cohere with the test
named one position away, the report is quarantined with a proposed offset. On
file 2 the pipeline reports:

> 6 of 15 checkable rows are incoherent with the test they name; 3 of the 4
> rows where the hypothesis is testable (75%) cohere instead with the test
> named 1 row later. The value columns appear shifted by +1.

**The rows are never realigned automatically.** A wrong realignment writes a
plausible-looking wrong value into a claim decision, and it would be
undetectable downstream. The offset is a proposal in the review queue for a
human to accept.

---

## F-2. The source's own interpretation is wrong 41% of the time

Where a lab reports an interpretation *and* the value and range are both
parseable, the interpretation can be checked against the arithmetic. **24 of 59
checkable rows disagree.**

| file | test | result | range | source says | arithmetic |
|---|---|---|---|---|---|
| file2 | `ematocrit HCT` | 82 | 83-101 | normal | **Below Range** |
| file2 | `DW-CV` | 14.2 | 11.6-14.0 | normal | **Above Range** |
| file2 | `LCR` | 72 | 40-70 | normal | **Above Range** |
| file4 | `BILIRUBIN(INDIRECT)` | 0.40 | 0-0.3 | low | **Above Range** |
| file4 | `ALKALINE PHOSPHATASE` | 124.00 | 38-126 | high | **Within Range** |
| file4 | `ALBUMIN` | 3.90 | 3.5-5 | low | **Within Range** |

Both error directions occur, so it is not a consistent offset that could be
corrected - it is unreliable.

**Consequence for the design:** `test_analytics` is always recomputed from the
value and the range. The source's verdict is retained in
`source_interpretation` as evidence, and where it disagrees the row is flagged
`source_interpretation_disagrees`, which turns a silent quality problem into a
per-clinic metric. Across the full corpus including the edge-case files, the
pipeline reports the disagreement rate with its denominator on the dashboard.

---

## F-3. The same field means different things in different files

`test_analytics` carries:

| file | source | values |
|---|---|---|
| file2 | FASTTRACK | `normal` x18, `OTE` x1, empty x4 - an **interpretation** |
| file4 | FASTTRACK | `low/normal/high` x44, `Normal` x38, `low` x15 - an **interpretation**; and `Calculated` x12, `Direct ISE` x8, `Electrical impedence` x4, `Flow cytometry` x2, `p-nitrophenyl phosphate` x2 - an **assay method** |
| file5 | ARTEMIS | `low/normal/high` x26, `low` x12, `Present` x6, `ABSENT` x2 - an **interpretation**, plus a leaked header row |

Two different meanings **inside a single file**, not merely across clinics.
`Sample_JSON_file4.json` reports an interpretation on some rows and the assay
method on others, in the same column. A pipeline that assumes one meaning per
column either writes assay methods into an interpretation column or discards
genuine interpretations - and in both cases does so silently.

Note also that `low/normal/high` appears as a *value*: the PDF's column
legend, leaked into the data on rows where the real value was blank.

**Consequence:** the clinic profile declares
`source_interpretation_semantics: interpretation | method | mixed | unknown`.
`mixed` sniffs per row against a configured vocabulary. Neither value is ever
used to classify a result.

---

## F-4. Duplicates do not share identifiers

`Sample_JSON_file1.json` and `Sample_JSON_file3.json` carry **the same
discharge summary** - identical admission and discharge dates, diagnosis,
examination text and all 18 medication lines - under different identifiers:

| | file1 | file3 |
|---|---|---|
| `documentId` | `DOC01Q6V9MH7F2V97XBAR3CPDLALSH` | `DOC01753KEPCMVLML3B3VTYA5KOS6I` |
| `traceId` | `8435a58f...` | `ad717d43...` |
| `correlationId` | `b1458a7f...` | `bf4e3c9c...` |
| `claim_no` | `...238343` | `...842817` |

`SELECT DISTINCT document_id` sees two records and the claim is adjudicated
twice. Separately, `Sample_JSON_file5.json` carries one discharge summary
**twice inside a single envelope**.

**Consequence:** identity is a SHA-256 over the clinical payload with transport
fields removed, configurable per clinic. Both cases are caught; a genuinely
corrected resubmission still produces a different hash and is not suppressed.

---

## F-5. Panels are repeated across pages

Multi-page OCR emits the same panel once per page it appears on.
`Sample_JSON_file4.json` repeats `PLATELET COUNT`, `ALBUMIN` and `GLOBULIN` six
times each with identical values; 73 of its 193 rows are verbatim repeats.
`Sample_JSON_file5.json`: 30 of 147.

**Consequence:** rows repeated verbatim within one report collapse on
`(test, result, range, unit)`. `result` is deliberately in the key, so two
draws of the same test with *different* values both survive - that is a falling
haemoglobin, which is exactly the signal an analyst wants.

---

## F-6. The result column holds at least eleven grammars

| shape | example | from |
|---|---|---|
| plain | `9700`, `5.45`, `04` | all |
| thousands comma | `4,290 cells/cu.mm` | file4 |
| inline unit | `13.7 g/dl`, `116.21 mL/min/1.73m2` | file4 |
| high/low marker | `H 0.7 %`, `L 23.5 %` | file5 |
| compound | `100/60 mmHg` | file5 |
| value + commentary | `99% On Room Air` | file5 |
| qualitative | `POSITIVE`, `NEGATIVE`, `NIL`, `ABSENT` | file2, file4, file5 |
| the range, leaked | `1.5-4.5` in both columns | file2 |
| OCR debris | `OTE` | file2 |
| whole panel packed | `12.0 g/dL, 36.10, 13-17, 40-50, 85.5 fL, ...` | file4 |
| narrative | a full USG report in a lab-result cell | file5 |

**Consequence:** `standardise/numeric.py` handles each shape explicitly and
**never invents a number**. A cell it cannot read produces a null value, the
original text, and a flag.

---

## F-7. Values are hidden inside test names

`Sample_JSON_file5.json` has rows whose `result` is empty because the values
were absorbed into the name:

```
"LFT ( SGOT - 38, SGPT -14, ALP - 127)"   -> three results
"KFT - sr creatinine - 0.3"               -> one result
"Na + (127)"                              -> one result
```

Five measurements no downstream rule would ever read.

**Consequence:** they are extracted into their own rows, flagged
`value_embedded_in_name`, and queued for confirmation. They are never merged
silently into the parent row, because the recovery is an inference.

---

## F-8. Placeholders and leaked structure

| what | example | file |
|---|---|---|
| the PDF header row, in the data | `test_name: "test_name"`, `result: "result"` | file5 |
| a date placeholder | `reports_date: "DD/MM/YYYY"` | file5 |
| a bill *number* in a date column | `bill_date: "LAB10945"` | file2 |
| the range leaked into the result | `result == range == "1.5-4.5"` | file2 |
| the test name leaked into the unit | `unit: "atelet Count"` | file2 |
| a letterhead strapline as a unit | `unit: "Expertise. Empowering you."` | file5 |
| a reference range as a unit | `unit: "0-3"` | file5 |
| panel headings as test names | `COMPLETE BLOOD COUNT (CBC) , WHOLE BLOOD EDTA` | file4 |

Panel headings matter more than they look: left unmatched they fuzzy-resolve to
whichever member test they most resemble, and the value printed beside the
heading - which belongs to the panel's first analyte - is then attributed to
the wrong test.

**Consequence:** all of these are recognised by configured patterns and routed
to the dead-letter store with a precise reason, so the review queue contains
only things a human can act on.

---

## F-9. Unit spellings, including OCR corruption

49 distinct spellings across the corpus, including `g/dL` / `g/dl` / `gm/dl` /
`gm/di`, `cells/cu.mm` / `cell/cu.mm` / `cells/cumm` / `Cells/cu.mm`,
`mil/cu.mm` / `Million/cu.mm` / `mil/cu.cm`, `fL` / `fl` / `FL` / `FI`,
`U/L` / `UIL` / `UA`, and the Indian `lac/cmm` / `lakhs/cumm` (1 lakh = 10^5).

`gm/di`, `FI`, `UIL` and `UA` are OCR misreads of real units. They are listed
as ordinary unit spellings, because a misread unit is still a known unit and
refusing to read it would discard a valid result. Tokens that are *not* units
at all are listed separately so they produce a precise reason rather than a
guess.

---

## F-10. Ambiguity that only the unit can settle

`NEUTROPHILS` appears with `%` and with `cells/cu.mm`. These are different
tests - a proportion and an absolute count - with different reference ranges
(40-70% versus 1800-7700 cells/cu.mm). The same is true of all five
differential counts.

**Consequence:** the catalogue holds both forms sharing their aliases, and the
resolver disambiguates on the row's unit dimension. Without this, one of the
two forms is always classified against the wrong band. In the supplied corpus
this affects 12 rows in `Sample_JSON_file4.json` alone.

---

## Summary table

| # | finding | severity | mechanism in the pipeline |
|---|---|---|---|
| F-1 | Name/value misalignment | **critical** | coherence engine + report-level detector |
| F-2 | Source interpretation 41% wrong | high | always recompute; record disagreement |
| F-3 | Field meaning differs per file | high | per-profile semantics, `mixed` sniffing |
| F-4 | Duplicates with different IDs | high | content-hash identity |
| F-5 | Panels repeated across pages | medium | within-report row collapsing |
| F-6 | Eleven result grammars | medium | explicit parsing, never invent |
| F-7 | Values inside names | medium | extraction into flagged rows |
| F-8 | Placeholders and leaked structure | medium | configured non-test patterns |
| F-9 | Unit spelling and OCR drift | medium | dimensional unit catalogue |
| F-10 | Percentage vs absolute ambiguity | medium | unit-based disambiguation |
