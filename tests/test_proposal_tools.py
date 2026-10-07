"""`propose_skill`: what it refuses, what it tells the model, and what it still cannot do.

It writes a proposal and never a skill, so the first test is the absence one: no turn gained a
way to write judgment into its own prompt.
"""

import asyncio
import pathlib

import pytest

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.behaviour_proposals import (
    InMemoryProposalStore,
    content_hash,
    default_proposal_store,
)
from chemclaw.agent.langgraph_agent import shipped_skill_names
from chemclaw.agent.proposal_tools import propose_skill
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.identity_context import reset_current_identity, set_current_identity

_BODY = "---\nname: cold-quench\ndescription: how to quench this class cold\n---\n\nQuench cold.\n"


@pytest.fixture(autouse=True)
def _queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh in-process queue per test."""
    from chemclaw.agent import behaviour_proposals

    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(behaviour_proposals, "_IN_MEMORY", InMemoryProposalStore())


#: One name this deployment's reviewed trees already occupy, read off `shipped_skill_names()` at
#: module scope (parametrize is evaluated at collection) so a rename in `skills/` carries the row.
_A_SHIPPED_NAME = sorted(shipped_skill_names())[0]


def _call(**kwargs: str) -> str:
    """Call the tool as a turn for one chemist would."""
    tokens = set_current_identity("a-chemist", frozenset())
    try:
        return str(asyncio.run(propose_skill(**kwargs)))
    finally:
        reset_current_identity(tokens)


def test_proposing_is_state_changing_so_no_helper_holds_it() -> None:
    """`propose_skill` is state-changing, so no helper holds it.

    A helper's surface subtracts `side_effecting_tools()`, so this membership is the whole
    narrowing. A helper proposing behaviour changes from a context the chemist cannot see is ruled
    out, as for `ask_clarifying_question`.
    """
    assert "propose_skill" in side_effecting_tools()


#: Modules that define a registered tool and may nonetheless name one of `_WRITES_BEHAVIOUR`,
#: each with the argument for it. An entry here is a reviewed exemption, not a waiver: the point is
#: that adding one is a diff a reader sees.
_ARGUED = {
    # The proposer imports the queue for `propose` and `content_hash`; it must not reach `decide`,
    # which the symbol check holds. `local_skills` is imported for `validated_skill`, a read of the
    # tier's admission rules; its writes (`save_local_skill`, `delete_local_skill`) are in
    # `_WRITES_BEHAVIOUR`.
    "chemclaw.agent.proposal_tools": {"behaviour_proposals", "local_skills"},
    # The generated `run_*` job launchers resolve a manifest's `module:attribute` reference, so
    # `importlib` is their purpose; `check_driver_module` guards it and they name nothing in
    # `_WRITES_BEHAVIOUR`. Present only once `_register_generated_tools()` has run.
    "chemclaw.connectors.jobs": {"importlib"},
}

#: What a turn must not be able to reach. Two writes into the personal skills tier, and the one
#: call that turns a proposal into behaviour.
_WRITES_BEHAVIOUR = frozenset({"save_local_skill", "delete_local_skill", "decide"})


def _tool_defining_modules() -> dict[str, pathlib.Path]:
    """Every module that registers a capability tool, by name, with its source file."""
    import inspect

    from chemclaw.agent import tool_modules  # noqa: F401 - populates the registry
    from chemclaw.agent.chemclaw_agent import _capability_tools
    from chemclaw.core.tool_registry import registered_tools

    # `_capability_tools` runs `_register_generated_tools()`, so this covers the shipped surface
    # including generated launchers, in isolation as in a full run.
    _capability_tools()
    found: dict[str, pathlib.Path] = {}
    for fn in registered_tools():
        module = inspect.getmodule(fn)
        source = getattr(module, "__file__", None)
        if module is not None and source and "chemclaw" in module.__name__:
            found[module.__name__] = pathlib.Path(source)
    return found


def test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one() -> None:
    """No turn can write a skill, even now that it can propose one.

    The subject is the tool registry: every module defining a tool, checked against the calls that
    turn a proposal into behaviour, not one module's source text. `importlib` is refused because a
    module name built at run time is the one thing a static reader cannot follow.
    """
    import ast

    for name, path in sorted(_tool_defining_modules().items()):
        tree = ast.parse(path.read_text())
        named = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        imported = {
            alias.name.rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        } | {
            node.module.rsplit(".", 1)[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }

        reaches = (named | imported) & _WRITES_BEHAVIOUR
        assert not reaches, (
            f"{name} defines a tool and names {sorted(reaches)}, which writes behaviour; a turn "
            "proposes and a person decides"
        )
        forbidden_imports = (imported - _ARGUED.get(name, set())) & {
            "local_skills",
            "behaviour_proposals",
            "importlib",
        }
        assert not forbidden_imports, (
            f"{name} defines a tool and imports {sorted(forbidden_imports)}; add an argued "
            "entry to "
            "_ARGUED if that is really intended"
        )


def test_the_symbols_that_absence_test_names_still_exist() -> None:
    """The symbols the absence test names still exist, so a rename fails the guard rather than
    satisfying it.
    """
    from chemclaw.agent import behaviour_proposals, local_skills

    assert callable(local_skills.save_local_skill)
    assert callable(local_skills.delete_local_skill)
    assert callable(behaviour_proposals.ProposalStore.decide)


def test_the_model_is_told_which_of_three_things_happened() -> None:
    """The model is told which of three things happened to its proposal.

    Each calls for a different next move; without it the only strategy after a decline is to propose
    again.
    """
    fresh = _call(name="cold-quench", body=_BODY, rationale="twice now")
    repeat = _call(name="cold-quench", body=_BODY, rationale="twice now")

    assert "proposed" in fresh and "waiting" in fresh
    assert "already waiting" in repeat, "a repeat reads as a fresh proposal"

    asyncio.run(
        default_proposal_store().decide(
            "a-chemist",
            "skill",
            "cold-quench",
            content_hash(_BODY),
            accepted=False,
            decided_by="a-chemist",
            reason="too narrow",
        )
    )
    decided = _call(name="cold-quench", body=_BODY, rationale="twice now")

    assert "rejected" in decided and "too narrow" in decided, (
        "the model is not told the verdict or the reason, so it cannot respond to the reason"
    )
    assert "cannot reopen" in decided


def test_a_re_proposed_superseded_body_is_reported_as_proposed_and_not_as_waiting() -> None:
    """A re-proposed superseded body is reported as proposed, not as already waiting.

    `propose` revives it; the test is that the tool says so truthfully rather than claiming a repeat
    of something listed nowhere the chemist looks.
    """
    _call(name="cold-quench", body=_BODY, rationale="twice now")
    _call(name="cold-quench", body=_BODY.replace("Quench cold.", "Quench warm."), rationale="or")

    revived = _call(name="cold-quench", body=_BODY, rationale="twice now")

    assert "proposed" in revived and "waiting" in revived, (
        "a revived proposal read as a repeat, so the model is told to stop mentioning a document "
        "the chemist is now being asked to decide"
    )
    assert "already waiting" not in revived, (
        "the model was told this exact text was already waiting, which is the falsehood the row is "
        "about: nothing was waiting, because a superseded row is listed nowhere"
    )


@pytest.mark.parametrize(
    ("kwargs", "because"),
    [
        ({"name": "x", "body": "no frontmatter", "rationale": "r"}, "not a skill at all"),
        (
            {"name": "x", "body": "---\nname: y\ndescription: d\n---\n\nb\n", "rationale": "r"},
            "two sources of one name can disagree",
        ),
        (
            {"name": "x", "body": "---\nname: x\n---\n\nb\n", "rationale": "r"},
            "a skill with no description is skipped by the loader",
        ),
        (
            {"name": "a/b", "body": "---\nname: a/b\ndescription: d\n---\n\nb\n", "rationale": "r"},
            "a '/' is the traversal shape",
        ),
        # A name the deployment already ships is refused at proposal time, as the accept route would
        # refuse it with 409. Otherwise the proposal waits unacceptably and can only be declined.
        # The name comes from `shipped_skill_names()`.
        (
            {
                "name": _A_SHIPPED_NAME,
                "body": f"---\nname: {_A_SHIPPED_NAME}\ndescription: d\n---\n\nb\n",
                "rationale": "r",
            },
            "the name is one this deployment already ships",
        ),
    ],
)
def test_a_body_that_could_not_be_written_is_refused_at_the_proposal(
    kwargs: dict[str, str], because: str
) -> None:
    """A body that could not be written is refused at the proposal, not at acceptance.

    `POST /skills/mine` refuses a malformed `SKILL.md`; failing after the person decided would look
    like the system losing their decision.
    """
    with pytest.raises(ChemclawError):
        _call(**kwargs)

    assert not asyncio.run(default_proposal_store().list_for("a-chemist")), (
        f"{because}: a refused body was stored anyway"
    )


def test_a_body_over_the_tiers_bound_is_refused_with_the_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same bound `POST /skills/mine` enforces, read from the same setting.

    A proposal the tier could not hold is one a person can accept and the system cannot honour.
    """
    monkeypatch.setattr(settings, "agent_local_skill_max_chars", 200)

    with pytest.raises(ChemclawError, match="200"):
        _call(name="cold-quench", body=_BODY + "x" * 300, rationale="r")
