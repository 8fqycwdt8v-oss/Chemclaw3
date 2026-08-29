"""The operational read model: what this system did, read back out of its own record.

Every other package here answers a question about the chemistry. This one answers a question about
the *work* — which tools were used, which jobs ran, what the agent proposed and what humans decided
about it, and where the effort went. It reads five tables that had writers and no readers, and it
writes nothing.

See `chemclaw.operations.activity` for the three rules every reading keeps (counts and identifiers
only, the window travels with the answer, and nothing the tables cannot see is inferred).
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
from chemclaw.operations.window import MAX_WINDOW_DAYS, Window

__all__ = [
    "MAX_WINDOW_DAYS",
    "ActorSpend",
    "Authorship",
    "Coverage",
    "JobActivity",
    "JobRun",
    "ProposalOutcome",
    "Spend",
    "ToolUsage",
    "ToolUse",
    "Window",
    "authorship",
    "job_activity",
    "spend",
    "tool_usage",
]
