"""What a commitment is, and the one thing this system adds to it.

A commitment (programme, activity, milestone, deliverable) is mirrored in from the portfolio tool
that owns it. It is a mirror, not a system of record: nothing here plans, schedules or computes a
critical path. What it adds is the link between a milestone and the chemistry holding it up —
`note_ids`, `job_ids` and `compounds`, as the source states them.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: What kind of thing a commitment is, in the vocabulary a programme uses. Short deliberately: a
#: deeper hierarchy is the portfolio tool's business rather than this mirror's.
CommitmentKind = Literal["programme", "activity", "milestone", "deliverable"]

#: Where it stands. `blocked` is separate from `open` because it is the one a manager asks for.
CommitmentState = Literal["open", "in-progress", "blocked", "done", "cancelled"]

#: The states a commitment is still live in — what "outstanding" means in every reading here.
LIVE_STATES: tuple[CommitmentState, ...] = ("open", "in-progress", "blocked")


class Commitment(BaseModel):
    """One mirrored unit of committed work, as the source stated it."""

    #: The data source this came from. Part of the key: two systems may both call something
    #: `PRJ-14`, and a bare id would silently merge them.
    source: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    kind: CommitmentKind = "activity"
    title: str = Field(min_length=1)
    #: Who owns it, in the source's namespace. Not resolved to an Entra oid: an invented mapping
    #: would be a second directory and could misattribute work.
    owner: str = ""
    state: CommitmentState = "open"
    due_at: datetime | None = None
    #: The parent's `external_id` within the same source, or empty at the top.
    parent_id: str = ""
    note_ids: list[str] = Field(default_factory=list)
    job_ids: list[str] = Field(default_factory=list)
    compounds: list[str] = Field(default_factory=list)

    @property
    def is_live(self) -> bool:
        """Whether this is still outstanding — the one predicate every reading shares."""
        return self.state in LIVE_STATES

    @property
    def links_to_science(self) -> bool:
        """Whether the source said what chemistry this is waiting on.

        A commitment with no link is one the portfolio tool already holds better.
        """
        return bool(self.note_ids or self.job_ids or self.compounds)
