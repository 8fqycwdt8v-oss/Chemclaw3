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

**There used to be a second list beside `EXEMPT`, and it is gone because it was paid off.**
`GRANDFATHERED` recorded the tools already on the surface when this gate arrived, so the gate could
be introduced without blocking unrelated work; seventeen of them came from the single merge that
added seventeen tools and 32% to the static context floor, which is exactly the event this file
exists to make visible and which landed while the file was still on a branch. Every one of them is
now probed by `data/evals/probes/multistep-calculation.yaml`, so the list is deleted rather than
left empty — a debt list that outlives its debt reads as live state, which is the rule
`DEFERRED.md`, `BACKLOG.md` and `test_context_floor.py::KNOWN_OVERSIZED` all run on. Nothing about
enforcement changes: a tool added after this gate existed could never reach that list anyway, so
the only thing it ever held was a closed, dated record.

**The exemption list is the design decision, and an exemption must name what covers it instead.**
Some tools genuinely should not appear in an `expects_tools` line — `write_todos` is the plan
surface and is driven as a *conversation* by `data/evals/probes/m12/plan_gate.yaml`, which is the
right shape for it. An exemption that names another suite is a statement about where the coverage
moved. An exemption that names nothing is a hole with a note on it, so this file refuses one.
"""

from __future__ import annotations

import re
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


def test_every_agent_callable_tool_is_probed_or_exempt() -> None:
    """The first direction: a tool the corpus has never heard of is a tool nothing measures."""
    unprobed = sorted(available_tool_names() - _expected_tools() - set(EXEMPT))
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


def _knowledge_note_ids() -> set[str]:
    """Every note id in the shipped corpus, by filename stem."""
    root = Path(__file__).resolve().parents[1] / "knowledge"
    return {path.stem for path in root.rglob("*.md") if path.stem != "README"}


def test_no_probe_expects_a_note_that_does_not_exist() -> None:
    """The offline half of the gold set: a label pointing at a note that is gone grades nothing.

    It fails for the wrong reason — the retriever was never going to return an id the corpus does
    not hold — so the pair stops measuring retrieval and starts measuring the label, while still
    counting toward the run's recall. This is the half CI can run; the recall itself needs a front
    door and is scored by `make live-probes`.
    """
    expected = {name for probe in _probes() for name in probe.expects_notes}
    assert expected, "no probe declares `expects_notes`; this test proves nothing"
    phantom = sorted(expected - _knowledge_note_ids())
    assert not phantom, (
        f"these probes expect notes the corpus does not hold: {phantom}. Either the note was "
        "renamed and the probe was not, or the label outlived its note."
    )


def test_every_note_a_direction_names_is_declared_as_data() -> None:
    """The pairs stay readable as data rather than sliding back into prose.

    46 labelled (query, note) pairs across 20 probes existed for months inside `direction:`, where
    only a human grader could read them — `DEFERRED.md` recorded the consequence as "the shipped
    graph has none", then corrected itself to "unreadable as data because `Probe` is
    `extra='forbid'`". Transcribing them once fixes that instant and nothing keeps it fixed: the
    next probe written in the same style re-opens the same hole, silently, because a direction
    naming a note still reads like a complete probe.

    So this asserts the direction and the field agree. A note id a direction names and the field
    omits is the hole coming back; the converse is allowed, because a label may be more precise
    than the prose that motivated it.
    """
    corpus = _knowledge_note_ids()
    missing: dict[str, list[str]] = {}
    for probe in _probes():
        named = sorted(
            note
            for note in corpus
            if re.search(rf"(?<![\w-]){re.escape(note)}(?![\w-])", probe.direction)
        )
        gap = sorted(set(named) - set(probe.expects_notes))
        if gap:
            missing[probe.id] = gap
    assert not missing, (
        f"{len(missing)} probe(s) name a corpus note in `direction:` and not in `expects_notes`: "
        f"{missing}. A pair only a human grader can read is the hole this field closed."
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


def test_no_tools_only_coverage_is_a_question_the_surface_cannot_answer() -> None:
    """A tool covered solely by a bucket-C probe is a tool the corpus does not exercise.

    The corpus is deliberately mixed: bucket A the surface should answer, B partly, **C not at
    all**. That mix is right for measuring honesty, and it means "this tool appears in a probe" and
    "this tool is exercised" are different statements — a C probe is satisfied by the system
    *declining*, so a tool whose only probe is a C is covered on paper and never called.

    This is the thin half of the concentration question, and it is the half that turned out to
    matter. Measured 2026-09-15: 39 of 114 agent-callable tools are named by exactly **one** probe —
    34% of the surface resting on a single phrasing — and **zero** of them rest on a C. So the tail
    is thin and not hollow, and this assertion is what keeps it that way.

    That figure read 45 for one day and was stale on the commit that wrote it: `process-chemistry`
    (+661 lines of probes) landed in the same merge range, which is
    `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` happening to a paragraph that cites
    it two tests below. The **zero** is the load-bearing half and is what the assertion holds; the
    ratio is a snapshot and is dated so a reader can tell which it is.

    A count is not asserted, deliberately. A ratchet on "how many tools have one probe" would block
    every new tool until somebody wrote it a second question, which is a toll on adding capability
    rather than a bound on risk. What must not happen is a tool arriving with coverage that cannot
    call it.
    """
    by_tool: dict[str, list[Probe]] = {}
    for probe in _probes():
        for name in probe.expects_tools:
            by_tool.setdefault(name, []).append(probe)
    live = available_tool_names()
    hollow = sorted(
        name
        for name, probes in by_tool.items()
        if name in live and all(probe.bucket == "C" for probe in probes)
    )
    assert by_tool, "no probe names a tool; this test proves nothing"
    assert not hollow, (
        f"{hollow} are named only by bucket-C probes, which the surface is not expected to answer. "
        "Such a tool is covered on paper and never called. Give each one a bucket A or B question."
    )


def test_the_corpus_is_not_concentrated_on_one_tool() -> None:
    """No single tool may be what most of the corpus measures.

    Measured on 2026-08-25: `gather_evidence` was in 116 of 232 probes — half the corpus testing one
    retrieval path. A corpus shaped like that reports broad coverage and delivers narrow coverage,
    and it is also why ChemToolAgent's finding (that tools do not consistently beat the base model)
    could not be reproduced against this system.

    The bound is deliberately loose. This is not asking for a flat distribution — a retrieval tool
    *should* be the most common thing an agent reaches for — it is asking that no one tool be a
    majority of what the suite knows how to check.

    **Re-measured 2026-09-15 and the concentration has gone**: `gather_evidence` is in 139 of 333
    probes, **41.7%**. The 2026-08-25 figure above is kept because it is why the bound exists, not
    because it is current — a paragraph that reads as a live measurement is the thing
    `D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` is about, and the live number is
    whatever this assertion computes. Which is why the 126-of-297 this said first was wrong within
    its own merge: `process-chemistry.yaml` added 36 probes in the same range and nothing re-ran the
    count.
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
