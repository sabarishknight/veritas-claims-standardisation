"""Dead-letter payload store for the cloud path (FR-4.2).

The ``processing_error`` table records *why* a file failed, with enough context
to triage. This module keeps the *bytes*, by copying the offending object to a
dead-letter prefix:

    gs://<bucket>/_deadletter/<reason>/<YYYY-MM-DD>/<sha256>.json

Partitioning by reason first is deliberate. The question an on-call engineer
asks is never "what failed on Tuesday", it is "what is this new malformed_json
spike", and a reason-first prefix makes that one listing. Reprocessing is then
``veritas run gs://bucket/_deadletter/malformed_json/2026-10-24/`` once the
clinic has fixed its feed, and because identity is the content hash the reload
is idempotent (NFR-3.2).

Retention is a lifecycle rule on the bucket, not code (``infra/storage.tf``):
90 days, because a dead letter nobody triaged in 90 days is not going to be.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..observe.logging import get_logger

log = get_logger(__name__)


def deadletter_path(prefix: str, reason: str, sha256: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reason)
    name = sha256[:32] if sha256 else "unhashed"
    return f"{prefix.rstrip('/')}/_deadletter/{safe_reason}/{day}/{name}.json"


class GcsDeadLetter:
    """Copies failed payloads to a dead-letter prefix."""

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self.bucket_name = bucket
        self.prefix = prefix
        self._client = None

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage  # type: ignore
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def put(self, raw: bytes, reason: str, sha256: str) -> str:
        path = deadletter_path(self.prefix, reason, sha256)
        blob = self._bucket().blob(path)
        blob.metadata = {"veritas_reason": reason, "veritas_sha256": sha256}
        blob.upload_from_string(raw, content_type="application/json")
        log.info("payload written to dead-letter store",
                 extra={"path": path, "reason": reason})
        return f"gs://{self.bucket_name}/{path}"


class LocalDeadLetter:
    """Filesystem equivalent, used by the local run and the tests."""

    def __init__(self, root: str) -> None:
        from pathlib import Path
        self.root = Path(root)

    def put(self, raw: bytes, reason: str, sha256: str) -> str:
        from pathlib import Path
        path = Path(deadletter_path(str(self.root), reason, sha256))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path.as_uri()
