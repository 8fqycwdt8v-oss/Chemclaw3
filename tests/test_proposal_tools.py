"""`propose_skill`: what it refuses, what it tells the model, and what it still cannot do.

The tool's standing is the point — it writes a *proposal* and never a skill — so the first test
here is the absence one: `SkillsReadOnlyRefusal` is unchanged, and adding a proposer did not open a
way for a turn to write judgment into its own prompt.
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


#: One name this deployment's reviewed trees already occupy, for the refusal table below.
#:
#: Read off `shipped_skill_names()` rather than written out, and at module scope because a
#: `parametrize` decorator is evaluated at collection: a literal would stop being a shipped name the
#: next time `skills/` is renamed, and the row would then pass for the wrong reason.
_A_SHIPPED_NAME = sorted(shipped_skill_names())[0]


def _call(**kwargs: str) -> str:
    """Call the tool as a turn for one chemist would."""
    tokens = set_current_identity("a-chemist", frozenset())
    try:
        return str(asyncio.run(propose_skill(**kwargs)))
    finally:
        reset_current_identity(tokens)


def test_proposing_is_state_changing_so_no_helper_holds_it() -> None:
    """Being in the partition is what takes it out of every helper, by arithmetic.

    A helper's surface is its caller's minus `side_effecting_tools()` on both halves
    (`D-2026-09-15-a-helper-shares-the-session-its-caller-already-opened`), so this one membership
    is the whole of the narrowing — no second list to keep in step. The reason is
    `ask_clarifying_question`'s exactly: a helper proposing behaviour changes from a context the
    chemist cannot see is worse than one that cannot propose.
    """
    assert "propose_skill" in side_effecting_tools()


#: Modules that define a registered tool and may nonetheless name one of `_WRITES_BEHAVIOUR`,
#: each with the argument for it. An entry here is a reviewed exemption, not a waiver: the point is
#: that adding one is a diff a reader sees.
_ARGUED = {
    # The proposer imports the queue for `propose` and `content_hash`. It must not reach `decide`,
    # which the symbol check below is what actually holds.
    #
    # **And `local_skills` for `validated_skill`, which is a read of the tier's admission rules and
    # not a write into it.** `_validated` used to hand-copy three of that function's four checks and
    # omit the fourth — the shipped-name conflict — so a proposal that the accept route answers 409
    # for was recorded as `open` and reported to the chemist as waiting for their decision, with no
    # way to accept it. The symbol half of this test is what keeps the exemption narrow: the two
    # writes into the tier (`save_local_skill`, `delete_local_skill`) are in `_WRITES_BEHAVIOUR`, so
    # naming either of them still reds this test with the import argued.
    "chemclaw.agent.proposal_tools": {"behaviour_proposals", "local_skills"},
    # The generated `run_*` job launchers, whose whole job is resolving a `module:attribute`
    # reference a bundle's `connector.yaml` declares — so `importlib` is what this module is for,
    # not a way around a static reader. It is guarded on its own terms by `check_driver_module`,
    # and the symbol half of this test still covers it: it names none of `_WRITES_BEHAVIOUR`.
    #
    # It appears here only once `_register_generated_tools()` has run, which is why this entry was
    # missing until a full-suite run — in isolation the registry holds no generated tool, so this
    # test passed on a smaller surface than the one it claims to cover. That is the same defect as
    # a ratchet whose fixture binds no connector, one file over.
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

    # `_capability_tools` is what runs `_register_generated_tools()`, and calling it here is the
    # difference between covering the shipped surface and covering whatever the import above
    # happens to reach. Without it the generated `run_*` launchers are absent, so this test passed
    # in isolation and failed in a full run — a control measuring a smaller system than the one it
    # claims, which is the defect this file exists to stop shipping.
    _capability_tools()
    found: dict[str, pathlib.Path] = {}
    for fn in registered_tools():
        module = inspect.getmodule(fn)
        source = getattr(module, "__file__", None)
        if module is not None and source and "chemclaw" in module.__name__:
            found[module.__name__] = pathlib.Path(source)
    return found


def test_no_turn_can_write_a_skill_even_now_that_it_can_propose_one() -> None:
    """The absence this whole feature rests on, over every module that defines a tool.

    `agent/skill_backend.SkillsReadOnlyRefusal` refuses every write verb on the shared tree and
    `agent/local_skills.ReadOnlyStoreBackend` on the chemist's own. Adding a proposer must not have
    opened a third path.

    **This used to read one module's source with `in`, and that is not the property.** Driven: a
    registered `settle_proposal` tool added to *that same module* — looking its caller's proposal
    up,
    writing it into their tier and calling `decide(accepted=True)` — left this file and
    `tests/test_api_proposals.py` at 16 passed. A substring check over one file is a claim about
    where somebody chose to put the code. So the subject is now the registry: every module that
    defines a tool, and every name in it, against the calls that turn a proposal into behaviour.

    **`importlib` is refused outright in this set**, because the one thing a static reader cannot
    follow is a module name built at run time, and no tool module has a reason to build one.
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
    """A rename must red the guard above rather than quietly satisfying it.

    An absence test passes trivially once the thing it forbids is called something else, which is
    the failure mode `tests/test_upstream_surface.py` names for its two absence arms. So the names
    are resolved against the modules that define them.
    """
    from chemclaw.agent import behaviour_proposals, local_skills

    assert callable(local_skills.save_local_skill)
    assert callable(local_skills.delete_local_skill)
    assert callable(behaviour_proposals.ProposalStore.decide)


def test_the_model_is_told_which_of_three_things_happened() -> None:
    """A proposer can learn what became of its proposal — a requirement, not a courtesy.

    Without it the only strategy after a decline is to propose again, which the queue's idempotence
    makes harmless and its counters make visible, but which wastes a turn every time. Three
    answers, because the three situations call for different next moves.
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
        # **The arm the fix was about, and the table had no row for it.** `_validated` used to
        # hand-copy three of `validated_skill`'s four checks and omit the fourth — that the name is
        # not one the deployment already ships — and the commit's only test change was an import
        # allowlist. Swallowing the `conflict=True` refusal leaves 34 tests green (48 with
        # `test_api_proposals` and `test_local_skills`) and reproduces the defect verbatim: shipped
        # code refuses `'protocol-generation' is the name of a skill this deployment already ships`,
        # the mutation answers "proposed … waiting for this chemist to accept or decline" while
        # `POST /proposals/skill/protocol-generation` answers **409**, so the proposal cannot be
        # accepted and the only exit is declining one they wanted.
        #
        # The name is taken from `shipped_skill_names()` rather than written down, so a rename in
        # `skills/` carries this row instead of emptying it.
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
    """Validated here rather than at acceptance, and that ordering is the point.

    `POST /skills/mine` refuses a malformed `SKILL.md`, so a proposal that skipped this check would
    be reviewed, accepted, and then fail at the write — the worst place to discover it, because the
    person has already decided and the failure looks like the system losing their decision.
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
