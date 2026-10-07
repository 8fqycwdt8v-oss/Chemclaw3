"""Deterministic note ids for synthesized memory notes.

A campaign or playbook id must stay stable while its evidence grows, so re-synthesis updates the
note in place instead of minting a new one beside it. Also owns the inverse, `is_cluster_anchored`,
used by `memory.supersede` and `memory.playbook`.
"""

from chemclaw.core.ids import stable_hash

# How a memory note cites a cluster member, and therefore how the member id is read back out of it.
MEMBER_PREFIX = "reaction-"


def stable_id(prefix: str, member_ids: list[str]) -> str:
    """Return `<prefix>-<12 hex chars>` keyed on the cluster's smallest member id.

    Anchored on the smallest member rather than the whole set, so the id (and file) stays stable as
    a cluster grows and the grown note supersedes the old one in place. Clusters within one run are
    disjoint, so anchors do not collide. Uses `chemclaw.core.ids.stable_hash`.
    """
    return f"{prefix}-{stable_hash(min(member_ids), chars=12)}"


def is_cluster_anchored(note_id: str, cited_note_ids: list[str]) -> bool:
    """True when `note_id` is exactly the `stable_id` the reactions it cites anchor.

    The checkable statement of where a memory note came from: it round-trips only for notes the
    synthesis builders wrote (not promoted observations or human playbooks). The prefix is read from
    `note_id`; non-reaction citations are ignored.

    Args:
        note_id: The note's full id, e.g. `playbook-4e21c0aa91bd`.
        cited_note_ids: The note ids it cites (`Note.outgoing_links()`, or the evidence a builder is
            about to cite).

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
