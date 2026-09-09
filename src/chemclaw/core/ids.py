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

    **`_stable_str` is the `default=` hook, and it is what makes "deterministically" true.** This
    docstring said `default=str` did that, and the word was false for
    the two classes whose `str()` is a property of the *run* rather than of the value, both
    measured (`D-2026-09-09-a-contract-checked-at-one-door-is-not-a-contract`): a `set` iterates
    in an order Python randomises per process, so one six-element set hashed to five digests
    under five `PYTHONHASHSEED` values; and an object overriding neither `__str__` nor `__repr__`
    renders its **memory address**, so one class hashed to `668cc845d7aa6ec5`, `f5fdffea01ba84c2`
    and `f4a76c115ad11869` in three processes. Both are now refused by name.

    Neither was on a live path — every caller reaches this with JSON-parsed data — and that is the
    argument for refusing rather than for leaving it: a docstring promising determinism is an
    invitation, and what this keys is the calculation cache, a BO campaign's decision space, a
    report id and a workflow id. `canonical_text` below records what the campaign case costs when
    an identity moves: not a duplicate run but a new campaign with no history, minted in silence.

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

    The `default=` hook, narrowed to exactly the two cases measured unstable and no further.
    An allow-list of accepted types was the alternative and is worse: `datetime`, `Decimal`,
    `Enum`, `UUID` and `Path` all have a `__str__` that is a property of the value, they are what
    actually reaches here, and an allow-list would have to refuse them to close a hole none of them
    is in.

    A `set` has no stable order and so no stable text. Sorting one instead of refusing it was
    considered and is not general — a heterogeneous set does not sort, and a set of dicts does not
    either — so the caller converts it to the ordered thing it means, which is a decision only the
    caller can take.

    An object that overrides neither `__str__` nor `__repr__` renders `<Cls object at 0x…>`.
    `_renders_itself` is what tells that apart from a type whose text *is* its value; a heuristic on
    the rendered string would be guessing at somebody else's `__repr__`.

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

    Asked of the MRO rather than by comparing `kind.__str__` against `object.__str__`: the identity
    check is the same test and `mypy --strict` refuses it as non-overlapping, because a bound
    `__str__` and an unbound one are different types to it. This reads the same fact off the class
    dictionaries — which class in the ancestry actually supplies the method — and is the clearer
    sentence anyway.

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
