"""PII tokenisation (NFR-4.3).

Patient identifiers must be usable for linkage - "is this the same patient as
that claim?" - without the warehouse holding names. So identifiers are
replaced by a keyed HMAC-SHA256 token:

*   **Deterministic**: the same patient tokenises to the same value every
    time, so claims link across clinics and across months.
*   **Not reversible**: a token cannot be turned back into a name, and without
    the key it cannot be confirmed against a guessed name either. A plain
    SHA-256 would fail that second test - the space of Indian patient names is
    small enough to enumerate - which is why this is an HMAC with a secret
    key rather than a hash.
*   **Key-rotatable**: the key lives in Secret Manager in production and an
    environment variable locally. Rotating it breaks historical linkage by
    design; the migration path is to re-tokenise from the retained raw zone,
    which is why FR-4.3's audit trail is kept in a separately access-controlled
    place.

The supplied samples arrive already redacted (``[PATIENT NAME REDACTED]``), so
tokenisation is exercised against the synthetic identities in
``sample-data/edge-cases/``. Redaction markers are recognised and mapped to
``None`` rather than tokenised: hashing the marker would create one very
popular token that looks like a single extremely busy patient and would
corrupt every linkage metric built on it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterable

from ..observe.logging import get_logger
from .demographics import is_redacted

log = get_logger(__name__)

_DEV_KEY = b"veritas-local-development-key-not-for-production"
_TOKEN_LENGTH = 32          # 128 bits of the digest; collision-free at scale


class Tokeniser:
    """Keyed, deterministic pseudonymisation of identifier fields."""

    def __init__(
        self,
        mode: str = "tokenise",
        fields: Iterable[str] = (),
        keep_plaintext: Iterable[str] = (),
        key: bytes | None = None,
        key_env: str = "VERITAS_PII_KEY",
        redaction_markers: Iterable[str] = (),
    ) -> None:
        self.mode = mode
        self.fields = frozenset(fields)
        self.keep_plaintext = frozenset(keep_plaintext)
        self.markers = frozenset(redaction_markers)
        self._key = key or self._resolve_key(key_env)

    @staticmethod
    def _resolve_key(key_env: str) -> bytes:
        raw = os.environ.get(key_env)
        if raw:
            return raw.encode("utf-8")
        # Loud, once. A silent fallback to a well-known key in production
        # would make every token in the warehouse forgeable.
        log.warning(
            "no PII key in environment; using the development key. Set %s "
            "(or wire Secret Manager) before processing real patient data.",
            key_env,
        )
        return _DEV_KEY

    def token(self, value: str) -> str:
        return hmac.new(
            self._key, value.strip().casefold().encode("utf-8"), hashlib.sha256
        ).hexdigest()[:_TOKEN_LENGTH]

    def process(self, field: str, value: object) -> tuple[str | None, str | None]:
        """Return ``(token, plaintext)`` for one field under the current mode.

        ``plaintext`` is ``None`` unless the mode or the field allow-list
        permits it, so the default path cannot leak a name into the warehouse
        through a forgotten column.
        """
        if value is None:
            return None, None
        text = str(value).strip()
        if not text:
            return None, None

        if is_redacted(text, self.markers):
            # Already redacted upstream. Record nothing: no token, no value.
            return None, None

        if field in self.keep_plaintext or self.mode == "passthrough":
            return None, text
        if self.mode == "drop":
            return None, None
        if field in self.fields:
            return self.token(text), None
        return None, text
