"""Every agent-callable tool is exercised by a probe, or is exempt with a pointer to what covers it.

`tests/test_repo_map.py` proves the pattern this file copies: a declaration checked against the tree
**in both directions**, so neither side can drift quietly. There it is directories against
`ARCHITECTURE.md`; here it is `data/evals/probes/` against the tool surface.

**The hole this closes opened silently and would have kept opening.** The 2026-08-25 field benchmark
measured 232 probes naming 50 of 67 agent-callable tools, and the seventeen with no probe were not a
random seventeen — they were the *newest* surface. The scratchpad and memory tools the M-phases
added, and `task`, the subagent seam three merged ADRs argue about. Nobody removed their coverage;
the corpus was written against the capability the system had when the corpus was written, and
nothing re-derived it afterwards.

**Two lists, and they mean different things.** `EXEMPT` is a claim that another suite covers the
tool as a *conversation* rather than as a turn. `GRANDFATHERED` claims nothing — it records tools
that were already on the surface when this gate arrived, so the gate could be introduced without
blocking unrelated work. Eighteen of those came from a single merge that added eighteen tools and
32% to the static context floor, which is exactly the event this file exists to make visible and
which landed while the file was still on a branch.

**The exemption list is the design decision, and an exemption must name what covers it instead.**
Some tools genuinely should not appear in an `expects_tools` line — `write_todos` is the plan
surface and is driven as a *conversation* by `data/evals/probes/m12/plan_gate.yaml`, which is the
right shape for it. An exemption that names another suite is a statement about where the coverage
moved. An exemption that names nothing is a hole with a note on it, so this file refuses one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.evals.probe import Probe, ProbeSet

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "evals" / "probes"

#: Tools no probe names, each mapped to what covers it instead.
#:
#: **The value is not a comment, it is the exemption.** A tool here without a real pointer is a
#: coverage hole wearing a label, which is the thing this file exists to make impossible.
EXEMPT: dict[str, str] = {
    "write_todos": (
        "the plan surface, driven as a conversation rather than a turn — "
        "data/evals/probes/m12/plan_gate.yaml and chemclaw.evals.live.run_plan_gate_probe"
    ),
    "task": (
        "subagent delegation, whose contract is the compiled graph a helper runs on rather than "
        "an answer — tests/test_subagents.py, and D-2026-08-10-a-subagent-is-an-attenuation-"
        "not-a-new-actor for the invariants it must keep"
    ),
}


def _probes() -> list[Probe]:
    """Every probe in the corpus, from both the flat files and the M12 suites.

    Parsed through `ProbeSet` rather than read as loose dicts, so this file also fails when a probe
    is malformed — a `bucket` typo or a `section` outside 1-17 would otherwise sit in the corpus
    being counted and never asked.
    """
    found: list[Probe] = []
    for path in sorted(PROBE_DIR.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        found.extend(ProbeSet.model_validate(document).probes)
    return found


def _expected_tools() -> set[str]:
    """Every tool name any probe declares it expects."""
    return {name for probe in _probes() for name in probe.expects_tools}


@pytest.fixture(scope="module", autouse=True)
def _profiles_loaded() -> None:
    """`available_tool_names()` spans the profiles, which are discovered from disk."""
    load_profiles()


#: Tools that predate this gate and have no probe yet — debt, not coverage.
#:
#: **Distinct from `EXEMPT`, and the distinction is the whole point.** An exemption is a claim that
#: another suite covers the tool as a conversation. This list claims nothing: it is a set of tools
#: that were on the surface before this file existed, recorded so the gate can be introduced without
#: either blocking unrelated work or quietly pretending the corpus is complete.
#:
#: Eighteen of them arrived in one merge — the GFN multi-step work — which is precisely the event
#: this gate was built to make visible, and it landed while the gate was still on a branch. Draining
#: this list is a `BACKLOG.md` row. **The set may only shrink**, which the test below enforces, so a
#: future merge cannot use it as a place to put a nineteenth.
GRANDFATHERED: frozenset[str] = frozenset(
    {
        "compute_ensemble_property",
        "describe_topology",
        "enumerate_bond_cleavages",
        "enumerate_degradants",
        "enumerate_protonation_states",
        "enumerate_stereoisomers",
        "enumerate_tautomers",
        "rank_species",
        "refine_ensemble",
        "run_bond_strength_survey",
        "run_degradant_triage",
        "run_ensemble_free_energy",
        "run_microspecies_profile",
        "run_regioselectivity_in_conformer",
        "run_stereoisomer_ranking",
        "run_tautomer_resolution",
        "survey_bond_strengths",
        "transform_structure",
    }
)


def test_the_grandfathered_set_only_shrinks() -> None:
    """A tool that gains a probe leaves this list; nothing may be added to it.

    Without this, `GRANDFATHERED` is just `EXEMPT` without the honesty requirement — the next merge
    to add a tool would drop it in here and the gate would measure nothing. The count is asserted
    rather than the membership so that *removing* one needs no edit here beyond the deletion.
    """
    assert len(GRANDFATHERED) <= 18, (
        f"GRANDFATHERED has grown to {len(GRANDFATHERED)}. It is a record of tools that predate "
        "this gate, not a place to put a new one — write a probe instead."
    )
    covered = sorted(GRANDFATHERED & _expected_tools())
    assert not covered, (
        f"{covered} now have probes and are no longer grandfathered. Delete them from the set — a "
        "debt list that outlives the debt reads as live state."
    )


def test_every_agent_callable_tool_is_probed_or_exempt() -> None:
    """The first direction: a tool the corpus has never heard of is a tool nothing measures."""
    unprobed = sorted(available_tool_names() - _expected_tools() - set(EXEMPT) - GRANDFATHERED)
    assert not unprobed, (
        f"{len(unprobed)} agent-callable tool(s) appear in no probe's `expects_tools`:\n  "
        + "\n  ".join(unprobed)
        + "\n\nWrite a probe in data/evals/probes/, or add the tool to EXEMPT with the suite that "
        "covers it instead. An exemption with no pointer is not accepted."
    )


def test_no_probe_expects_a_tool_that_does_not_exist() -> None:
    """The second direction: a probe naming a tool that is gone can never fail correctly.

    It passes for the wrong reason — the model was never going to call a name that is not on the
    surface — so the probe stops testing while still counting toward the corpus. Measured zero on
    2026-08-25, and worth keeping at zero.
    """
    phantom = sorted(_expected_tools() - available_tool_names())
    assert not phantom, (
        f"these probes expect tools that no longer exist: {phantom}. Either the tool was renamed "
        "and the probe was not, or the probe outlived its capability."
    )


def test_every_exemption_names_what_covers_it() -> None:
    """An exemption is a claim that the coverage moved. This is the claim being checked."""
    empty = sorted(name for name, reason in EXEMPT.items() if len(reason.strip()) < 40)
    assert not empty, (
        f"{empty} are exempt with no real pointer. Name the suite, the test module or the ADR that "
        "covers the tool instead — otherwise this list is a hole with a label on it."
    )


def test_no_exemption_outlives_its_reason() -> None:
    """The other direction on the exemptions: one that is now probed should stop being exempt.

    Same rule `DEFERRED.md` and `BACKLOG.md` both run on — a row that outlives its closure reads as
    live state, so it is deleted rather than annotated.
    """
    redundant = sorted(set(EXEMPT) & _expected_tools())
    assert not redundant, (
        f"{redundant} are now named by a probe and no longer need an exemption. Delete them from "
        "EXEMPT."
    )


def test_no_exemption_names_a_tool_that_does_not_exist() -> None:
    """And the third: an exemption for a deleted tool is a claim about nothing."""
    gone = sorted(set(EXEMPT) - available_tool_names())
    assert not gone, f"{gone} are exempt but are not on the agent surface at all. Delete them."


def test_the_corpus_is_not_concentrated_on_one_tool() -> None:
    """No single tool may be what most of the corpus measures.

    Measured on 2026-08-25: `gather_evidence` was in 116 of 232 probes — half the corpus testing one
    retrieval path. A corpus shaped like that reports broad coverage and delivers narrow coverage,
    and it is also why ChemToolAgent's finding (that tools do not consistently beat the base model)
    could not be reproduced against this system.

    The bound is deliberately loose. This is not asking for a flat distribution — a retrieval tool
    *should* be the most common thing an agent reaches for — it is asking that no one tool be a
    majority of what the suite knows how to check.
    """
    probes = _probes()
    counts: dict[str, int] = {}
    for probe in probes:
        for name in probe.expects_tools:
            counts[name] = counts.get(name, 0) + 1
    if not counts:  # pragma: no cover — an empty corpus is the test above's problem.
        return
    tool, hits = max(counts.items(), key=lambda item: item[1])
    share = hits / len(probes)
    assert share <= 0.60, (
        f"{tool} is expected by {hits} of {len(probes)} probes ({share:.0%}). Broaden the corpus "
        "rather than raising this bound: a suite that mostly measures one tool reports coverage it "
        "does not have."
    )
