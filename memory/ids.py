"""Deterministic note ids for synthesized memory notes.

Why this exists: a campaign or playbook id must stay stable while its evidence grows,
so periodic re-synthesis over a *grown* corpus updates the existing note in place
through the idempotent PR-gate branch (`note/<id>`) instead of minting a fresh note
beside the stale one. Both jobs derive their ids identically, so the derivation lives
once (DRY).
"""

from chemclaw.ids import stable_hash


def stable_id(prefix: str, member_ids: list[str]) -> str:
    """Return `<prefix>-<12 hex chars>` keyed on the cluster's smallest member id.

    The anchor is the *smallest* member id, not the full member set: hashing the exact
    set would mint a brand-new id whenever a cluster gains a member (routine under
    periodic ELN sync), leaving the already-merged subset note in the graph as stale
    "current" knowledge with no supersede link. Anchoring on the smallest member keeps
    the id — and therefore the PR-gate branch and the merged file path — stable as the
    cluster grows, so the grown note supersedes the old one in place. Clusters within
    one synthesis run are disjoint (connected components / similarity partitions), so
    anchors never collide. Uses the shared `chemclaw.ids.stable_hash`, so memory ids
    share the system-wide hashing scheme (stable across runs and processes).
    """
    return f"{prefix}-{stable_hash(min(member_ids), chars=12)}"
