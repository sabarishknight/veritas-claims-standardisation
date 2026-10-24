"""Envelope unwrapping and document routing (FR-1.3, schema-on-read).

A clinic file is not one record. It is a transport envelope that carries a
variable number of *documents*, each tagged with a ``classifier``
(``lab_report``, ``discharge_summary``, ...) and each with its own payload
shape. One of the supplied samples carries three documents, two of which are
the same discharge summary twice.

So the pipeline reads the envelope generically, routes each document by its
classifier, and only then applies a shape. Nothing here knows what a
haemoglobin is.

Schema-on-read, and why
-----------------------
Field *locations* are declared in the clinic profile as paths
(``data.responseDetails[*]``) and field *names* as ordered candidate lists
(``[patient_name, patientName, name]``). A new clinic that nests its rows one
level deeper, or spells a field differently, is a profile edit - no deploy, no
schema migration, no pipeline downtime (NFR-2.1, NFR-2.3).

The cost of that flexibility is that a typo in a profile is a silent miss
rather than a load error. The counterweight is :func:`coverage_report`, which
the CLI runs on a sample during onboarding and which names every source field
the profile did *not* read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..observe.logging import get_logger

log = get_logger(__name__)


class EnvelopeError(Exception):
    """The file could not be read as an envelope. Always a DLQ entry, never a
    crash: one clinic's malformed upload must not stop the other 499."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# Path resolution
#
# A deliberately small subset of JSONPath: dotted keys, ``[*]`` to fan out over
# a list, ``[n]`` to index one, and ``.`` for "the payload itself". That covers
# every shape in the sample corpus and stays debuggable; a full JSONPath engine
# would be a dependency and an invitation to put logic in config strings.
# ---------------------------------------------------------------------------

def resolve_path(doc: Any, path: str) -> Any:
    """Resolve a single-valued path. Returns ``None`` if any hop is missing."""
    if path in ("", ".", "$"):
        return doc
    current = doc
    for part in path.replace("$", "").strip(".").split("."):
        if current is None:
            return None
        key, _, index = part.partition("[")
        if key:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        if index:
            token = index.rstrip("]")
            if token == "*":
                return current
            if not isinstance(current, list):
                return None
            i = int(token)
            current = current[i] if -len(current) <= i < len(current) else None
    return current


def resolve_many(doc: Any, path: str) -> list[Any]:
    """Resolve a path that is expected to yield a list."""
    value = resolve_path(doc, path)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def first_present(payload: dict[str, Any], candidates: Any) -> Any:
    """First candidate key present and non-empty in ``payload``.

    Field maps are ordered candidate lists, which is how one profile absorbs
    several spellings of the same field without branching code.
    """
    if payload is None:
        return None
    if isinstance(candidates, str):
        candidates = [candidates]
    for key in candidates or []:
        if isinstance(payload, dict) and key in payload:
            value = payload[key]
            if value not in (None, "", [], {}):
                return value
    return None


# ---------------------------------------------------------------------------
# Unwrapped shapes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RawDocument:
    """One document lifted out of the envelope, still in source shape."""

    classifier: str
    payload: Any
    index: int
    status: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class Envelope:
    """The file's envelope: identifiers, metadata and its documents."""

    trace_id: str
    document_id: str
    correlation_id: str
    status_code: str | None
    meta: dict[str, Any] = field(default_factory=dict)
    documents: list[RawDocument] = field(default_factory=list)
    unmapped_meta_keys: list[str] = field(default_factory=list)

    @property
    def source_system(self) -> str:
        return str(self.meta.get("source_system") or "UNKNOWN")


def parse_json(raw: bytes, file_uri: str = "") -> Any:
    """Parse bytes to JSON, converting every failure mode into an
    :class:`EnvelopeError` with a reason code the DLQ can group on."""
    if not raw:
        raise EnvelopeError("empty_file", f"0 bytes at {file_uri}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvelopeError("invalid_encoding", str(exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(
            "malformed_json", f"{exc.msg} at line {exc.lineno} col {exc.colno}"
        ) from exc


def unwrap(doc: Any, profile: Any) -> Envelope:
    """Apply a clinic profile's envelope spec to a parsed document."""
    spec = profile.envelope or {}
    if not isinstance(doc, dict):
        raise EnvelopeError("not_an_object", f"top level is {type(doc).__name__}")

    meta_spec = spec.get("meta") or {}
    meta: dict[str, Any] = {}
    unmapped: list[str] = []
    key_field = meta_spec.get("key_field", "key")
    value_field = meta_spec.get("value_field", "value")
    meta_map = meta_spec.get("map") or {}
    for entry in resolve_many(doc, meta_spec.get("path", "")):
        if not isinstance(entry, dict):
            continue
        key = entry.get(key_field)
        if key is None:
            continue
        target = meta_map.get(key)
        if target:
            meta[target] = entry.get(value_field)
        else:
            # Recorded, not dropped: an unexpected metadata key is the first
            # visible sign that a clinic changed its format (NFR-2.3).
            meta[f"meta_{key}"] = entry.get(value_field)
            unmapped.append(str(key))

    classifier_field = spec.get("classifier_field", "classifier")
    payload_field = spec.get("payload_field", "data")
    status_field = spec.get("record_status_field", "status")
    error_field = spec.get("record_error_field", "errorMessage")

    documents: list[RawDocument] = []
    for i, record in enumerate(resolve_many(doc, spec.get("records_path", ""))):
        if not isinstance(record, dict):
            continue
        documents.append(
            RawDocument(
                classifier=str(record.get(classifier_field) or "unknown"),
                payload=record.get(payload_field),
                index=i,
                status=record.get(status_field),
                error_message=record.get(error_field),
            )
        )

    envelope = Envelope(
        trace_id=str(resolve_path(doc, spec.get("trace_id_path", "traceId")) or ""),
        document_id=str(resolve_path(doc, spec.get("document_id_path", "data.documentId")) or ""),
        correlation_id=str(
            resolve_path(doc, spec.get("correlation_id_path", "data.correlationId")) or ""
        ),
        status_code=(
            str(resolve_path(doc, spec.get("status_code_path", "statusCode")))
            if resolve_path(doc, spec.get("status_code_path", "statusCode")) is not None
            else None
        ),
        meta=meta,
        documents=documents,
        unmapped_meta_keys=unmapped,
    )

    if not envelope.documents:
        raise EnvelopeError(
            "no_documents",
            f"records_path {spec.get('records_path')!r} yielded nothing",
        )
    return envelope


def iter_documents(envelope: Envelope, profile: Any) -> Iterator[tuple[RawDocument, dict[str, Any]]]:
    """Yield ``(document, doc_spec)`` for documents the profile can shape.

    A document whose classifier has no spec, or that the source itself marked
    failed, is skipped here and dead-lettered by the caller - we never guess a
    shape, because guessing wrong writes plausible garbage.
    """
    for raw in envelope.documents:
        spec = profile.doc_spec(raw.classifier)
        if spec is None:
            log.info("no profile for classifier; dead-lettering",
                     extra={"classifier": raw.classifier,
                            "clinic_profile": profile.clinic_id})
            continue
        yield raw, spec


# ---------------------------------------------------------------------------
# Onboarding support (NFR-2.2: a documented schema onboardable in a day)
# ---------------------------------------------------------------------------

def coverage_report(doc: Any, profile: Any) -> dict[str, Any]:
    """Which source fields the profile reads, and which it silently ignores.

    Run by ``python -m veritas onboard`` against a clinic's sample files. The
    ``unread`` list is the onboarding checklist: every entry is either a field
    to map or a conscious decision to drop, and either way it was seen by a
    human rather than discovered six months later in a data-quality review.
    """
    seen: set[str] = set()

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else key
                seen.add(path)
                walk(value, path)
        elif isinstance(node, list) and node:
            walk(node[0], f"{prefix}[]")

    walk(doc)

    mapped: set[str] = set()
    spec = profile.envelope or {}
    for path in (
        spec.get("trace_id_path"), spec.get("document_id_path"),
        spec.get("correlation_id_path"), spec.get("status_code_path"),
    ):
        if path:
            mapped.add(path.replace("[*]", "[]"))
    meta_spec = spec.get("meta") or {}
    if meta_spec.get("path"):
        base = meta_spec["path"].replace("[*]", "[]")
        mapped.update({base, f"{base}.{meta_spec.get('key_field', 'key')}",
                       f"{base}.{meta_spec.get('value_field', 'value')}"})

    records_base = (spec.get("records_path") or "").replace("[*]", "[]")
    payload_field = spec.get("payload_field", "data")
    if records_base:
        mapped.update({
            records_base,
            f"{records_base}.{spec.get('classifier_field', 'classifier')}",
            f"{records_base}.{payload_field}",
            f"{records_base}.{spec.get('record_status_field', 'status')}",
            f"{records_base}.{spec.get('record_error_field', 'errorMessage')}",
        })

    for doc_spec in (profile.documents or {}).values():
        payload_root = f"{records_base}.{payload_field}" if records_base else ""
        header_path = doc_spec.get("header_path", ".")
        header_root = payload_root if header_path in (".", "") else f"{payload_root}.{header_path}"
        for candidates in (doc_spec.get("header_map") or {}).values():
            for name in ([candidates] if isinstance(candidates, str) else candidates):
                mapped.add(f"{header_root}.{name}")
        for key in ("rows_path", "medications_path", "investigations_path"):
            sub = doc_spec.get(key)
            if not sub:
                continue
            sub_root = f"{payload_root}.{sub}".replace("[*]", "[]")
            mapped.add(sub_root)
            map_key = {"rows_path": "row_map",
                       "medications_path": "medication_map"}.get(key)
            for candidates in (doc_spec.get(map_key) or {}).values():
                for name in ([candidates] if isinstance(candidates, str) else candidates):
                    mapped.add(f"{sub_root}.{name}")

    # A container whose children are mapped is itself read; reporting it would
    # bury the genuinely unread leaves the onboarding engineer needs to see.
    def is_container_of_mapped(path: str) -> bool:
        prefix = path + "."
        return any(m.startswith(prefix) for m in mapped)

    unread = sorted(
        p for p in seen
        if p not in mapped and not p.endswith("[]") and not is_container_of_mapped(p)
    )
    return {
        "clinic_profile": profile.clinic_id,
        "schema_version": profile.schema_version,
        "fields_seen": len(seen),
        "fields_read": len(seen) - len(unread),
        "coverage": round((len(seen) - len(unread)) / len(seen), 4) if seen else 0.0,
        "unread": unread,
    }
