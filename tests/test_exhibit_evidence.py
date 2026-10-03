"""The one rule for which stored tool results are evidence, held against the agent's live surface.

`exhibits/evidence.py` names what is not evidence because it sits below `agent`; this is what keeps
the name list equal to the division `agent/chemclaw_agent` draws between capabilities and the
agent's own scaffolding, in both directions, so a new scaffolding tool reds here rather than quietly
grounding figures the model wrote.
"""

from chemclaw.agent.chemclaw_agent import available_tool_names, capability_tool_names
from chemclaw.agent.handoff import HANDOFF_PREFIX, handoff_tool_name
from chemclaw.core.tool_registry import registered_tool_names
from chemclaw.exhibits.evidence import (
    AGENT_DOCUMENT_TOOLS,
    HANDOFF_TOOL_PREFIX,
    SCAFFOLDING_TOOLS,
    is_evidence,
)


def test_the_scaffolding_named_is_exactly_what_the_agent_holds_beside_its_capabilities() -> None:
    """Every non-capability name is listed (or is a handoff), and every listed one is real."""
    scaffolding = {
        name
        for name in available_tool_names() - capability_tool_names()
        if not name.startswith(HANDOFF_PREFIX)
    }
    assert scaffolding == set(SCAFFOLDING_TOOLS), (
        f"unlisted: {sorted(scaffolding - SCAFFOLDING_TOOLS)}; "
        f"gone: {sorted(SCAFFOLDING_TOOLS - scaffolding)}"
    )


def test_the_agent_documents_named_are_registered_tools_and_handoffs_are_a_shape() -> None:
    """A renamed artefact or protocol tool would leave its readout counting as evidence."""
    assert AGENT_DOCUMENT_TOOLS <= set(registered_tool_names())
    assert HANDOFF_TOOL_PREFIX == HANDOFF_PREFIX
    assert not is_evidence(handoff_tool_name("property-lookup"))
    assert not any(is_evidence(name) for name in {"task", "read_file", "grep", "write_todos"})
    assert is_evidence("predict_pka") and is_evidence("gather_evidence")
