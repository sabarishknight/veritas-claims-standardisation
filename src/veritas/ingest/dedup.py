"""Duplicate suppression (FR-1.2) and deterministic identity (NFR-3.2).

The finding that shaped this module
-----------------------------------
``Sample_JSON_file1.json`` and ``Sample_JSON_file3.json`` carry the *same*
discharge summary - identical admission and discharge dates, diagnosis,
examination text and all 18 medication lines - under three different
identifiers:

    file1: documentId=DOC01Q6V9MH7F2V97XBAR3CPDLALSH  claim=...238343
    file3: documentId=DOC01753KEPCMVLML3B3VTYA5KOS6I  claim=...842817

An identity-based deduplicator (``DISTINCT document_id``) sees two records and
the claim is adjudicated twice. The same file also demonstrates the inverse
problem: ``Sample_JSON_file5.json`` carries one discharge summary twice inside
a single envelope.

So identity cannot come from the source. It comes from the content: a SHA-256
over the clinical payload with transport fields removed. Which fields are
transport is a config decision (``dedup.content_hash_excludes``), because the
answer differs by source - and a clinic that genuinely resubmits a corrected
report needs a *different* hash, which is exactly what including the clinical
fields and excluding the identifiers achieves.

Three strategies are offered and the choice is per clinic, because this is a
judgement about a source's behaviour, not a universal truth:

    content_hash   default. Catches resubmission under new identifiers.
    document_id    cheapest. Correct only for sources that never resubmit.
    composite_key  patient + encounter dates + document type. Catches
                   resubmission even when the payload was lightly edited,
                   at the cost of suppressing genuine corrections.

Row identity
------------
``row_id`` is a hash of (document identity, canonical test, occurrence index).
It is deterministic across runs and across workers, which is what makes the
loader a MERGE rather than an INSERT, and therefore makes reruns free of
duplicates (NFR-3.2) without a distributed lock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_HASH_PREFIX_BITS = 128           # 32 hex chars: collision-free at our volumes


def _stable_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, stable float formatting.

    Stability matters more than beauty. The same payload must hash identically
    on any machine, any Python version, in any key order the clinic sent.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def _strip_keys(value: Any, excludes: frozenset[str]) -> Any:
    """Recursively drop excluded keys, case-insensitively.

    Case-insensitive because the same logical field arrives as ``claim_no``,
    ``claimNo`` and ``CLAIM_NO`` across sources, and a hash that depends on
    casing is a hash that silently stops deduplicating.
    """
    if isinstance(value, dict):
        return {
            k: _strip_keys(v, excludes)
            for k, v in value.items()
            if k.casefold() not in excludes
        }
    if isinstance(value, list):
        return [_strip_keys(v, excludes) for v in value]
    return value


def content_hash(payload: Any, excludes: Iterable[str] = ()) -> str:
    """Content identity of a clinical payload."""
    folded = frozenset(e.casefold() for e in excludes)
    stripped = _strip_keys(payload, folded)
    digest = hashlib.sha256(_stable_json(stripped).encode("utf-8")).hexdigest()
    return digest[: _HASH_PREFIX_BITS // 4]


def composite_key(header: dict[str, Any], record_type: str) -> str:
    """Encounter-level identity: who, when, what kind of document."""
    parts = [
        record_type,
        str(header.get("patient_name") or header.get("patient_token") or ""),
        str(header.get("uhid") or header.get("uhid_token") or ""),
        str(header.get("admission_date") or ""),
        str(header.get("discharge_date") or ""),
        str(header.get("report_date") or header.get("reports_date") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[: _HASH_PREFIX_BITS // 4]


def document_identity(
    *,
    strategy: str,
    payload: Any,
    header: dict[str, Any],
    record_type: str,
    document_id: str,
    source_row_index: int,
    excludes: Iterable[str] = (),
) -> str:
    """Identity under the configured strategy.

    ``document_id`` is combined with ``source_row_index`` so that two documents
    inside one envelope do not collide - without it, the two discharge
    summaries in file 5 would be indistinguishable *before* we have decided
    whether they are duplicates, which would make the duplicate finding
    unprovable.
    """
    if strategy == "document_id":
        return hashlib.sha256(
            f"{document_id}|{source_row_index}|{record_type}".encode()
        ).hexdigest()[: _HASH_PREFIX_BITS // 4]
    if strategy == "composite_key":
        return composite_key(header, record_type)
    return content_hash(payload, excludes)


def row_id(document_pk: str, canonical_or_original: str, occurrence: int) -> str:
    """Deterministic primary key for one test observation."""
    return hashlib.sha256(
        f"{document_pk}|{canonical_or_original}|{occurrence}".encode()
    ).hexdigest()[: _HASH_PREFIX_BITS // 4]


def medication_id(document_pk: str, medicine: str, occurrence: int) -> str:
    return hashlib.sha256(
        f"{document_pk}|med|{medicine}|{occurrence}".encode()
    ).hexdigest()[: _HASH_PREFIX_BITS // 4]


# ---------------------------------------------------------------------------
# Within-report row collapsing
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RowCollapse:
    kept: list[dict[str, Any]]
    collapsed: int


def collapse_repeat_rows(
    rows: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...] = ("test_name", "result", "range", "unit"),
    enabled: bool = True,
    keep_first: bool = True,
) -> RowCollapse:
    """Collapse rows repeated verbatim inside one report.

    Multi-page OCR duplicates whole panels: ``Sample_JSON_file4.json`` repeats
    ``PLATELET COUNT`` six times and ``ALBUMIN`` six times, with identical
    values each time, because the same panel appears on several pages of the
    PDF.

    The key deliberately includes ``result``: two draws of the same test on the
    same day with *different* values are a genuine clinical series and must
    both survive - that is a falling haemoglobin, which is exactly the signal a
    claims analyst is looking for. Only verbatim repeats collapse. ``page_no``
    is deliberately *excluded* from the key, since the same value on two pages
    is the artefact we are removing.
    """
    if not enabled:
        return RowCollapse(rows, 0)

    seen: set[tuple[Any, ...]] = set()
    kept: list[dict[str, Any]] = []
    collapsed = 0
    for row in rows:
        key = tuple(str(row.get(f, "")).strip().casefold() for f in key_fields)
        if key in seen:
            collapsed += 1
            if not keep_first:
                for i, existing in enumerate(kept):
                    if tuple(
                        str(existing.get(f, "")).strip().casefold() for f in key_fields
                    ) == key:
                        kept[i] = row
                        break
            continue
        seen.add(key)
        kept.append(row)
    return RowCollapse(kept, collapsed)


class SeenRegistry:
    """In-run duplicate tracker.

    Deliberately in-memory and per-run. Cross-run duplicate suppression is the
    loader's MERGE on a deterministic primary key, which is durable and does
    not need a cache that could disagree with the database. Keeping those two
    mechanisms separate means a cache miss can never produce a duplicate row -
    it can only produce a redundant MERGE.
    """

    __slots__ = ("_seen", "duplicates")

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self.duplicates = 0

    def check(self, identity: str, where: str) -> str | None:
        """Register ``identity``; return where it was first seen if duplicate."""
        first = self._seen.get(identity)
        if first is not None:
            self.duplicates += 1
            return first
        self._seen[identity] = where
        return None

    def __len__(self) -> int:
        return len(self._seen)
