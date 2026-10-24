"""The operational UI server (FR-5.1 to FR-5.4).

``python -m veritas serve`` and open the printed URL. A stdlib
``ThreadingHTTPServer`` with a small router: no framework, no build step, no
network fetch at render time.

Production note: in the deployed topology this is a separate Cloud Run service
reading BigQuery, behind IAP. It is in the same package here so the prototype is
one artefact to run, and the query layer (``ui/queries.py``) is the only part
that would change.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Config
from ..load.sqlite_sink import SqliteSink
from ..observe.logging import get_logger
from . import queries as q
from . import render as r

log = get_logger(__name__)

Handler = Callable[[SqliteSink, Config, dict[str, list[str]], str], str]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_dashboard(sink: SqliteSink, config: Config, params, path) -> str:
    head = q.headline(sink)
    run = q.latest_run(sink)
    disagreement = q.source_disagreement(sink)
    misaligned = q.misalignments(sink)

    stat_tiles = r.tiles([
        ("Files", head["files"], f'{head["files_failed"]} could not be read'),
        ("Documents", head["documents"], f'{head["duplicates"]} duplicates suppressed'),
        ("Test results", head["rows"],
         f'{r.num(head["usable"])} carry a usable, classified value'),
        ("Withheld", head["withheld"],
         f'{head["invalid"]} unreadable, {head["outliers"]} implausible'),
        ("Unmapped names", head["unmapped"], "no canonical test; never guessed"),
        ("Open reviews", head["reviews_open"], "awaiting a human decision"),
    ])

    banner = ""
    if misaligned:
        items = []
        for row in misaligned:
            evidence = str(row["evidence"]).split(" | ")
            lines = "".join(f'<p class="ev">{r.e(part)}</p>' for part in evidence[:5])
            items.append(
                f'<p style="margin:10px 0 4px"><strong>{r.e(row["subject"])}</strong></p>'
                f'{lines}'
                f'<p class="ev" style="border-color:var(--critical)">'
                f'<strong>Proposal:</strong> {r.e(row["proposal"])}</p>'
            )
        banner = (
            f'<section class="callout" style="margin-bottom:14px">'
            f'<h2><i class="dot s-critical"></i>'
            f'{len(misaligned)} report(s) quarantined: column misalignment suspected</h2>'
            f'<p class="sub">The test names in these reports do not describe the values '
            f'printed beside them. Rows are flagged and withheld from analytics rather '
            f'than realigned automatically: a wrong realignment would write a '
            f'plausible-looking wrong value into a claim decision.</p>'
            f'{"".join(items)}</section>'
        )

    disagree_note = ""
    if disagreement["checkable"]:
        disagree_note = (
            f'The lab\'s own verdict disagrees with the arithmetic on '
            f'<strong>{r.num(disagreement["disagree"])} of '
            f'{r.num(disagreement["checkable"])}</strong> rows where both a verdict and a '
            f'usable value exist ({r.pct(disagreement["disagree"], disagreement["checkable"], 0)}). '
            f'Every classification on this page is recomputed from the value and the '
            f'range; the source verdict is kept only as evidence.'
        )

    charts = (
        '<div class="grid g-2">'
        + r.card("Result classification",
                 r.bar_chart(q.analytics_distribution(sink), show_status=True),
                 note=disagree_note)
        + r.card("How test names were resolved",
                 r.bar_chart(q.resolution_methods(sink)),
                 note="Exact and alias matches are dictionary hits. "
                      "<em>fuzzy_affix</em> is the OCR-crop route "
                      "(<span class='mono'>aemoglobin</span> &rarr; HAEMOGLOBIN); a rising "
                      "share from one source means its scanner is degrading.")
        + "</div>"
    )

    second = (
        '<div class="grid g-2" style="margin-top:14px">'
        + r.card("Why rows are flagged", r.bar_chart(q.flag_counts(sink), show_share=False))
        + r.card("Dead-lettered rows by reason",
                 r.table([("reason", "mono"), ("stage", "text"), ("value", "num")],
                         q.dlq_reasons(sink), empty="Nothing dead-lettered.",
                         renderers={"value": lambda row:
                                    f'<td class="num">{r.num(row["value"])}</td>'}),
                 note="Rows that are not test results at all - page footers, panel "
                      "headings, narrative. Routed out with a precise reason so the "
                      "review queue stays actionable.")
        + "</div>"
    )

    run_card = ""
    if run:
        run_card = r.card("Latest run", f"""<dl class="kv">
<dt>run</dt><dd class="mono">{r.e(run['run_id'])}</dd>
<dt>config</dt><dd class="mono">{r.e(run['config_fingerprint'])}</dd>
<dt>started</dt><dd>{r.e(run['started_at'])}</dd>
<dt>duration</dt><dd>{r.e(run['duration_seconds'])} s</dd>
<dt>workers</dt><dd>{r.e(run['worker_count'])}</dd>
<dt>files</dt><dd>{r.num(run['files_processed'])} processed,
  {r.num(run['files_failed'])} failed, {r.num(run['files_skipped_duplicate'])} skipped</dd>
<dt>genai</dt><dd>{r.num(run['llm_calls'])} call(s),
  {r.num(run['llm_cache_hits'])} cache hit(s)</dd>
</dl>""", note="Output is tied to the configuration that produced it: the same file "
               "reprocessed after a catalogue change can legitimately resolve "
               "differently, and the fingerprint is how an analyst sees why.")

    return r.page(
        "Pipeline dashboard",
        banner + stat_tiles + charts + second
        + f'<div class="grid g-2" style="margin-top:14px">{run_card}</div>',
        current="/", counts=_nav_counts(sink),
        subtitle="Ingestion health, result quality and everything withheld from "
                 "analytics, for the current warehouse contents.",
    )


def page_flagged(sink: SqliteSink, config: Config, params, path) -> str:
    flag = (params.get("flag") or [""])[0] or None
    analytics = (params.get("analytics") or [""])[0] or None
    source = (params.get("source") or [""])[0] or None
    rows = q.flagged_rows(sink, flag=flag, analytics=analytics, source=source)

    def options(name: str, values, selected) -> str:
        return (
            f'<select name="{name}"><option value="">all {name}s</option>'
            + "".join(
                f'<option value="{r.e(v)}"'
                f'{" selected" if v == selected else ""}>{r.e(v)}</option>'
                for v in values)
            + "</select>")

    filters = (
        '<form class="filters" method="get">'
        + options("flag", [f["label"] for f in q.flag_counts(sink, 40)], flag)
        + options("analytics", q.ANALYTICS_ORDER, analytics)
        + options("source", q.source_systems(sink), source)
        + '<button type="submit">Filter</button>'
        + '<a class="tag" href="/flagged">reset</a></form>'
    )

    body = r.table(
        [("test (as received)", "test_name_original"), ("canonical", "test_name_canonical"),
         ("result", "result_text_original"), ("unit", "unit_original"),
         ("range", "range_text_original"), ("value", "result_value"),
         ("class", "test_analytics"), ("method", "normalization_method"),
         ("conf", "normalization_confidence"), ("why", "flags"), ("doc", "document_pk")],
        rows,
        renderers={
            "class": lambda row: (
                f'<td><i class="dot s-{q.ANALYTICS_STATUS.get(row["test_analytics"], "neutral")}">'
                f'</i>{r.e(row["test_analytics"])}</td>'),
            "why": lambda row: f'<td class="wrap">{r.flag_tags(row["flags"])}'
                               f'<div class="muted" style="font-size:11.5px;margin-top:3px">'
                               f'{r.e((row["coherence_note"] or "")[:190])}</div></td>',
            "doc": lambda row: (f'<td><a class="mono" href="/record/{r.e(row["document_pk"])}">'
                                f'{r.e(str(row["document_pk"])[:10])}</a></td>'),
            "conf": lambda row: f'<td class="num">{row["normalization_confidence"] or 0:.2f}</td>',
            "value": lambda row: f'<td class="num">{r.e(row["result_value"])}</td>',
        },
        empty="No flagged rows match this filter.",
    )
    return r.page("Flagged rows", filters + r.card(f"{len(rows)} row(s)", body),
                  current="/flagged", counts=_nav_counts(sink),
                  subtitle="Every row the pipeline would not load unsupervised, with the "
                           "original cell values beside the reason. The first four columns "
                           "are exactly what the clinic sent.")


def page_reviews(sink: SqliteSink, config: Config, params, path) -> str:
    kind = (params.get("kind") or [""])[0] or None
    kinds = q.review_kinds(sink)
    chips = " ".join(
        f'<a class="tag" href="/reviews?kind={r.e(k["label"])}">'
        f'{r.e(k["label"])} ({r.num(k["value"])})</a>' for k in kinds)
    rows = q.review_queue(sink, kind)
    body = r.table(
        [("pri", "priority"), ("kind", "kind"), ("subject", "subject"),
         ("evidence", "evidence"), ("proposal", "proposal"), ("source", "source_system")],
        rows,
        renderers={
            "pri": lambda row: f'<td class="num">{r.e(row["priority"])}</td>',
            "subject": lambda row: f'<td class="wrap"><strong>{r.e(row["subject"])}</strong></td>',
            "evidence": lambda row: f'<td class="wrap">{r.e(row["evidence"])}</td>',
            "proposal": lambda row: f'<td class="wrap">{r.e(row["proposal"] or "-")}</td>',
        },
        empty="Review queue is empty.",
    )
    return r.page(
        "Review queue", f'<div style="margin-bottom:14px">{chips} '
                        f'<a class="tag" href="/reviews">all</a></div>'
                        + r.card(f"{len(rows)} open item(s)", body),
        current="/reviews", counts=_nav_counts(sink),
        subtitle="Decisions the pipeline could make but should not make unsupervised. "
                 "Accepting one promotes it into config, and the same spelling is then "
                 "free forever - that ratchet is how name coverage reaches 98%.")


def page_clinics(sink: SqliteSink, config: Config, params, path) -> str:
    rows = q.clinic_quality(sink)
    peak = max((row["rows_total"] for row in rows), default=1)
    expectations = {p.clinic_id: p.quality_expectations for p in config.profiles}

    def slo(row: dict[str, Any]) -> str:
        profile = next((p for p in config.profiles
                        if p.match.get("source_system") == row["source_system"]), None)
        limits = expectations.get(profile.clinic_id if profile else "_default", {}) or {}
        notes: list[str] = []
        for metric, key, label in (
            ("unmapped_rate", "max_unmapped_test_rate", "unmapped"),
            ("invalid_rate", "max_invalid_row_rate", "invalid"),
        ):
            limit = limits.get(key)
            actual = row.get(metric)
            if limit is None or actual is None:
                continue
            breached = float(actual) > float(limit)
            notes.append(
                f'<span class="tag {"bad" if breached else ""}">'
                f'<i class="dot s-{"critical" if breached else "good"}"></i>'
                f'{label} {float(actual):.1%} / {float(limit):.0%}</span>')
        return f'<td class="wrap">{"".join(notes) or "<span class=muted>-</span>"}</td>'

    body = r.table(
        [("source", "source_system"), ("rows", "rows_total"),
         ("unmapped", "rows_unmapped_test"), ("invalid", "rows_invalid"),
         ("outlier", "rows_outlier"), ("out of range", "rows_out_of_range"),
         ("unit mismatch", "rows_unit_mismatch"),
         ("source verdict wrong", "rows_source_disagrees"),
         ("fuzzy", "rows_fuzzy_matched"), ("avg conf", "avg_confidence"),
         ("vs SLO", "slo")],
        rows,
        renderers={
            "rows": lambda row: f'<td class="num">{r.mini_bar(row["rows_total"], peak)}</td>',
            "unmapped": lambda row: f'<td class="num">{r.num(row["rows_unmapped_test"])}</td>',
            "invalid": lambda row: f'<td class="num">{r.num(row["rows_invalid"])}</td>',
            "outlier": lambda row: f'<td class="num">{r.num(row["rows_outlier"])}</td>',
            "out of range": lambda row: f'<td class="num">{r.num(row["rows_out_of_range"])}</td>',
            "unit mismatch": lambda row: f'<td class="num">{r.num(row["rows_unit_mismatch"])}</td>',
            "source verdict wrong": lambda row:
                f'<td class="num">{r.num(row["rows_source_disagrees"])}</td>',
            "fuzzy": lambda row: f'<td class="num">{r.num(row["rows_fuzzy_matched"])}</td>',
            "avg conf": lambda row: f'<td class="num">{(row["avg_confidence"] or 0):.3f}</td>',
            "vs SLO": slo,
        },
        empty="No rows loaded yet.",
    )
    return r.page(
        "Per-source data quality", r.card("Quality by source system", body,
            note="Thresholds come from each clinic profile's "
                 "<span class='mono'>quality_expectations</span> block. A breach is a "
                 "conversation with that clinic, not a code change - which is the point "
                 "of keeping them in config."),
        current="/clinics", counts=_nav_counts(sink),
        subtitle="One row per source system. These are the numbers that turn "
                 "“the data is messy” into a specific, evidenced request.")


def page_records(sink: SqliteSink, config: Config, params, path) -> str:
    search = (params.get("q") or [""])[0].strip() or None
    rows = q.documents(sink, search=search)
    form = (f'<form class="filters" method="get">'
            f'<input type="search" name="q" placeholder="document id, claim or hospital" '
            f'value="{r.e(search or "")}" style="min-width:300px">'
            f'<button type="submit">Search</button>'
            f'<a class="tag" href="/records">reset</a></form>')
    body = r.table(
        [("document", "document_id"), ("type", "record_type"), ("source", "source_system"),
         ("claim", "claim_no"), ("hospital", "hospital_name"), ("report date", "report_date"),
         ("rows", "rows_n"), ("meds", "meds_n"), ("state", "is_duplicate")],
        rows,
        renderers={
            "document": lambda row: (f'<td><a class="mono" href="/record/{r.e(row["id"])}">'
                                     f'{r.e(row["document_id"])}</a></td>'),
            "rows": lambda row: f'<td class="num">{r.num(row["rows_n"])}</td>',
            "meds": lambda row: f'<td class="num">{r.num(row["meds_n"])}</td>',
            "state": lambda row: (
                '<td><i class="dot s-neutral"></i>duplicate</td>' if row["is_duplicate"]
                else '<td><i class="dot s-good"></i>loaded</td>'),
            "hospital": lambda row: f'<td class="wrap">{r.e(row["hospital_name"])}</td>',
        },
        empty="No documents match.",
    )
    return r.page("Record inspector", form + r.card(f"{len(rows)} document(s)", body),
                  current="/records", counts=_nav_counts(sink),
                  subtitle="Pick a document to see the clinic's raw JSON beside the "
                           "standardised rows it produced.")


def page_record(sink: SqliteSink, config: Config, params, path) -> str:
    document_pk = path.rsplit("/", 1)[-1]
    doc = q.document(sink, document_pk)
    if not doc:
        return r.page("Record not found",
                      '<p class="empty">No document with that id.</p>',
                      current="/records", counts=_nav_counts(sink))

    rows = q.document_rows(sink, document_pk)
    meds = q.document_medications(sink, document_pk)
    errors = q.document_errors(sink, doc["document_id"])

    identity = r.card("Document", f"""<dl class="kv">
<dt>document id</dt><dd class="mono">{r.e(doc['document_id'])}</dd>
<dt>internal id</dt><dd class="mono">{r.e(doc['id'])}</dd>
<dt>type</dt><dd>{r.e(doc['record_type'])}</dd>
<dt>source</dt><dd>{r.e(doc['source_system'])} &middot; profile
  <span class="mono">{r.e(doc['clinic_profile'])}</span> schema
  <span class="mono">{r.e(doc['schema_version'])}</span></dd>
<dt>claim</dt><dd class="mono">{r.e(doc['claim_no'])}</dd>
<dt>patient</dt><dd class="mono">{r.e(doc['patient_token'] or doc['patient_name'] or '-')}
  {'<span class="muted">(HMAC token)</span>' if doc['patient_token'] else ''}</dd>
<dt>age / sex</dt><dd>{r.e(doc['age_years'])} ({r.e(doc['age_text'])}) / {r.e(doc['gender'] or '-')}</dd>
<dt>hospital</dt><dd>{r.e(doc['hospital_name'])}</dd>
<dt>dates</dt><dd>admit {r.e(doc['admission_date'] or '-')} &middot;
  discharge {r.e(doc['discharge_date'] or '-')} &middot;
  report {r.e(doc['report_date'] or '-')} &middot; bill {r.e(doc['bill_date'] or '-')}</dd>
<dt>content hash</dt><dd class="mono">{r.e(doc['content_hash'])}</dd>
<dt>state</dt><dd>{'duplicate of <span class="mono">' + r.e(doc['duplicate_of']) + '</span>'
                  if doc['is_duplicate'] else 'loaded'}</dd>
<dt>flags</dt><dd>{r.flag_tags(doc['flags'])}</dd>
<dt>lineage</dt><dd class="mono" style="font-size:11.5px">{r.e(doc['file_uri'])}</dd>
<dt>trace</dt><dd class="mono" style="font-size:11.5px">{r.e(doc['trace_id'])}</dd>
<dt>correlation</dt><dd class="mono" style="font-size:11.5px">{r.e(doc['correlation_id'])}</dd>
<dt>run</dt><dd class="mono" style="font-size:11.5px">{r.e(doc['run_id'])}</dd>
</dl>""")

    standardised = r.card(f"Standardised rows ({len(rows)})", r.table(
        [("#", "source_row_index"), ("as received", "test_name_original"),
         ("canonical", "test_name_canonical"), ("raw result", "result_text_original"),
         ("raw unit", "unit_original"), ("raw range", "range_text_original"),
         ("value", "result_value"), ("unit", "unit_canonical"),
         ("range", "range"), ("class", "test_analytics"),
         ("method", "normalization_method"), ("flags", "flags")],
        rows,
        renderers={
            "#": lambda row: f'<td class="num muted">{r.e(row["source_row_index"])}</td>',
            "value": lambda row: f'<td class="num">{r.e(row["result_value"])}</td>',
            "range": lambda row: (f'<td class="num">{r.e(row["range_low"])}'
                                  f'&ndash;{r.e(row["range_high"])}'
                                  f'<div class="muted" style="font-size:11px">'
                                  f'{r.e(row["range_source"])}</div></td>'),
            "class": lambda row: (
                f'<td><i class="dot s-{q.ANALYTICS_STATUS.get(row["test_analytics"], "neutral")}">'
                f'</i>{r.e(row["test_analytics"])}</td>'),
            "method": lambda row: (f'<td>{r.e(row["normalization_method"])}'
                                   f'<div class="muted" style="font-size:11px">'
                                   f'{(row["normalization_confidence"] or 0):.2f}</div></td>'),
            "flags": lambda row: (f'<td class="wrap">{r.flag_tags(row["flags"])}'
                                  f'<div class="muted" style="font-size:11px;margin-top:3px">'
                                  f'{r.e((row["coherence_note"] or "")[:240])}</div></td>'),
        },
        empty="No standardised rows for this document."))

    med_card = ""
    if meds:
        med_card = r.card(f"Medications ({len(meds)})", r.table(
            [("as written", "medicine_original"), ("molecule", "medicine_generic"),
             ("brand", "brand_matched"), ("ATC", "atc_code"), ("form", "dosage_form"),
             ("strength", "strength"), ("dose", "dose"), ("frequency", "frequency"),
             ("conf", "confidence"), ("flags", "flags")],
            meds,
            renderers={
                "conf": lambda row: f'<td class="num">{(row["confidence"] or 0):.2f}</td>',
                "flags": lambda row: f'<td class="wrap">{r.flag_tags(row["flags"])}</td>',
            }))

    error_card = ""
    if errors:
        error_card = r.card(f"Rows routed to the dead-letter store ({len(errors)})", r.table(
            [("#", "source_row_index"), ("stage", "stage"), ("reason", "reason"),
             ("detail", "detail")],
            errors,
            renderers={
                "#": lambda row: f'<td class="num muted">{r.e(row["source_row_index"])}</td>',
                "reason": lambda row: f'<td class="mono">{r.e(row["reason"])}</td>',
                "detail": lambda row: f'<td class="wrap">{r.e(row["detail"])}</td>',
            }),
            note="These rows were not test results. Routed out with a precise reason "
                 "rather than guessed at.")

    raw = r.card("Raw JSON as the clinic sent it", r.pretty_json(doc.get("raw_payload")),
                 note="Retained for audit (FR-4.3), so any standardisation decision on "
                      "the right can be checked against the left.")

    return r.page(
        f"Document {doc['document_id'][:18]}",
        f'<div class="split">{identity}{raw}</div>'
        f'<div style="margin-top:14px">{standardised}</div>'
        + (f'<div style="margin-top:14px">{med_card}</div>' if med_card else "")
        + (f'<div style="margin-top:14px">{error_card}</div>' if error_card else ""),
        current="/records", counts=_nav_counts(sink),
        subtitle="Raw input on the right, standardised output below, with the original "
                 "cell beside every derived value.")


def page_duplicates(sink: SqliteSink, config: Config, params, path) -> str:
    rows = q.duplicates(sink)
    body = r.table(
        [("suppressed document", "document_id"), ("claim", "claim_no"),
         ("kept document", "kept_document_id"), ("kept claim", "kept_claim_no"),
         ("content hash", "content_hash"), ("source", "source_system"),
         ("file", "file_uri")],
        rows,
        renderers={
            "content hash": lambda row: f'<td class="mono">{r.e(row["content_hash"])}</td>',
            "file": lambda row: (f'<td class="wrap mono" style="font-size:11px">'
                                 f'{r.e(row["file_uri"])}</td>'),
        },
        empty="No duplicates suppressed.")
    return r.page(
        "Duplicate suppression", r.card(f"{len(rows)} suppressed document(s)", body,
            note="Identity is a hash of the clinical payload, not the source's "
                 "identifiers. In the supplied corpus two files carry the same discharge "
                 "summary under <em>different</em> documentId and claim_no, so an "
                 "identity-based deduplicator would adjudicate that claim twice."),
        current="/duplicates", counts=_nav_counts(sink),
        subtitle="A silent deduplicator is one nobody trusts, so every suppression is "
                 "listed with the record it was merged into.")


def page_errors(sink: SqliteSink, config: Config, params, path) -> str:
    rows = sink.query(
        "SELECT occurred_at, stage, reason, detail, file_uri, document_id, "
        "       source_row_index, retryable FROM processing_error "
        "ORDER BY occurred_at DESC LIMIT 400")
    body = r.table(
        [("when", "occurred_at"), ("stage", "stage"), ("reason", "reason"),
         ("detail", "detail"), ("file", "file_uri"), ("retryable", "retryable")],
        rows,
        renderers={
            "reason": lambda row: f'<td class="mono">{r.e(row["reason"])}</td>',
            "detail": lambda row: f'<td class="wrap">{r.e(str(row["detail"])[:220])}</td>',
            "file": lambda row: (f'<td class="wrap mono" style="font-size:11px">'
                                 f'{r.e(str(row["file_uri"]).rsplit("/", 1)[-1])}</td>'),
            "retryable": lambda row: (
                '<td><i class="dot s-warning"></i>retry</td>' if row["retryable"]
                else '<td><i class="dot s-neutral"></i>terminal</td>'),
        },
        empty="Nothing dead-lettered.")
    return r.page(
        "Dead-letter store", r.card(f"{len(rows)} entr(ies)", body,
            note="A failure is a row, not an exception: one clinic's malformed upload "
                 "must never stop the other 499 (NFR-3.1). Retryable entries can be "
                 "replayed once the source is fixed; because identity is the content "
                 "hash, the replay is idempotent."),
        current="/errors", counts=_nav_counts(sink),
        subtitle="Everything the pipeline could not process, with enough context to "
                 "reprocess it without the original operator.")


def page_conversions(sink: SqliteSink, config: Config, params, path) -> str:
    rows = q.unit_conversions(sink)
    body = r.table(
        [("test", "test_name_canonical"), ("as received", "result_text_original"),
         ("source unit", "unit_original"), ("canonical value", "result_value"),
         ("canonical unit", "unit_canonical"), ("range", "range"), ("class", "test_analytics")],
        rows,
        renderers={
            "canonical value": lambda row: f'<td class="num">{r.e(row["result_value"])}</td>',
            "range": lambda row: (f'<td class="num">{r.e(row["range_low"])}'
                                  f'&ndash;{r.e(row["range_high"])}</td>'),
            "class": lambda row: (
                f'<td><i class="dot s-{q.ANALYTICS_STATUS.get(row["test_analytics"], "neutral")}">'
                f'</i>{r.e(row["test_analytics"])}</td>'),
        },
        empty="No unit conversions in this dataset.")
    return r.page(
        "Unit harmonisation", r.card(f"{len(rows)} converted row(s)", body,
            note="The reference range is converted with the same factor as the value. "
                 "Converting one without the other would misclassify every row in the "
                 "report, and both numbers would still look reasonable on their own - "
                 "which is why it is worth showing side by side."),
        current="/conversions", counts=_nav_counts(sink),
        subtitle="FR-2.4 in evidence: what the clinic sent, and the comparable number "
                 "it became.")


def page_aliases(sink: SqliteSink, config: Config, params, path) -> str:
    rows = q.learned_aliases(sink)
    body = r.table(
        [("source spelling", "source_name_example"), ("canonical", "canonical"),
         ("provider", "provider"), ("confidence", "confidence"),
         ("why", "rationale"), ("learned", "created_at")],
        rows,
        renderers={
            "confidence": lambda row: f'<td class="num">{(row["confidence"] or 0):.2f}</td>',
            "why": lambda row: f'<td class="wrap">{r.e(row["rationale"])}</td>',
        },
        empty="No learned aliases yet. They accumulate as novel spellings are resolved.")
    return r.page(
        "Learned aliases", r.card(f"{len(rows)} promoted spelling(s)", body,
            note="Each novel spelling is reasoned about once - by the fuzzy matcher, a "
                 "language model, or a human - and is then a dictionary entry forever. "
                 "That ratchet is the mechanism behind NFR-4.1's 98% coverage target, "
                 "and it is why GenAI cost decays toward zero instead of recurring."),
        current="/aliases", counts=_nav_counts(sink),
        subtitle="The pipeline's accumulated knowledge about how clinics spell things.")


def page_runs(sink: SqliteSink, config: Config, params, path) -> str:
    rows = q.all_runs(sink)
    body = r.table(
        [("run", "run_id"), ("started", "started_at"), ("files", "files_processed"),
         ("rows", "rows_emitted"), ("flagged", "rows_flagged"), ("errors", "errors_emitted"),
         ("seconds", "duration_seconds"), ("config", "config_fingerprint")],
        rows,
        renderers={
            "run": lambda row: f'<td class="mono">{r.e(row["run_id"])}</td>',
            "files": lambda row: f'<td class="num">{r.num(row["files_processed"])}</td>',
            "rows": lambda row: f'<td class="num">{r.num(row["rows_emitted"])}</td>',
            "flagged": lambda row: f'<td class="num">{r.num(row["rows_flagged"])}</td>',
            "errors": lambda row: f'<td class="num">{r.num(row["errors_emitted"])}</td>',
            "seconds": lambda row: f'<td class="num">{r.e(row["duration_seconds"])}</td>',
            "config": lambda row: f'<td class="mono">{r.e(row["config_fingerprint"])}</td>',
        },
        empty="No runs recorded.")
    return r.page("Run history", r.card(f"{len(rows)} run(s)", body,
        note="Re-running the same input is safe: primary keys are deterministic content "
             "hashes, so a second run rewrites the same rows rather than appending "
             "duplicates. <span class='mono'>python -m veritas verify</span> proves it."),
        current="/runs", counts=_nav_counts(sink),
        subtitle="Every run, with the configuration fingerprint that produced it.")


def api_summary(sink: SqliteSink, config: Config, params, path) -> str:
    return json.dumps({
        "headline": q.headline(sink),
        "latest_run": q.latest_run(sink),
        "analytics": q.analytics_distribution(sink),
        "clinic_quality": q.clinic_quality(sink),
        "source_disagreement": q.source_disagreement(sink),
        "config_fingerprint": config.fingerprint,
    }, indent=2, default=str)


def api_health(sink: SqliteSink, config: Config, params, path) -> str:
    """Liveness and SLO check, for Cloud Run and for the alerting rules."""
    run = q.latest_run(sink) or {}
    processed = (run.get("files_processed") or 0) + (run.get("files_failed") or 0)
    error_rate = (run.get("files_failed") or 0) / processed if processed else 0.0
    threshold = float(config.section("observability").get(
        "error_rate_alert_threshold", 0.01))
    return json.dumps({
        "status": "ok" if error_rate <= threshold else "degraded",
        "error_rate": round(error_rate, 6),
        "error_rate_threshold": threshold,
        "latest_run": run.get("run_id"),
        "config_fingerprint": config.fingerprint,
    }, indent=2)


ROUTES: dict[str, tuple[Handler, str]] = {
    "/": (page_dashboard, "text/html; charset=utf-8"),
    "/flagged": (page_flagged, "text/html; charset=utf-8"),
    "/reviews": (page_reviews, "text/html; charset=utf-8"),
    "/clinics": (page_clinics, "text/html; charset=utf-8"),
    "/records": (page_records, "text/html; charset=utf-8"),
    "/duplicates": (page_duplicates, "text/html; charset=utf-8"),
    "/errors": (page_errors, "text/html; charset=utf-8"),
    "/conversions": (page_conversions, "text/html; charset=utf-8"),
    "/aliases": (page_aliases, "text/html; charset=utf-8"),
    "/runs": (page_runs, "text/html; charset=utf-8"),
    "/api/summary": (api_summary, "application/json; charset=utf-8"),
    "/api/health": (api_health, "application/json; charset=utf-8"),
}


def _nav_counts(sink: SqliteSink) -> dict[str, int]:
    try:
        return {
            "/flagged": sink.scalar("SELECT COUNT(*) FROM lab_result WHERE flags<>''") or 0,
            "/reviews": sink.scalar(
                "SELECT COUNT(*) FROM review_item WHERE status='open'") or 0,
            "/errors": sink.scalar("SELECT COUNT(*) FROM processing_error") or 0,
            "/duplicates": sink.scalar(
                "SELECT COUNT(*) FROM clinical_document WHERE is_duplicate=1") or 0,
        }
    except Exception:                                      # noqa: BLE001
        return {}


def serve(db_path: str, config: Config, *, host: str = "127.0.0.1", port: int = 8080,
          open_browser: bool = True) -> None:
    """Start the UI. One SQLite connection per request thread."""
    local = threading.local()

    def sink_for_thread() -> SqliteSink:
        existing = getattr(local, "sink", None)
        if existing is None:
            existing = SqliteSink(db_path)
            local.sink = existing
        return existing

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "veritas-ops"

        def do_GET(self) -> None:                          # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            params = parse_qs(parsed.query)
            try:
                sink = sink_for_thread()
                if path.startswith("/record/"):
                    handler, content_type = page_record, "text/html; charset=utf-8"
                elif path in ROUTES:
                    handler, content_type = ROUTES[path]
                else:
                    self._send(404, "text/html; charset=utf-8", r.page(
                        "Not found", '<p class="empty">No such page.</p>',
                        counts=_nav_counts(sink)))
                    return
                body = handler(sink, config, params, path)
                self._send(200, content_type, body)
            except Exception as exc:                       # noqa: BLE001
                log.exception("request failed", extra={"path": self.path})
                self._send(500, "text/html; charset=utf-8", r.page(
                    "Error", f'<div class="callout"><h2>Request failed</h2>'
                             f'<p class="sub">{r.e(f"{type(exc).__name__}: {exc}")}</p>'
                             f'<p class="sub">If the database is empty, run '
                             f'<span class="mono">python -m veritas demo</span> first.</p>'
                             f'</div>'))

        def _send(self, status: int, content_type: str, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # The UI renders its own markup and fetches nothing, so the policy
            # can be strict. 'unsafe-inline' covers the one inline style block
            # and the theme toggle; there is no external origin at all.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("http", extra={"request": fmt % args})

    httpd = ThreadingHTTPServer((host, port), RequestHandler)
    url = f"http://{host}:{port}/"
    print(f"Veritas operations UI  ->  {url}")
    print(f"  database: {db_path}")
    print("  Ctrl-C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
