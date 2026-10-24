"""Shared test fixtures.

Tests run on stdlib ``unittest`` so ``python3 -m unittest discover -s tests -t .``
works with nothing installed; pytest discovers these classes too, so ``make
test`` works either way. The ``sys.path`` setup lives in ``tests/__init__.py``.
"""

from __future__ import annotations

from pathlib import Path

from veritas.config import load_config

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "sample-data"
INCOMING = SAMPLE_DIR / "incoming"
EDGE_CASES = SAMPLE_DIR / "edge-cases"

_CONFIG = None


def config():
    """One config load for the whole suite; it is read-only."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config(ROOT / "config")
    return _CONFIG


def catalog():
    return config().catalog


def test(canonical: str):
    return config().catalog.tests[canonical]
