"""Deterministic note ids for synthesized memory notes.

Why this exists: a campaign or playbook id must be a pure function of its member
set, so re-running synthesis over the same evidence proposes the *same* note
(idempotent through the PR-gate) instead of a duplicate under a fresh id. Both
jobs derive their ids identically, so the derivation lives once (DRY).
"""

from chemclaw.ids import stable_hash


def stable_id(prefix: str, member_ids: list[str]) -> str:
    """Return `<prefix>-<12 hex chars>` derived from the sorted member ids.

    Sorting makes the id independent of input order; the short SHA-256 digest is
    stable across runs and processes (unlike `hash()`). Uses the shared
    `chemclaw.ids.stable_hash`, so memory ids share the system-wide hashing scheme.
    """
    return f"{prefix}-{stable_hash(sorted(member_ids), chars=12)}"
