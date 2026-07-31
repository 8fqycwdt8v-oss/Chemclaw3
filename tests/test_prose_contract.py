"""The agent's prose must only name capability the agent has (gap IDEA-7).

This check exists because two shipped defects were the same shape and no gate saw either:
`skills/experiment-design/SKILL.md` pointed the agent at `BoCampaignWorkflow` (no tool exposed it),
and `skills/deep-research/SKILL.md` named `find_similar_reactions(...)` when the agent's actual MCP
tool is `similar_reactions` — so loading that skill taught the agent three tool names that would
fail at call time. `mypy` cannot see prose, `pytest` did not read it, and `make skill-validate` only
checks frontmatter.
"""

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.cli.validate_prose_contract import (
    _ALLOWED_NON_TOOLS,
    _prose_sources,
    check_prose_contract,
    referenced_note_types,
)
from chemclaw.kg.note import KNOWN_NOTE_TYPES


def test_shipped_prose_names_only_real_tools() -> None:
    """The committed skills + instructions pass — the regression guard itself."""
    assert check_prose_contract() == []


def test_mcp_tools_count_as_real() -> None:
    """MCP capability tools have no Python symbol, so they must come from the config allowlist."""
    names = available_tool_names()
    assert {"similar_molecules", "substructure_matches", "similar_reactions"} <= names


def test_in_process_tools_count_as_real() -> None:
    """The function tools registered on the agent are recognised too."""
    assert {"gather_evidence", "expand_note", "predict_pka"} <= available_tool_names()


def test_a_made_up_tool_is_caught(tmp_path: object, monkeypatch: object) -> None:
    """A skill naming a nonexistent tool fails the check — the `find_similar_reactions` case."""
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "Call `retrosynthesize(target)` to plan a route."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "retrosynthesize" in problems[0]


def test_pointing_the_agent_at_a_workflow_is_caught(monkeypatch: object) -> None:
    """The `BoCampaignWorkflow` case: the agent cannot invoke a workflow class, only a tool."""
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "For many rounds reach for the durable `BoCampaignWorkflow`."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "BoCampaignWorkflow" in problems[0]
    assert "cannot invoke" in problems[0]


def test_the_allowlist_is_small_and_deliberate() -> None:
    """The escape hatch must stay a review decision, not a dumping ground."""
    assert len(_ALLOWED_NON_TOOLS) <= 3


def test_a_note_type_the_graph_does_not_know_is_caught(monkeypatch: object) -> None:
    """The `experiment-batch` case: prose told the agent to write a type `kg-validate` rejects.

    The defect this guards was invisible from both ends — the skill named the type, the schema did
    not hold it, and the PR where `kg-validate` would have objected is never opened, so the
    proposal died on a branch with no error anywhere.
    """
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "Draft it through `propose_knowledge_note` (type `wishlist`)."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "wishlist" in problems[0]
    assert "KNOWN_NOTE_TYPES" in problems[0]


def test_a_note_type_enumeration_is_read(monkeypatch: object) -> None:
    """The taught vocabulary list is checked too — it is what an agent copies from."""
    import chemclaw.cli.validate_prose_contract as module

    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "_prose_sources",
        lambda: {"fake/SKILL.md": "- **type**: the smallest accurate kind (`reaction`, `myth`)."},
    )
    problems = check_prose_contract()
    assert len(problems) == 1
    assert "myth" in problems[0]


def test_the_two_proposal_types_the_skills_teach_are_real() -> None:
    """`protocol`/`experiment-batch` must stay known — the regression this rule was written for."""
    assert {"protocol", "experiment-batch"} <= KNOWN_NOTE_TYPES


def test_every_type_the_system_mints_is_taught_somewhere() -> None:
    """The graph's vocabulary and the prose that teaches it must not drift apart again.

    The enumeration in `knowledge-graph-write` had drifted in *both* directions: it named two types
    that did not exist (caught by `check_prose_contract`) and omitted three that did, which is the
    direction no checker can see from the prose alone — a missing entry is silence, not an error.
    """
    taught: set[str] = set()
    for text in _prose_sources().values():
        taught |= referenced_note_types(text)
    assert KNOWN_NOTE_TYPES <= taught, (
        f"note types no skill teaches: {sorted(KNOWN_NOTE_TYPES - taught)}"
    )
