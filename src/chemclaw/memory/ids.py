"""Deterministic note ids for synthesized memory notes.

Why this exists: a campaign or playbook id must stay stable while its evidence grows,
so periodic re-synthesis over a *grown* corpus updates the existing note in place
through the idempotent PR-gate branch (`note/<id>`) instead of minting a fresh note
beside the stale one. Both jobs derive their ids identically, so the derivation lives
once (DRY).

The module also owns the *inverse*: `is_cluster_anchored` asks whether a note's id is the one
`stable_id` would mint from the members it cites. Two callers need that question answered and
neither may guess at the answer — `memory.supersede` uses it to decide whether a note is one this
synthesis owns, and `memory.playbook` uses it to state a note's provenance — so the rule lives
beside the derivation it inverts rather than being re-spelled at each site.
"""

from chemclaw.core.ids import stable_hash

# How a memory note cites a cluster member, and therefore how the member id is read back out of it.
MEMBER_PREFIX = "reaction-"


def stable_id(prefix: str, member_ids: list[str]) -> str:
    """Return `<prefix>-<12 hex chars>` keyed on the cluster's smallest member id.

    The anchor is the *smallest* member id, not the full member set: hashing the exact
    set would mint a brand-new id whenever a cluster gains a member (routine under
    periodic ELN sync), leaving the already-merged subset note in the graph as stale
    "current" knowledge with no supersede link. Anchoring on the smallest member keeps
    the id — and therefore the PR-gate branch and the merged file path — stable as the
    cluster grows, so the grown note supersedes the old one in place. Clusters within
    one synthesis run are disjoint (connected components / similarity partitions), so
    anchors never collide. Uses the shared `chemclaw.core.ids.stable_hash`, so memory ids
    share the system-wide hashing scheme (stable across runs and processes).
    """
    return f"{prefix}-{stable_hash(min(member_ids), chars=12)}"


def is_cluster_anchored(note_id: str, cited_note_ids: list[str]) -> bool:
    """True when `note_id` is exactly the `stable_id` the reactions it cites anchor.

    The inverse of `stable_id`, and the only *checkable* statement about where a memory note came
    from. A synthesis note cites precisely the cluster it was minted from and anchors its id on
    that cluster's smallest member, so reconstructing the id from the citations round-trips for
    exactly the notes the three builders wrote — and for nothing else. The observations tier's
    promoted playbook anchors on an observation's *scope* instead (D-161), a human-authored
    playbook on a slug, and neither reconstructs.

    The prefix is read back off `note_id` rather than mapped from a note type, so no table anywhere
    can drift from the builders' own `stable_id(...)` calls. Citations that are not reactions (an
    `interaction` note in a promoted observation's evidence) are ignored: they were never part of
    an anchor.

    Args:
        note_id: The note's full id, e.g. `playbook-4e21c0aa91bd`.
        cited_note_ids: The note ids it cites — `Note.outgoing_links()`, or the evidence list a
            builder is about to cite.

    Returns:
        Whether the id is this synthesis's own derivation over those citations. False when nothing
        cited is a reaction, because then there is no anchor to reconstruct.
    """
    members = [
        cited.removeprefix(MEMBER_PREFIX)
        for cited in cited_note_ids
        if cited.startswith(MEMBER_PREFIX)
    ]
    if not members:
        return False
    return stable_id(note_id.rpartition("-")[0], members) == note_id
