"""Regressions for bugs that were found and fixed during development.

Each of these was a real defect in an earlier revision. They are kept as tests
because every one of them was *silent* - the pipeline produced plausible output
and nothing failed - which is exactly the class of bug that returns.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from veritas.load.sqlite_sink import SqliteSink
from veritas.observe.logging import configure
from veritas.pipeline import Pipeline
from veritas.standardise.numeric import parse_result

from .support import ROOT, catalog, config

configure(level="CRITICAL", fmt="text")


class BlankUnitIsNotDimensionless(unittest.TestCase):
    """An empty unit column once resolved to the `dimensionless_ratio`
    dimension, so every blank-unit row failed the coherence check against its
    own test - 64 false findings in one sample file."""

    def test_blank_unit_has_no_dimension(self) -> None:
        self.assertIsNone(catalog().unit_spec(""))

    def test_a_blank_unit_row_is_not_judged_incoherent(self) -> None:
        from veritas.standardise.ranges import parse_range
        from veritas.validate.coherence import CoherenceEngine
        engine = CoherenceEngine(catalog(), config().section("coherence"))
        verdict = engine.check_row(
            0, catalog().tests["HAEMOGLOBIN"], "", parse_range("13-17"))
        self.assertTrue(verdict.coherent)


class ThousandsCommaIsOneValue(unittest.TestCase):
    """`4,290 cells/cu.mm` was read as a packed multi-value cell and
    discarded, losing a perfectly good result."""

    def test_a_thousands_comma_does_not_look_like_a_packed_panel(self) -> None:
        result = parse_result("4,290 cells/cu.mm")
        self.assertEqual(result.value, 4290.0)
        self.assertEqual(result.flags, [])


class UnitsConvertBeforeClassification(unittest.TestCase):
    """A body temperature of `98.6 degree F` was classified against a
    canonical envelope in degC and flagged as a wild outlier."""

    def test_fahrenheit_is_not_an_outlier(self) -> None:
        from veritas.models import Analytics
        from veritas.standardise.ranges import parse_range
        from veritas.validate.rules import classify, harmonise
        test = catalog().tests["BODY TEMPERATURE"]
        result = parse_result("98.6", test=test)
        harmonised = harmonise(catalog(), result, parse_range(""), "degree F",
                               test, "Temp")
        result.value = harmonised.value
        verdict = classify(result, parse_range(""), test)
        self.assertEqual(harmonised.value, 37.0)
        self.assertIs(verdict.analytics, Analytics.WITHIN_RANGE)


class RerunRefreshesRatherThanSkipping(unittest.TestCase):
    """A document was treated as its own duplicate on reprocessing, so a
    rerun emitted zero rows. Reprocessing after a catalogue fix would have
    silently left stale values in the warehouse."""

    def test_a_second_run_re_emits_every_row(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="veritas-rerun-"))
        try:
            sink = SqliteSink(tmp / "rerun.db")
            source = str(ROOT / "sample-data" / "incoming")
            first = Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                source, workers=1)
            second = Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                source, workers=1)
            self.assertGreater(first.rows_emitted, 0)
            self.assertEqual(
                first.rows_emitted, second.rows_emitted,
                "a rerun must re-emit and refresh rows, not skip them")
            sink.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReviewQueueIsStableAcrossRuns(unittest.TestCase):
    """Review item ids included the run id, so every rerun refilled the queue
    with duplicates and an analyst's dismissed items came back."""

    def test_the_queue_does_not_grow_on_a_rerun(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="veritas-queue-"))
        try:
            sink = SqliteSink(tmp / "queue.db")
            source = str(ROOT / "sample-data" / "incoming")
            Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                source, workers=1)
            after_first = sink.scalar("SELECT COUNT(*) FROM review_item")
            Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                source, workers=1)
            after_second = sink.scalar("SELECT COUNT(*) FROM review_item")
            self.assertEqual(after_first, after_second)
            sink.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SlottedModelsRejectStrayAttributes(unittest.TestCase):
    """Fields were once attached with setattr onto slotted dataclasses, which
    raised at runtime and sent every row to the dead-letter store."""

    def test_lab_result_requires_its_document_key(self) -> None:
        from veritas.models import LabResult
        self.assertIn("document_pk", LabResult.__slots__)

    def test_medication_carries_strength_as_a_field(self) -> None:
        from veritas.models import Medication
        self.assertIn("strength", Medication.__slots__)


class DeadLetterReasonsAreCodes(unittest.TestCase):
    """A reason once arrived as the sentence 'empty test name', which cannot
    be grouped on."""

    def test_reasons_contain_no_spaces(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="veritas-reason-"))
        try:
            sink = SqliteSink(tmp / "reason.db")
            Pipeline(config(), sink, config_dir=str(ROOT / "config")).run(
                str(ROOT / "sample-data"), workers=1)
            reasons = [r["reason"] for r in sink.query(
                "SELECT DISTINCT reason FROM processing_error")]
            self.assertTrue(reasons)
            for reason in reasons:
                with self.subTest(reason=reason):
                    self.assertNotIn(" ", reason)
            sink.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class ResolutionIsDeterministicAcrossProcesses(unittest.TestCase):
    """Fuzzy shortlist ties were broken by set iteration order, so the same
    input resolved to *different tests* in different processes depending on
    PYTHONHASHSEED.

    This is the worst class of bug this pipeline can have: both answers look
    perfectly reasonable on their own, nothing fails, and the output is simply
    not reproducible - which defeats the audit trail the whole design exists
    to provide. An in-process test cannot catch it, so this one forks.
    """

    PROBE = r'''
import sys
sys.path.insert(0, "src")
from veritas.config import load_config
from veritas.standardise.names import NameResolver

catalog = load_config("config").catalog
# Deliberately loose thresholds: they force every name through the fuzzy
# tier, which is where the non-determinism lived.
resolver = NameResolver(catalog, accept_threshold=0.1, review_threshold=0.1,
                        affix_tolerance=3)
names = ["SALMONELLA TYPHI O` , SERUM", "aemoglobin", "atelet Count",
         "lac/cmm", "BC INDICES", "eutrophils", "tal WBC Count",
         "lean Corp Volume MCV", "DW-CV", "ematocrit HCT"]
print("|".join(str(resolver.resolve(n).canonical) for n in names))
'''

    def test_same_answer_under_every_hash_seed(self) -> None:
        import os
        import subprocess
        import sys

        results = set()
        for seed in ("0", "1", "2", "3", "42"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            completed = subprocess.run(
                [sys.executable, "-c", self.PROBE],
                capture_output=True, text=True, cwd=str(ROOT), env=env, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-400:])
            results.add(completed.stdout.strip())

        self.assertEqual(
            len(results), 1,
            "name resolution is not reproducible across processes:\n"
            + "\n".join(sorted(results)),
        )


class RunsOnTheOldestSupportedPython(unittest.TestCase):
    """`enum.StrEnum` was introduced by a lint auto-fix and silently raised the
    minimum interpreter to 3.11, breaking the suite on 3.10 - which is what the
    reviewer's machine happened to run.

    A dependency floor the project did not choose is a portability bug, not a
    style question, so the floor is asserted here rather than left to a comment
    in pyproject.toml.
    """

    MINIMUM = (3, 10)

    def test_the_declared_floor_matches_what_the_code_needs(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        expected = f'requires-python = ">={self.MINIMUM[0]}.{self.MINIMUM[1]}"'
        self.assertIn(expected, text)

    # (module, attribute) pairs that do not exist on the declared floor.
    # Matched only as a qualified attribute or an explicit `from X import Y`,
    # never as a bare name - a local variable called `override` is not
    # `typing.override`, and a scan that cannot tell the difference produces
    # false failures nobody will trust.
    TOO_NEW = {
        ("enum", "StrEnum"): "3.11+",
        ("enum", "ReprEnum"): "3.11+",
        ("itertools", "batched"): "3.12+",
        ("typing", "override"): "3.12+",
        ("typing", "Self"): "3.11+",
        ("datetime", "UTC"): "3.11+",
        ("asyncio", "TaskGroup"): "3.11+",
    }

    def test_no_module_uses_a_feature_newer_than_the_floor(self) -> None:
        import ast

        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    key = (node.value.id, node.attr)
                    if key in self.TOO_NEW:
                        offenders.append(
                            f"{path.name}: {key[0]}.{key[1]} is {self.TOO_NEW[key]}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        key = (node.module, alias.name)
                        if key in self.TOO_NEW:
                            offenders.append(
                                f"{path.name}: from {key[0]} import {key[1]} "
                                f"is {self.TOO_NEW[key]}")
        self.assertEqual(offenders, [], "code requires a newer Python than declared")

    def test_the_scan_actually_detects_a_violation(self) -> None:
        """A guard that cannot fail is not a guard."""
        import ast

        tree = ast.parse("import enum\nclass X(enum.StrEnum): pass\n")
        found = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in self.TOO_NEW
        ]
        self.assertEqual(len(found), 1)


class WorksOnFilesystemsWithoutWal(unittest.TestCase):
    """SQLite's WAL mode needs shared memory, which network filesystems do not
    provide. On a mounted or synced folder the first write failed with a bare
    `disk I/O error` - a useless message for a reviewer who simply cloned the
    repo onto a shared drive.
    """

    def test_journal_mode_is_verified_not_assumed(self) -> None:
        source = (ROOT / "src" / "veritas" / "load" / "sqlite_sink.py").read_text()
        self.assertIn("journal_mode=DELETE", source,
                      "there must be a fallback when WAL does not take")
        self.assertIn("cur.fetchone()", source,
                      "the pragma result must be read back, not assumed")

    def test_the_sink_opens_and_writes_whatever_the_journal_mode(self) -> None:
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="veritas-journal-"))
        try:
            sink = SqliteSink(tmp / "j.db")
            sink.initialise(catalog().tests.keys())
            mode = sink.scalar("PRAGMA journal_mode")
            self.assertIn(str(mode).lower(), ("wal", "delete", "truncate",
                                              "persist", "memory", "off"))
            self.assertTrue(sink.table_exists("lab_result"))
            sink.close()
        except sqlite3.DatabaseError as exc:   # pragma: no cover
            self.fail(f"sink could not initialise: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
