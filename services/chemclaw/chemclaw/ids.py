"""Deterministic content-addressed hashing for identity keys across every layer.

Why one home: the calculation cache key (`calc.store`), the QM workflow id
(`workflows.models`), BO-candidate note ids (`workflows.bo_knowledge`), and
synthesized-memory note ids (`memory.ids`) are all "a stable short hash of some
canonical value". Before this they were four near-identical helpers that had
drifted — different digest lengths and, in one case, a weaker algorithm (SHA-1).
Centralizing the derivation makes every identity in the system share one
canonical-JSON + SHA-256 scheme, so equivalent inputs always collapse to the same
key and the digest strength is uniform (Rule of Three: four callers, one home).
"""

import hashlib
import json
from typing import Any

# Default digest width for a content-addressed key. 16 hex chars = 64 bits: enough
# that a collision between two distinct calculations is not a practical concern.
_DEFAULT_CHARS = 16


def stable_hash(payload: Any, *, chars: int = _DEFAULT_CHARS) -> str:
    """Return a stable short SHA-256 of the canonical JSON form of `payload`.

    Sorted keys and tight separators make the hash independent of dict ordering and
    whitespace, so semantically identical inputs collapse to the same key.
    `default=str` lets values that are not JSON-native serialize deterministically.

    Args:
        payload: Any JSON-serializable value (mapping, list, scalar).
        chars: Number of leading hex characters to keep (4 bits each). The default
            (16 → 64 bits) suits content-addressed keys; callers needing a shorter
            human-facing id can request fewer, accepting the weaker collision bound.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:chars]
