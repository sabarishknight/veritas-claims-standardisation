"""Test package.

The ``src`` layout means the package is not importable without installation,
so the path is set up here - in the package ``__init__`` - rather than in each
test module. That runs before any test module is imported, which keeps import
order inside the tests free and the linter happy.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
