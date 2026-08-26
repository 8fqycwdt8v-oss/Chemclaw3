"""Deterministic content-addressed hashing for identity keys across every layer.

Why one home: the calculation cache key (`chemclaw.science.calc.store`), the QM workflow id
(`chemclaw.connectors.calc.specs`), BO-candidate note ids (`chemclaw.connectors.bo.knowledge`), and
synthesized-memory note ids (`chemclaw.memory.ids`) are all "a stable short hash of some
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


def canonical_text(value: str) -> str:
    """Free text reduced to what it means: whitespace collapsed, case folded.

    **Only ever for building an identity**, never for storage or display: the words a person chose
    are what a draft renders and what a chemist reads back, and this deliberately loses them.

    It exists because the requester of every id in this system is a language model, and a model
    re-emits a value it has just read with the spacing and capitalisation it feels like — so a
    byte-exact key makes "asking the same question twice" true only for a byte-identical question.
    `agent/durable_tools._report_id` measured that and folded here first; `science/bo` found the
    same defect a level down, where the consequence is worse than a duplicate run: a campaign id is
    a hash of its decision space, so a re-cased category label mints a *new campaign with no
    history* and nothing raises.

    Two callers, one rule, and the rule is the interesting part — apply it to what a model
    authors, never to what identifies a principal. Folding two spellings of an actor or a role
    together would merge things that must not merge, which is why `_report_id` canonicalises its
    title and sections and leaves `requested_by` and `requested_roles` byte-exact.
    """
    return " ".join(value.split()).casefold()
