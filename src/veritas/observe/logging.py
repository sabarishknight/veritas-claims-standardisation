"""Structured logging (NFR-5.2).

Every log line is one JSON object carrying ``run_id``, ``trace_id``,
``correlation_id`` and ``file_sha256`` when they are known. That is what makes
"show me everything that happened to this one record" a single query in Cloud
Logging rather than a grep across workers:

    jsonPayload.correlation_id="b1458a7fe2ddad8233334cabc2c416c1"

The source already supplies ``traceId`` and ``correlationId``; we adopt them
rather than minting our own, so a trace started in the clinic's gateway stays
intact across our pipeline. Log-based metrics in ``infra/monitoring.tf`` are
built on these fields.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import time
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

# An immutable default: a mutable one would be shared across every context
# that never set it, so one worker's fields could leak into another's lines.
_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "veritas_log_context", default=_EMPTY
)

# Attributes LogRecord always carries; anything else an adapter attached is
# treated as structured payload.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, in the shape Cloud Logging indexes natively."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,            # Cloud Logging's key
            "time": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
            ) + f".{int(record.msecs):03d}Z",
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_CONTEXT.get())
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable form for local runs; the context is appended compactly."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _CONTEXT.get()
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        bits = " ".join(f"{k}={v}" for k, v in {**ctx, **extras}.items() if v is not None)
        head = f"{record.levelname:<7} {record.name:<28} {record.getMessage()}"
        line = f"{head}  {bits}" if bits else head
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure(level: str = "INFO", fmt: str = "json", stream: Any = None) -> None:
    """Install the root handler. Idempotent, so workers can call it freely."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # Our own log volume is the point; third-party chatter is not.
    for noisy in ("urllib3", "google", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log line emitted inside the block.

    Nested blocks merge, so the file-level context (``file_sha256``) is still
    present on a line emitted while processing one row of that file.
    """
    current = _CONTEXT.get()
    token = _CONTEXT.set({**current, **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


def configure_from(pipeline_cfg: dict[str, Any]) -> None:
    obs = pipeline_cfg.get("observability") or {}
    configure(
        level=os.environ.get("VERITAS_LOG_LEVEL", obs.get("log_level", "INFO")),
        fmt=os.environ.get("VERITAS_LOG_FORMAT", obs.get("log_format", "json")),
    )
