"""The operational read model: what this system did, read back out of its own record.

Every other package here answers a question about the chemistry. This one answers a question about
the *work* — which tools were used, which jobs ran, what the agent proposed and what humans decided
about it, and where the effort went. It reads five tables that no one could *aggregate*, and it
writes nothing.

**The original framing here was "five tables that had writers and no readers", and that is false
for four of them** — `cli/explain.py` reads `audit_events` and `job_records`, `publish/backfill.py`
and `durable/job_record_store.py` read `job_records`, `kg/proposal_store.py` reads `note_proposals`
and `agent/plan_approval_store.py` reads `plan_approvals`. Only `turn_costs` had no reader at all,
which is exactly what its own docstring said.

The true claim is narrower and is the one the package is for: every existing reader is a **point
lookup** — one session's reconstruction, one proposal, one approval — and none of them can answer a
question *across* the record. "Who else has used this playbook", "how many hazard flags did the
group raise last quarter" and "how much of that note was agent-written" were unanswerable from rows
the system had already stamped, and that is what changed.

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
