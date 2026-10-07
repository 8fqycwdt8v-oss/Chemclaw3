"""The operational read model: what this system did, read back out of its own record.

Where every other package answers a question about the chemistry, this one answers questions
about the *work*: which tools were used, which jobs ran, what the agent wrote into the graph,
where the effort went. It aggregates `audit_events`, `job_records`, `plan_approvals`,
`turn_costs` and `effects`, and writes nothing.

`activity` aggregates across the record under three rules (counts and identifiers only, the
window travels with the answer, nothing the tables cannot see is inferred). `evidence_pack`
assembles one conversation's whole record into a context-of-use artefact; it is the one reading
that returns free text, and is scoped to a single session for that reason.
"""

from chemclaw.operations.activity import (
    ActorSpend,
    Authorship,
    Coverage,
    JobActivity,
    JobRun,
    KnowledgeWrites,
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
    "KnowledgeWrites",
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
