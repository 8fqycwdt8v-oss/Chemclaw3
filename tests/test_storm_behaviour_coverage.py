"""Every agent-callable tool is called by a storm behaviour, or is exempt with what covers it.

The same declaration-checked-in-both-directions shape as `tests/test_probe_coverage.py`, and for
the same reason that file gives: a corpus written against the capability the system had when the
corpus was written goes stale silently, because nobody removes coverage — the surface simply grows
past it.

**The hole here was wider than the probe corpus's ever was.** Measured before this file existed,
`cli/storm_behaviours.py` declared twenty behaviours naming **five** of the ninety-nine tools the
agent advertises: `find_notes`, `gather_evidence`, `expand_note`, `find_past_jobs` and
`compute_reaction_energy`. The storm's own report said "the tool path is genuinely exercised" — of
5% of the surface. That is LOAD-1's shape one level up: the harness measuring something narrower
than the thing it named, and the previous version of exactly that mistake is what the mock's
`_validate` was written for.

**`_validate` is not this check, and the difference is the point.** It refuses a behaviour naming a
tool or an argument the system does not have — the *first* direction, per behaviour. It cannot
notice a tool that no behaviour names at all, because nothing hands it that tool. So the gate that
keeps the catalogue honest has to be here, over the surface rather than over the catalogue.

**An exemption must name what covers the tool instead**, exactly as in `test_probe_coverage.py`: an
exemption that names nothing is a hole with a label on it, so a short value fails. And the reverse
holds too — an exemption for a tool that is now driven by a behaviour, or for a tool that has left
the surface, is stale state and fails rather than being annotated.
"""

from __future__ import annotations

import pytest

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.cli.storm_behaviours import BEHAVIOURS
from chemclaw.connectors.jobs import build_job_tool
from chemclaw.connectors.registry import enabled
from chemclaw.templates.registry import build_template_tool
from chemclaw.templates.registry import enabled as enabled_templates
from chemclaw.templates.registry import tool_name as template_tool_name

#: Tools no storm behaviour calls, each mapped to what drives it instead.
#:
#: **The value is not a comment, it is the exemption.** Both entries are tools the mock genuinely
#: must not emit rather than tools nobody got round to: driving them from a scripted model would
#: measure the harness rather than the system.
EXEMPT: dict[str, str] = {
    "write_todos": (
        "the plan surface. A behaviour is a fixed plan, and `agent/plan_gate.py` reads the todo "
        "list as it stands at that instant, so a scripted write_todos would be the storm approving "
        "its own plan — it is driven as a conversation instead by "
        "data/evals/probes/m12/plan_gate.yaml and chemclaw.evals.live.run_plan_gate_probe"
    ),
    "task": (
        "subagent delegation, whose contract is the compiled graph the helper runs on rather than "
        "an answer — and a scripted `task` under the mock would put the same mock on both sides of "
        "the delegation. tests/test_subagents.py holds it, with "
        "D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor for the invariants it must keep"
    ),
}


@pytest.fixture(scope="module", autouse=True)
def _profiles_loaded() -> None:
    """`available_tool_names()` spans the profiles, which are discovered from disk."""
    load_profiles()


def _called_tools() -> set[str]:
    """Every tool name any behaviour in the catalogue emits a call for.

    The adversarial behaviours are included rather than filtered: `f-unknown-tool` names a tool
    that does not exist and `f-empty-name` names none at all, and both are caught by the second
    direction below only if this set is the *literal* call surface rather than a cleaned-up one.
    """
    return {call.tool for behaviour in BEHAVIOURS for call in behaviour.calls}


def test_every_agent_callable_tool_is_called_or_exempt() -> None:
    """The first direction: a tool no behaviour calls is one the storm can say nothing about."""
    uncalled = sorted(available_tool_names() - _called_tools() - set(EXEMPT))
    assert not uncalled, (
        f"{len(uncalled)} agent-callable tool(s) are named by no storm behaviour:\n  "
        + "\n  ".join(uncalled)
        + "\n\nAdd a behaviour in src/chemclaw/cli/storm_behaviours.py — with arguments the tool "
        "would actually accept, since a call that dies in the parse-error branch is LOAD-1 — and "
        "name it from a check in cli/live_storm.py. Or add the tool to EXEMPT with what drives it "
        "instead. An exemption with no pointer is not accepted."
    )


def test_every_exemption_names_what_covers_it() -> None:
    """An exemption is a claim that the coverage moved. This is the claim being checked."""
    empty = sorted(name for name, reason in EXEMPT.items() if len(reason.strip()) < 40)
    assert not empty, (
        f"{empty} are exempt with no real pointer. Name the suite, the test module or the ADR that "
        "drives the tool instead — otherwise this list is a hole with a label on it."
    )


def test_no_exemption_outlives_its_reason() -> None:
    """The other direction on the exemptions: one that is now driven should stop being exempt.

    Same rule `DEFERRED.md` and `BACKLOG.md` both run on — a row that outlives its closure reads as
    live state, so it is deleted rather than annotated.
    """
    redundant = sorted(set(EXEMPT) & _called_tools())
    assert not redundant, (
        f"{redundant} are now called by a behaviour and no longer need an exemption. Delete them "
        "from EXEMPT."
    )


def test_no_exemption_names_a_tool_that_does_not_exist() -> None:
    """And the third: an exemption for a deleted tool is a claim about nothing."""
    gone = sorted(set(EXEMPT) - available_tool_names())
    assert not gone, f"{gone} are exempt but are not on the agent surface at all. Delete them."


def test_no_real_tool_is_covered_only_by_an_adversarial_behaviour() -> None:
    """The fourth: `adversarial=True` opts out of argument validation, so it cannot be coverage.

    `mock_llm._validate` skips an adversarial behaviour entirely — that is what the flag is for,
    since `f-unknown-tool` and `f-empty-name` exist precisely to send names the surface does not
    have. The consequence is that an adversarial call proves nothing about a *real* tool's
    arguments, so a tool whose only appearance in the catalogue is one would satisfy the first
    direction above while being exactly as unmeasured as before.

    Not hypothetical: `find_notes` is named by five adversarial behaviours, and if the ordinary
    ones were ever deleted this file would still call it covered.
    """
    surface = available_tool_names()
    hidden = sorted(
        call.tool
        for behaviour in BEHAVIOURS
        if behaviour.adversarial
        for call in behaviour.calls
        if call.tool in surface
        and call.tool not in {c.tool for b in BEHAVIOURS if not b.adversarial for c in b.calls}
    )
    assert not hidden, (
        f"{hidden} are only ever called by an adversarial behaviour, which skips argument "
        "validation. A real tool's only coverage must not be a call the system is meant to reject."
    )


def test_every_job_and_template_payload_validates_against_its_own_model() -> None:
    """LOAD-1, over the half `mock_llm._validate` is structurally unable to reach.

    `_validate` checks a call's argument *names* against the real callable's `__annotations__`, and
    skips any tool it cannot find in the in-process registry — which is every connector job and
    every template launcher. Those are the calls whose arguments are hardest to get right: the
    whole payload is nested under one `params` key, so the name check `_validate` does perform sees
    a single correct argument and learns nothing about the twelve fields inside it. A behaviour
    with a mistyped `kind` or a missing `solvents` would pass every check in this file and in the
    mock, and then die at launch validation before the job existed — the parse-error branch, one
    layer down.

    The model is read off the tool the agent actually binds (`build_job_tool` /
    `build_template_tool` both annotate `params` with it), rather than off the manifest, so this
    validates against the same class `prepare_job_launch` will.
    """
    jobs = {job.name: (manifest.name, job) for manifest in enabled() for job in manifest.jobs}
    templates = {template_tool_name(t): t for t in enabled_templates()}

    for behaviour in BEHAVIOURS:
        if behaviour.adversarial:  # its arguments are meant to be rejected
            continue
        for call in behaviour.calls:
            if call.tool in jobs:
                connector, job = jobs[call.tool]
                model = build_job_tool(connector, job).__annotations__["params"]
            elif call.tool in templates:
                model = build_template_tool(templates[call.tool]).__annotations__["params"]
            else:
                continue
            model.model_validate(call.arguments["params"])
