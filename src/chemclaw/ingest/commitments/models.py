"""What a commitment is, and the one thing this system adds to it.

A commitment is a unit of work a programme has committed to — a programme, an activity, a
milestone, a deliverable — mirrored in from the system that owns it. **It is a mirror and not a
system of record**: the organisation already runs a portfolio tool, that tool is the truth, and
nothing here plans, schedules, levels resources or computes a critical path. A deployment that let
it try would have two answers to "when does this land", and the second one would be wrong more
often.

What this adds is the join no portfolio tool can compute: between a slipping milestone and the
*chemistry* that is slipping it. `note_ids`, `job_ids` and `compounds` are how the source states
that link, and they are the reason the mirror is worth keeping at all.
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
    #: Who owns it, **in the source's namespace**. Deliberately not resolved to an Entra oid: a
    #: mapping this system invented would be a second directory, and a wrong one would attribute
    #: somebody else's work.
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

        The property the whole mirror exists for. A commitment with no link is a row a portfolio
        tool already holds and holds better; one with a link is a question only this system can
        answer.
        """
        return bool(self.note_ids or self.job_ids or self.compounds)
