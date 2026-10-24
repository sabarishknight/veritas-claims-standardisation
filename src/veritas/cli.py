"""Command line interface.

    python -m veritas demo                 run the 5 supplied files + edge cases
    python -m veritas run <path|gs://...>  run the pipeline
    python -m veritas serve                start the operational UI
    python -m veritas ddl --dialect bigquery
    python -m veritas onboard <file>       field-coverage report for a new clinic
    python -m veritas bench --files 200000 synthetic throughput benchmark
    python -m veritas cost                 the GenAI cost comparison
    python -m veritas verify               re-run and prove idempotency
    python -m veritas export --view v_ideal_output_schema --out out.csv
    python -m veritas config               show the loaded configuration

argparse rather than a CLI framework: one less dependency, and the pipeline's
only runtime requirement stays PyYAML.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .config import load_config
from .load import schema
from .load.sqlite_sink import SqliteSink
from .observe.logging import configure, configure_from, get_logger

log = get_logger(__name__)

DEFAULT_DB = "data/veritas.db"
SAMPLE_DIR = "sample-data"


def _sink(args: argparse.Namespace, canonical_tests) -> SqliteSink:
    sink = SqliteSink(args.db)
    sink.initialise(canonical_tests)
    return sink


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline import Pipeline

    config = load_config(args.config_dir)
    configure_from(config.pipeline)
    sink = _sink(args, config.catalog.tests.keys())
    try:
        pipeline = Pipeline(config, sink, config_dir=args.config_dir)
        summary = pipeline.run(
            args.input, workers=args.workers, limit=args.limit,
            skip_seen_files=args.skip_seen,
        )
    finally:
        sink.close()
    _print_summary(summary)
    # A non-zero exit when the error rate breaches the SLO, so a scheduled run
    # fails loudly instead of succeeding quietly with a broken feed (NFR-5.1).
    threshold = float(config.section("observability").get("error_rate_alert_threshold", 0.01))
    if summary.error_rate > threshold:
        print(f"\nERROR RATE {summary.error_rate:.2%} EXCEEDS THRESHOLD {threshold:.2%}",
              file=sys.stderr)
        return 2
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """The reviewer's entry point: one command, no credentials, no setup."""
    config = load_config(args.config_dir)
    configure(level="WARNING", fmt="text")

    sample_root = Path(args.input or SAMPLE_DIR)
    if not sample_root.exists():
        print(f"sample directory not found: {sample_root}", file=sys.stderr)
        return 1

    from .pipeline import Pipeline

    db = Path(args.db)
    if db.exists() and not args.keep:
        db.unlink()
        for suffix in ("-wal", "-shm"):
            extra = Path(str(db) + suffix)
            if extra.exists():
                extra.unlink()

    sink = _sink(args, config.catalog.tests.keys())
    try:
        summary = Pipeline(config, sink, config_dir=args.config_dir).run(
            str(sample_root), workers=args.workers)
        _print_summary(summary)
        print("\n" + "-" * 78)
        print("WHAT THE PIPELINE FOUND IN THIS DATA")
        print("-" * 78)
        for row in sink.query(
            "SELECT reason, COUNT(*) n FROM processing_error GROUP BY 1 ORDER BY n DESC"
        ):
            print(f"  dead-lettered  {row['n']:4}  {row['reason']}")
        print()
        for row in sink.query(
            "SELECT kind, COUNT(*) n FROM review_item GROUP BY 1 ORDER BY n DESC"
        ):
            print(f"  for review     {row['n']:4}  {row['kind']}")
        print()
        misaligned = sink.query(
            "SELECT subject, evidence, proposal FROM review_item "
            "WHERE kind = 'misaligned_report'")
        for row in misaligned:
            print("  REPORT QUARANTINED FOR SUSPECTED COLUMN MISALIGNMENT")
            print(f"    {row['subject']}")
            for part in str(row["evidence"]).split(" | "):
                print(f"      {part}")
            print(f"    proposal: {row['proposal']}")
        disagree = sink.scalar(
            "SELECT COUNT(*) FROM lab_result "
            "WHERE flags LIKE '%source_interpretation_disagrees%'")
        checkable = sink.scalar(
            "SELECT COUNT(*) FROM lab_result WHERE source_interpretation IS NOT NULL "
            "AND source_interpretation <> '' AND result_value IS NOT NULL")
        if checkable:
            print(f"\n  SOURCE VERDICT WRONG on {disagree} of {checkable} checkable rows "
                  f"({disagree / checkable:.0%}) - recomputed from the arithmetic")
        print("\n" + "-" * 78)
        print(f"database: {args.db}")
        print("next:     python -m veritas serve          (operational dashboard)")
        print("          python -m veritas verify         (prove idempotency)")
        print("-" * 78)
    finally:
        sink.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .ui.server import serve

    config = load_config(args.config_dir)
    configure(level="WARNING", fmt="text")
    serve(args.db, config, host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_ddl(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir)
    if args.dialect == "bigquery":
        statements = schema.bigquery_ddl(args.dataset)
    else:
        statements = schema.sqlite_ddl()
    print("\n".join(statements))
    if args.views:
        print()
        print("\n\n".join(schema.all_views(config.catalog.tests.keys())))
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    """Field-coverage report for a prospective clinic file (NFR-2.2).

    Prints every source field the profile does *not* read. That list is the
    onboarding checklist: each entry is either mapped or consciously dropped,
    which is how a new clinic is onboarded in a day without discovering six
    months later that a field was silently ignored.
    """
    from .ingest.envelope import coverage_report, parse_json, unwrap

    config = load_config(args.config_dir)
    raw = Path(args.file).read_bytes()
    document = parse_json(raw, args.file)
    probe = unwrap(document, config.profile_for({}))
    profile = config.profile_for(probe.meta)
    report = coverage_report(document, profile)

    print(f"file           {args.file}")
    print(f"source_system  {probe.source_system}")
    print(f"profile        {report['clinic_profile']} (schema {report['schema_version']})")
    print(f"documents      {[d.classifier for d in probe.documents]}")
    print(f"coverage       {report['coverage']:.1%} "
          f"({report['fields_read']}/{report['fields_seen']} fields read)")
    if report["unread"]:
        print("\nfields the profile does NOT read:")
        for field_path in report["unread"]:
            print(f"  - {field_path}")
        print("\nMap each one in config/clinics/<clinic>.yaml, or record the decision "
              "to drop it.")
    else:
        print("\nevery field is mapped.")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from .benchmark import run_benchmark
    return run_benchmark(args)


def cmd_cost(args: argparse.Namespace) -> int:
    from .standardise.llm import cost_comparison

    model = cost_comparison(args.rows_per_day, args.novel_names_per_day)
    print(json.dumps(model, indent=2))
    print()
    print(f"Classifying every row with an LLM:   ${model['llm_per_row_usd_per_day']:,.2f}/day"
          f"  (${model['llm_per_row_usd_per_year']:,.0f}/year)")
    print(f"Resolving only the unmapped tail:    ${model['llm_tail_usd_per_day']:,.4f}/day"
          f"  (${model['llm_tail_usd_per_year']:,.2f}/year)")
    print(f"Ratio:                               {model['ratio']:,.0f}x")
    print("\nThe tail cost decays further as the learned-alias table fills: a novel\n"
          "spelling is paid for once, then it is a dictionary entry forever.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Prove idempotency by running the same input twice (NFR-3.2)."""
    from .pipeline import Pipeline

    config = load_config(args.config_dir)
    configure(level="ERROR", fmt="text")
    source = args.input or SAMPLE_DIR

    db = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        extra = Path(str(db) + suffix)
        if extra.exists():
            extra.unlink()

    sink = _sink(args, config.catalog.tests.keys())
    try:
        first = Pipeline(config, sink, config_dir=args.config_dir).run(source, workers=1)
        checksum_one = sink.checksum()
        counts_one = _counts(sink)

        second = Pipeline(config, sink, config_dir=args.config_dir).run(source, workers=1)
        checksum_two = sink.checksum()
        counts_two = _counts(sink)
    finally:
        sink.close()

    print(f"{'':22} {'run 1':>12} {'run 2':>12}")
    for table in counts_one:
        print(f"{table:22} {counts_one[table]:>12} {counts_two[table]:>12}")
    print(f"{'content checksum':22} {checksum_one:>12} {checksum_two:>12}")
    print(f"{'rows written':22} {first.rows_emitted:>12} {second.rows_emitted:>12}")

    identical = counts_one == counts_two and checksum_one == checksum_two
    print()
    if identical:
        print("IDEMPOTENT: the second run rewrote the same rows with the same values.")
        print("Row counts and content checksum are unchanged (NFR-3.2).")
        return 0
    print("NOT IDEMPOTENT: table contents changed on the second run.", file=sys.stderr)
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    sink = SqliteSink(args.db)
    try:
        if not sink.table_exists(args.view):
            print(f"{args.view!r} does not exist. Run the pipeline first.", file=sys.stderr)
            return 1
        rows = sink.query(f"SELECT * FROM {args.view} LIMIT ?", (args.limit,))
    finally:
        sink.close()
    if not rows:
        print("no rows", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows x {len(rows[0])} columns to {out}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = load_config(args.config_dir)
    print(config.describe())
    print("\nprofiles, most specific first:")
    for profile in config.profiles:
        print(f"  {profile.clinic_id:12} priority={profile.priority:<5} "
              f"match={profile.match} schema={profile.schema_version}")
    print("\npanels:")
    panels: dict[str, int] = {}
    for test in config.catalog.tests.values():
        panels[test.panel or "-"] = panels.get(test.panel or "-", 0) + 1
    for panel, count in sorted(panels.items(), key=lambda kv: -kv[1]):
        print(f"  {panel:16} {count} tests")
    return 0


def _counts(sink: SqliteSink) -> dict[str, int]:
    tables = ("clinical_document", "lab_result", "medication",
              "processing_error", "review_item", "file_ledger")
    return {t: sink.scalar(f"SELECT COUNT(*) FROM {t}") or 0 for t in tables}


def _print_summary(summary) -> None:
    data = summary.as_dict()
    width = 78
    print("=" * width)
    print(f"RUN {data['run_id']}    config {data['config_fingerprint']}    "
          f"{data['duration_seconds']}s    {data['worker_count']} worker(s)")
    print("=" * width)
    groups = (
        ("files", (("discovered", "files_discovered"), ("processed", "files_processed"),
                   ("skipped", "files_skipped_duplicate"), ("failed", "files_failed"))),
        ("documents", (("emitted", "documents_emitted"),
                       ("duplicate", "documents_duplicate"))),
        ("rows", (("emitted", "rows_emitted"), ("flagged", "rows_flagged"),
                  ("invalid", "rows_invalid"), ("outlier", "rows_outlier"),
                  ("out_of_range", "rows_out_of_range"))),
        ("medicines", (("emitted", "medications_emitted"),)),
        ("review", (("dead_letters", "errors_emitted"),
                    ("queue", "review_items_emitted"),
                    ("misaligned_reports", "reports_misaligned"))),
        ("genai", (("calls", "llm_calls"), ("cache_hits", "llm_cache_hits"))),
    )
    for title, keys in groups:
        parts = "  ".join(f"{label}={data[key]}" for label, key in keys)
        print(f"  {title:10} {parts}")
    if data["rows_emitted"]:
        rate = data["rows_emitted"] / max(data["duration_seconds"], 1e-9)
        print(f"  {'rate':10} {rate:,.0f} rows/s   "
              f"{data['files_processed'] / max(data['duration_seconds'], 1e-9):,.1f} files/s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="veritas",
        description="Veritas Claims medical-data standardisation pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start with:  python -m veritas demo",
    )
    parser.add_argument("--config-dir", default=None,
                        help="override the config directory")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pipeline over a path or gs:// prefix")
    run.add_argument("input")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--skip-seen", action="store_true",
                     help="skip files already processed in a previous run")
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="run the bundled sample data end to end")
    demo.add_argument("input", nargs="?", default=None)
    demo.add_argument("--workers", type=int, default=1)
    demo.add_argument("--keep", action="store_true", help="append to an existing database")
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="start the operational UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(func=cmd_serve)

    ddl = sub.add_parser("ddl", help="print DDL")
    ddl.add_argument("--dialect", choices=("sqlite", "bigquery"), default="sqlite")
    ddl.add_argument("--dataset", default="veritas_prod")
    ddl.add_argument("--views", action="store_true")
    ddl.set_defaults(func=cmd_ddl)

    onboard = sub.add_parser("onboard", help="field-coverage report for a clinic file")
    onboard.add_argument("file")
    onboard.set_defaults(func=cmd_onboard)

    bench = sub.add_parser("bench", help="synthetic throughput benchmark")
    bench.add_argument("--files", type=int, default=10_000)
    bench.add_argument("--workers", type=int, default=0)
    bench.add_argument("--rows-per-file", type=int, default=150)
    bench.add_argument("--keep-data", action="store_true")
    bench.add_argument(
        "--sink", choices=("none", "sqlite"), default="none",
        help="'none' measures processing cost, which is what sizes Cloud Run "
             "workers; 'sqlite' also measures the local write path")
    bench.add_argument("--out", default="bench/results.json")
    bench.set_defaults(func=cmd_bench)

    cost = sub.add_parser("cost", help="LLM-per-row vs LLM-on-the-tail cost model")
    cost.add_argument("--rows-per-day", type=int, default=30_000_000)
    cost.add_argument("--novel-names-per-day", type=int, default=2_000)
    cost.set_defaults(func=cmd_cost)

    verify = sub.add_parser("verify", help="run twice and prove idempotency")
    verify.add_argument("input", nargs="?", default=None)
    verify.set_defaults(func=cmd_verify)

    export = sub.add_parser("export", help="export a view to CSV")
    export.add_argument("--view", default="v_ideal_output_schema")
    export.add_argument("--out", default="out/export.csv")
    export.add_argument("--limit", type=int, default=100_000)
    export.set_defaults(func=cmd_export)

    config_cmd = sub.add_parser("config", help="show the loaded configuration")
    config_cmd.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                                  # noqa: BLE001
        log.exception("command failed")
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
