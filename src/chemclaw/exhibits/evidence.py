"""Which stored tool results are evidence: what a tool computed or read, never what the model wrote.

Three readers ask the same question of a session's stored results and must get one answer: the
grounding check (is a figure accounted for), bindings (may a value be taken from this result), and
the handle stamp (`agent/tool_framing.stamp_result_handles` — is this result something a binding
may name). Every tool result is stored and linked, so each of them used to treat the agent's own
words handed back as if a tool had computed them. Measured twice: `read_exhibit` returning the
artefact cleared `['9.95', '16']` to `[]` on the next revision, and after that was closed a helper
(`task`) that read the artefact and reported back cleared them again.

**The rule.** A result is evidence when a *capability* produced it — a calculation, a lookup, a
search, a connector — which is `agent/chemclaw_agent.capability_tool_names`'s own division of the
surface. What is not evidence is named here, because this package sits below `agent` and the three
queries need the set as data:

- the agent's **scaffolding** — the subagent spawner, the harness's todo writer, the filesystem
  verbs over its own scratchpad, and the peer handoffs — whose results are a helper's report, the
  model's own todo list, a file the model wrote, a handoff reason;
- the capability tools that **hand back a document the agent itself wrote**: the artefact tools and
  the experiment-protocol draft and its read.

`tests/test_exhibit_evidence.py` holds the first half against the live surface in both directions —
every name the agent can call that is not a capability is listed, and every listed name exists — so
a new scaffolding tool fails a test rather than quietly grounding figures.
"""

from __future__ import annotations

from chemclaw.exhibits.models import EXHIBIT_TOOLS

#: The agent's scaffolding, by name: `task`, `write_todos` and the filesystem verbs. Spelled here
#: and derived in `agent/chemclaw_agent` (`available_tool_names() - capability_tool_names()`), which
#: the test compares.
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

    A link whose tool collapsed to `''` (two tools returned identical text, `api/tool_results.py`)
    counts as evidence, which is the conservative reading only when one of the two was a capability
    — and a model-written echo identical byte-for-byte to a capability's output carries no figure
    the capability did not.
    """
    return f"NOT ({column} = ANY(%s) OR starts_with({column}, %s))"


def evidence_params() -> tuple[list[str], str]:
    """The parameters `evidence_predicate` takes, in its order."""
    return sorted(MODEL_AUTHORED_TOOLS), HANDOFF_TOOL_PREFIX
