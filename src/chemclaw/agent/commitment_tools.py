"""The agent tool over the commitment mirror: what a programme committed to, and what it waits on.

**One tool, and it reads.** Nothing here moves a milestone, changes a date or assigns anybody: the
mirror is read-only by the same rule every data source is (`ingest/sources/README.md` — a source
"cannot acquire a write path by declaring one"), and writing back to a portfolio system is the
effector seam's business rather than a tool's.

The value it adds over the portfolio tool the organisation already runs is the join: this is the
only place a slipping milestone sits beside the chemistry that is slipping it.
"""

from chemclaw.agent.framing import defang
from chemclaw.core.tool_registry import tool
from chemclaw.ingest.commitments.store import mirror_freshness, outstanding


@tool
async def review_commitments(
    owner: str = "", source: str = "", limit: int = 25
) -> dict[str, object]:
    """Read what a programme has committed to and has not yet delivered, soonest deadline first.

    Each entry says what it is, who owns it, what state it is in, when it is due, and — the part
    only this system has — which notes, durable jobs and compounds the source said it depends on.
    That link is what makes "which programmes are at risk, and what chemistry is holding them up"
    answerable here rather than in the portfolio tool.

    Three things to carry into any answer built on this:

    - **It is a mirror, not the plan.** The organisation's own portfolio system is the truth. This
      knows nothing this system was not told, and it cannot reschedule, re-level or re-forecast
      anything. Never present a date here as a commitment being made now.
    - **Report `mirrored_at`.** A mirror's characteristic failure is staleness, not error: the
      export stops running and the numbers keep answering. If it is old, say so before the list.
    - **An empty list has two meanings** and the answer distinguishes them: nothing outstanding, or
      nothing ever mirrored. `mirrored_at` is null in the second case.

    Args:
        owner: Narrow to one owner, **in the source's own namespace** — a portfolio tool's user
            name, not an Entra id. Empty returns every owner.
        source: Narrow to one mirrored system. Empty returns all of them.
        limit: How many to return, soonest deadline first; ones with no date come last.

    Returns:
        The outstanding commitments, when the mirror was last refreshed, and how many of the
        returned rows say what chemistry they wait on.
    """
    commitments, freshness = await outstanding(owner=owner, source=source, limit=limit)
    refreshed = freshness or await mirror_freshness(source)
    return {
        "mirrored_at": refreshed.isoformat() if refreshed else None,
        "linked_to_science": sum(1 for row in commitments if row.links_to_science),
        "commitments": [
            {
                **row.model_dump(mode="json", exclude={"title", "owner"}),
                # `title` and `owner` are free text from a system this one does not control, and
                # they reach the model exactly as a retrieved chunk does. The identifiers beside
                # them are keys and bounded vocabularies, so only these two need it.
                "title": defang(row.title),
                "owner": defang(row.owner),
            }
            for row in commitments
        ],
    }
