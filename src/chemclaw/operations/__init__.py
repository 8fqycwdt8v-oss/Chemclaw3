"""The operational read model: what this system did, read back out of its own record.

Every other package here answers a question about the chemistry. This one answers a question about
the *work* — which tools were used, which jobs ran, what the agent proposed and what humans decided
about it, and where the effort went. It reads the six tables no one could *aggregate* —
`audit_events`, `job_records`, `note_proposals`, `plan_approvals`, `turn_costs` and
`effects` — and it writes nothing. (Named rather than counted: this said "five" while the
package read six, and a name is a thing `grep -ohE 'FROM [a-z_]+' src/chemclaw/operations/*.py`
can check.)

**The original framing here was "five tables that had writers and no readers", and that is false
for four of them** — `cli/explain.py` reads `audit_events` and `job_records`, `publish/backfill.py`
and `durable/job_record_store.py` read `job_records`, `kg/record.py` reads `note_proposals`
and `agent/plan_approval_store.py` reads `plan_approvals`. Only `turn_costs` had no reader at all,
which is exactly what its own docstring said.

The true claim is the one in the sentence above, and the narrower version first written here was
**also wrong**: it said "every existing reader is a point lookup", which holds only for
`cli/explain.py` and `plan_approval_store`. `job_record_store._SEARCH` is a cross-record search over
every connector's runs (it is what `find_past_jobs` calls), `proposal_store._SELECT_MANY` is a
paginated listing, and `publish/backfill._JOBS` sweeps the whole table.

What none of them does is *aggregate*. "Who else has used this playbook", "how many hazard flags did
the group raise last quarter" and "how much of that note was agent-written" were unanswerable from
rows the system had already stamped, because no reader grouped or counted anything — and that is
what changed. A correction that is itself wrong is worse than the claim it replaced, which is why
this paragraph now says what was actually measured rather than what sounded narrower.

See `chemclaw.operations.activity` for the three rules every reading keeps (counts and identifiers
only, the window travels with the answer, and nothing the tables cannot see is inferred).

`evidence_pack` is the other half: where `activity` aggregates *across* the record, that assembles
one conversation's whole record into the context-of-use artefact a regulated deployment is asked
for. It is the one reading here that returns free text, because a rationale and a plan hash are
exactly what such a pack is for — and it is scoped to a single session for that reason.
"""

from chemclaw.operations.activity import (
    ActorSpend,
    Authorship,
    Coverage,
    JobActivity,
    JobRun,
    ProposalOutcome,
    Spend,
    ToolUsage,
    ToolUse,
    authorship,
    job_activity,
    spend,
    tool_usage,
)
from chemclaw.operations.evidence_pack import EvidencePack, assemble
from chemclaw.operations.window import MAX_WINDOW_DAYS, Window

__all__ = [
    "MAX_WINDOW_DAYS",
    "ActorSpend",
    "Authorship",
    "Coverage",
    "EvidencePack",
    "JobActivity",
    "JobRun",
    "ProposalOutcome",
    "Spend",
    "ToolUsage",
    "ToolUse",
    "Window",
    "assemble",
    "authorship",
    "job_activity",
    "spend",
    "tool_usage",
]
