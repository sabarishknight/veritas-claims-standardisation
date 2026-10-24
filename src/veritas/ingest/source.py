"""File discovery (FR-1.1).

One interface, two implementations: a local directory and a GCS bucket. The
pipeline only ever sees :class:`FileRef`, so the same code path runs on a
laptop and on Cloud Run. The local source is not a toy - it is how the test
suite and the benchmark run, and it is what the reviewer uses.

Why read bytes and hash them here
---------------------------------
The content hash is the idempotency key (NFR-3.2) and the lineage anchor
(NFR-4.2), so it is computed once, at the boundary, before anything can
mutate the payload.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..observe.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class FileRef:
    """A file to process, with its identity already established."""

    uri: str
    sha256: str
    size_bytes: int
    raw: bytes
    # Partition hints lifted from the object path, where the convention
    # gs://bucket/clinic_id=X/dt=YYYY-MM-DD/file.json holds. Advisory only -
    # the authoritative source system comes from the envelope metadata, because
    # a clinic can and will upload to the wrong prefix.
    path_clinic_hint: str | None = None
    path_date_hint: str | None = None


class Source(Protocol):
    def discover(self) -> Iterator[FileRef]: ...


def _hints_from_path(parts: Iterable[str]) -> tuple[str | None, str | None]:
    clinic = date = None
    for part in parts:
        if part.startswith("clinic_id="):
            clinic = part.split("=", 1)[1] or None
        elif part.startswith("dt="):
            date = part.split("=", 1)[1] or None
    return clinic, date


class LocalSource:
    """Recursively read ``*.json`` under a directory.

    Files are returned in sorted order so a run is reproducible, which matters
    for the duplicate-suppression test: "first one wins" is only a meaningful
    rule if "first" is deterministic.
    """

    def __init__(self, root: str | Path, pattern: str = "*.json",
                 max_bytes: int = 20 * 1024 * 1024) -> None:
        # Resolved to absolute so every FileRef carries a stable, portable URI.
        # A relative path cannot be expressed as file:// at all, and a lineage
        # column that only means something from one working directory is not
        # lineage.
        self.root = Path(root).expanduser().resolve()
        self.pattern = pattern
        self.max_bytes = max_bytes

    def discover(self) -> Iterator[FileRef]:
        if not self.root.exists():
            raise FileNotFoundError(f"input path does not exist: {self.root}")
        paths = [self.root] if self.root.is_file() else sorted(self.root.rglob(self.pattern))
        for path in paths:
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > self.max_bytes:
                # Deliberately not read into memory. An oversized file is a DLQ
                # entry, not an OOM: one bad upload must not take out a worker.
                log.warning(
                    "file exceeds max_file_bytes; routing to dead-letter unread",
                    extra={"file_uri": path.as_uri(), "size_bytes": size,
                           "max_bytes": self.max_bytes},
                )
                yield FileRef(path.as_uri(), "", size, b"")
                continue
            raw = path.read_bytes()
            clinic, date = _hints_from_path(path.parts)
            yield FileRef(
                uri=path.as_uri(),
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=size,
                raw=raw,
                path_clinic_hint=clinic,
                path_date_hint=date,
            )


class GcsSource:
    """Read objects from a GCS prefix.

    Imports the client lazily so the core pipeline keeps zero cloud
    dependencies; ``pip install -e .[gcp]`` enables it. In the deployed
    topology this class is used by the backfill/replay path - steady-state
    traffic arrives as Pub/Sub notifications, one object per message, which
    hits :meth:`fetch_one`.
    """

    def __init__(self, bucket: str, prefix: str = "",
                 max_bytes: int = 20 * 1024 * 1024) -> None:
        self.bucket_name = bucket
        self.prefix = prefix
        self.max_bytes = max_bytes
        self._client = None

    def _bucket(self):
        if self._client is None:
            try:
                from google.cloud import storage  # type: ignore
            except ImportError as exc:                    # pragma: no cover
                raise SystemExit(
                    "google-cloud-storage is needed for gs:// input.\n"
                    "Install it with:  python3 -m pip install -e '.[gcp]'"
                ) from exc
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def discover(self) -> Iterator[FileRef]:
        for blob in self._bucket().list_blobs(prefix=self.prefix):
            if not blob.name.endswith(".json"):
                continue
            if blob.size and blob.size > self.max_bytes:
                log.warning("GCS object exceeds max_file_bytes",
                            extra={"file_uri": f"gs://{self.bucket_name}/{blob.name}"})
                yield FileRef(f"gs://{self.bucket_name}/{blob.name}", "", blob.size or 0, b"")
                continue
            raw = blob.download_as_bytes()
            clinic, date = _hints_from_path(blob.name.split("/"))
            yield FileRef(
                uri=f"gs://{self.bucket_name}/{blob.name}",
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                raw=raw,
                path_clinic_hint=clinic,
                path_date_hint=date,
            )

    def fetch_one(self, object_name: str) -> FileRef:
        """Single-object fetch, used by the Pub/Sub push handler."""
        blob = self._bucket().blob(object_name)
        raw = blob.download_as_bytes()
        clinic, date = _hints_from_path(object_name.split("/"))
        return FileRef(
            uri=f"gs://{self.bucket_name}/{object_name}",
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            raw=raw,
            path_clinic_hint=clinic,
            path_date_hint=date,
        )


def build_source(uri: str, max_bytes: int = 20 * 1024 * 1024) -> Source:
    """``gs://bucket/prefix`` or a local path."""
    if uri.startswith("gs://"):
        rest = uri[len("gs://"):]
        bucket, _, prefix = rest.partition("/")
        return GcsSource(bucket, prefix, max_bytes=max_bytes)
    if uri.startswith("file://"):
        uri = uri[len("file://"):]
    return LocalSource(Path(uri).expanduser(), max_bytes=max_bytes)
