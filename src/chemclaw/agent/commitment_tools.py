"""The agent tool over the commitment mirror: what a programme committed to, and what it waits on.

**One tool, and it reads.** Nothing here moves a milestone, changes a date or assigns anybody: the
mirror is read-only by the same rule every data source is (`ingest/sources/README.md` — a source
"cannot acquire a write path by declaring one"), and writing back to a portfolio system is the
effector seam's business rather than a tool's.

The value it adds over the portfolio tool the organisation already runs is the join: this is the
only place a slipping milestone sits beside the chemistry that is slipping it.
"""

from datetime import datetime

from pydantic import BaseModel, Field, computed_field

from chemclaw.agent.framing import defang
from chemclaw.core.tool_registry import tool
from chemclaw.ingest.commitments.store import mirror_freshness, outstanding


class CommitmentReview(BaseModel):
    """The outstanding book as this system mirrors it, **and what the list does not say**.

    The bare `dict` this replaced carried three facts and needed four. Freshness was there;
    `linked_to_science` was there; the rows were there. What was missing is that the rows are a
    *page*: measured, 40 outstanding commitments answered a `limit=25` call with 25 rows and no
    field, log line or counter naming the fifteen — so "which programmes are at risk" was answered
    over the 25 soonest deadlines and presented as the whole book.

    The other silence was worse-placed rather than absent. The tool's docstring reasoned that an
    empty list has two meanings and that `mirrored_at` distinguishes them — a distinction that
    lived *only* in the docstring, which the model reads once when the tool is defined, and not in
    the payload, which is what sits in the context window when the answer is written. That is the
    pattern `FingerprintSearch.verdict` exists to end, and `commitment_export_dir`'s own config
    comment records this exact failure reaching a project leader as a truthful empty portfolio.
    """

    commitments: list[dict[str, object]] = Field(default_factory=list)
    mirrored_at: datetime | None = None
    # How many of the returned rows say what chemistry they wait on — the join that is this
    # mirror's whole reason to exist beside the portfolio tool.
    linked_to_science: int = Field(default=0, ge=0)
    # Everything live under the same filters, before the page bound.
    total_outstanding: int = Field(default=0, ge=0)
    # The bound the store actually applied, which is not always the one asked for.
    limit_applied: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before saying what a programme owes.

        `computed_field` rather than a bare property, for the reason `FingerprintSearch.verdict`
        states in full: a plain property is not serialized, so `model_dump()` would carry the rows
        and drop every qualification on them.

        The mirror caveat is unconditional because it is true of every arm: this is a copy of
        somebody else's plan, and no answer built on it may present a date as a commitment being
        made now.
        """
        mirror = (
            "This is a MIRROR of the organisation's portfolio system, never the plan itself: it "
            "knows nothing this system was not told and can reschedule nothing."
        )
        if self.mirrored_at is None:
            return (
                "NEVER MIRRORED: no export has ever been read for this filter, so the empty list "
                "means the mirror has not run — NOT that nothing is outstanding. Say that the "
                f"portfolio is unknown and that an operator must run the sync. {mirror}"
            )
        stamp = self.mirrored_at.isoformat()
        if self.total_outstanding > len(self.commitments):
            return (
                f"PARTIAL: {len(self.commitments)} of {self.total_outstanding} outstanding "
                f"commitments are shown, soonest deadline first (page bound "
                f"{self.limit_applied}); mirrored at {stamp}. The rest have later deadlines and "
                f"are NOT delivered — do not describe this as the whole book. {mirror}"
            )
        if not self.commitments:
            return (
                f"NOTHING OUTSTANDING: the mirror last refreshed at {stamp} and holds no live "
                f"commitment under this filter. {mirror}"
            )
        return (
            f"COMPLETE: every outstanding commitment under this filter is shown; mirrored at "
            f"{stamp}. Report that date — a mirror's characteristic failure is staleness, not "
            f"error. {mirror}"
        )


@tool
async def review_commitments(
    owner: str = "", source: str = "", limit: int = 25
) -> CommitmentReview:
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
    - **An empty list has two meanings** — nothing outstanding, or nothing ever mirrored — and a
      **short** list has two more: `commitments` is a page and `total_outstanding` is the book.
      `verdict` says which; never describe a page as a portfolio.

    Args:
        owner: Narrow to one owner, **in the source's own namespace** — a portfolio tool's user
            name, not an Entra id. Empty returns every owner.
        source: Narrow to one mirrored system. Empty returns all of them.
        limit: How many to return, soonest deadline first; undated last (bounded; `limit_applied`).

    Returns:
        A page of outstanding commitments, `mirrored_at`, how many rows name the chemistry they
        wait on, `total_outstanding`, and a `verdict`.
    """
    page = await outstanding(owner=owner, source=source, limit=limit)
    refreshed = page.mirrored_at or await mirror_freshness(source)
    return CommitmentReview(
        mirrored_at=refreshed,
        linked_to_science=sum(1 for row in page.commitments if row.links_to_science),
        total_outstanding=page.total_outstanding,
        limit_applied=page.limit_applied,
        commitments=[
            {
                **row.model_dump(mode="json", exclude={"title", "owner"}),
                # `title` and `owner` are free text from a system this one does not control, and
                # they reach the model exactly as a retrieved chunk does. The identifiers beside
                # them are keys and bounded vocabularies, so only these two need it.
                "title": defang(row.title),
                "owner": defang(row.owner),
            }
            for row in page.commitments
        ],
    )
