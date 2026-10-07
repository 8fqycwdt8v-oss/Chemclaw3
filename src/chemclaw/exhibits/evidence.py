"""Which stored tool results are evidence: what a tool computed or read, never what the model wrote.

The grounding check, bindings and the handle stamp (`agent/tool_framing.stamp_result_handles`) must
agree on this, since every tool result is stored and linked, including the agent's own words handed
back.

A result is evidence when a capability produced it (`agent/chemclaw_agent.capability_tool_names`).
Listed here as data, since this package sits below `agent`:

- the agent's **scaffolding** — subagent spawner, todo writer, scratchpad filesystem verbs, peer
  handoffs;
- capability tools that **hand back a document the agent wrote**: the artefact tools and the
  experiment-protocol draft and read.

`tests/test_exhibit_evidence.py` holds the scaffolding list against the live surface in both
directions.
"""

from __future__ import annotations

from chemclaw.exhibits.models import EXHIBIT_TOOLS

# The agent's scaffolding, by name; `agent/chemclaw_agent` derives the same set and a test compares
# them.
SCAFFOLDING_TOOLS: frozenset[str] = frozenset(
    {"task", "write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep"}
)

#: The capability tools whose result is a document the agent wrote: an artefact, or a protocol
#: design the agent drafted, read back.
AGENT_DOCUMENT_TOOLS: frozenset[str] = EXHIBIT_TOOLS | frozenset(
    {"draft_experiment_protocol", "read_experiment_protocol"}
)

#: Every tool whose stored result is never evidence, but for the handoffs, which are a shape.
MODEL_AUTHORED_TOOLS: frozenset[str] = SCAFFOLDING_TOOLS | AGENT_DOCUMENT_TOOLS

#: The prefix every peer handoff tool carries (`agent/handoff.HANDOFF_PREFIX`, compared by the
#: test): a shape rather than a set, because the peer roster is a setting this package cannot see.
HANDOFF_TOOL_PREFIX = "transfer_to_"


def is_evidence(tool: str) -> bool:
    """Whether a result `tool` produced may ground a figure or be bound to."""
    return tool not in MODEL_AUTHORED_TOOLS and not tool.startswith(HANDOFF_TOOL_PREFIX)


def evidence_predicate(column: str) -> str:
    """The SQL form of `is_evidence` over `column`, taking `evidence_params()` in that order.

    A link whose tool collapsed to `''` (two tools returned identical text) counts as evidence: a
    model-written echo identical to a capability's output carries no figure the capability did not.
    """
    return f"NOT ({column} = ANY(%s) OR starts_with({column}, %s))"


def evidence_params() -> tuple[list[str], str]:
    """The parameters `evidence_predicate` takes, in its order."""
    return sorted(MODEL_AUTHORED_TOOLS), HANDOFF_TOOL_PREFIX
