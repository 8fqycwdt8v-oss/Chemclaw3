"""Deterministic content-addressed hashing for identity keys across every layer.

The calculation cache key, workflow ids, BO-candidate and synthesized-memory note ids all derive
from one canonical-JSON + SHA-256 scheme, so equivalent inputs collapse to the same key with
uniform digest strength.
"""

import hashlib
import json
from typing import Any

# Default digest width for a content-addressed key. 16 hex chars = 64 bits: enough
# that a collision between two distinct calculations is not a practical concern.
_DEFAULT_CHARS = 16


def stable_hash(payload: Any, *, chars: int = _DEFAULT_CHARS) -> str:
    """Return a stable short SHA-256 of the canonical JSON form of `payload`.

    Sorted keys and tight separators make the hash independent of dict ordering and whitespace.
    Values JSON cannot encode go through `_stable_str`, which refuses those whose `str()` varies per
    process (sets, objects rendering their address), because these keys identify cache rows,
    campaigns, reports and workflows.

    Args:
        payload: Any JSON-serializable value (mapping, list, scalar).
        chars: Number of leading hex characters to keep (4 bits each). The default
            (16 → 64 bits) suits content-addressed keys; callers needing a shorter
            human-facing id can request fewer, accepting the weaker collision bound.

    Raises:
        TypeError: `payload` contains a value whose `str()` is not stable across processes, naming
            the value — a `set`/`frozenset`, or an object that renders as its address.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_stable_str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:chars]


def _stable_str(value: Any) -> str:
    """`str(value)` for a value JSON cannot encode, or `TypeError` where that string is not stable.

    Refuses only the two unstable cases. A set has no stable order and may not be sortable, so the
    caller converts it. An object overriding neither `__str__` nor `__repr__` renders its address.
    Types like `datetime`, `Decimal`, `Enum`, `UUID` and `Path` pass through.

    Args:
        value: The value `json.dumps` could not encode itself.

    Raises:
        TypeError: `value` has no stable string form, naming the type and why.
    """
    if isinstance(value, set | frozenset):
        raise TypeError(
            f"stable_hash cannot hash a {type(value).__name__}: its iteration order is randomised "
            "per process, so the digest is not stable across runs. Pass a sorted list instead — "
            "which order it should have is the caller's decision, not this function's."
        )
    kind = type(value)
    if not _renders_itself(kind):
        raise TypeError(
            f"stable_hash cannot hash a {kind.__name__}: it overrides neither `__str__` nor "
            "`__repr__`, so `str()` renders its memory address and the digest is not stable across "
            "runs. Pass the value it stands for — a dict, a `model_dump(mode='json')`, an id."
        )
    return str(value)


def _renders_itself(kind: type) -> bool:
    """Whether `kind` gives `str()` a text of its own, rather than inheriting `object`'s address.

    Read off the MRO's class dictionaries; an identity check against `object.__str__` is rejected by
    `mypy --strict`.

    Args:
        kind: The type of the value being hashed.
    """
    return any(
        "__str__" in base.__dict__ or "__repr__" in base.__dict__
        for base in kind.__mro__
        if base is not object
    )


def canonical_text(value: str) -> str:
    """Free text reduced to what it means: whitespace collapsed, case folded.

    Only for building an identity, never for storage or display. Ids here are requested by a model
    that re-emits text with arbitrary spacing and case, so a byte-exact key would mint a new report
    or
    a new BO campaign with no history for the same question. Apply it to what a model authors, never
    to what identifies a principal (actors and roles stay byte-exact).
    """
    return " ".join(value.split()).casefold()
