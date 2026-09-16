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

**The second subject is the claim that runs the other way, and it arrived because this file could
not see it.** A probe may also assert that a capability is *absent*, and
`D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing` found six sites
doing so while the declared `safety` bundle bound all three capabilities they denied. Nothing here
caught it, for a structural reason:
`test_no_tools_only_coverage_is_a_question_the_surface_cannot_answer` builds `by_tool` from
`expects_tools`, and a probe that wrongly asserts a capability is absent names no tool at all. So
`Probe.asserts_absent` makes the claim structured, and the three tests below resolve it against the
same surface every other assertion in this file reads.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest
import yaml

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.evals.probe import ABSENT_MARKER, Probe, ProbeSet
from tests.siblings import SIBLING_SKIP, fleet_published_tool_names, sibling_root

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
    phantom = sorted(_expected_tools() - available_tool_names() - fleet_expected_tools())
    assert not phantom, (
        f"these probes expect tools that no longer exist: {phantom}. Either the tool was renamed "
        "and the probe was not, or the probe outlived its capability. A tool the fleet serves and "
        "this tree declares no bundle for is not a phantom — say so with `needs_bundle:` on the "
        "probe, which is checked against the fleet's own manifests separately."
    )


def fleet_expected_tools() -> set[str]:
    """Expected tool names that a probe declared a fleet bundle for and this tree cannot resolve.

    Public because `tests/test_live_probes.py` asserts the same rule over the live runner's own
    loader and must not restate this one — two definitions of one invariant is how the second
    becomes the weaker, and it already had: that file's copy covered 336 probes to this file's 338.

    Surface-aware on purpose. A probe is allowed to mix the two — an-04 pastes six injections and
    wants `replicate_precision` from the fleet's `suitability` *and* `predict_pka` from this tree —
    so forgiving every name on a `needs_bundle:` probe would quietly exempt the local ones from the
    phantom check too, and a renamed in-process tool would stop being caught on the probes that
    need catching most.

    Read from the probes rather than from a list beside this test, so the declaration lives on the
    question that depends on it. What this returns is only "the corpus says these come from a bundle
    we do not declare"; whether that is *true* is
    `test_every_fleet_served_expectation_names_a_tool_that_bundle_declares`'s question, and it needs
    the sibling checkout to answer.
    """
    surface = available_tool_names()
    return {
        name
        for probe in _probes()
        if probe.needs_bundle is not None
        for name in probe.expects_tools
        if name not in surface
    }


def test_every_fleet_served_expectation_names_a_tool_that_bundle_declares() -> None:
    """The cross-repository half: a `needs_bundle:` pairing is checked against the fleet's manifest.

    `needs_bundle` is what lets a probe name a tool this checkout cannot resolve, so on its own it
    is an assertion with nothing behind it — exactly the shape
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` is about. This is what
    puts something behind it: the fleet's published `manifests/` are read off disk, and every name a
    `needs_bundle:` probe expects has to be either on this tree's own surface or declared by the
    bundle it named. Both halves, because a probe may legitimately mix them.

    Reading YAML from a shallow clone rather than running the fleet's servers, deliberately — the
    cheap tier `tests/test_sibling_manifest_agreement.py` uses, so this is plausible in CI where the
    schema measurement in `tests/test_context_floor.py` is not. What the fleet *declares* and what
    it *serves* are held to each other on that side, by `assert_manifest_matches` against a running
    server, so a declared-but-unserved name fails there rather than passing quietly here.

    **It does less in the lane that mounts the fleet, and that is worth saying rather than
    discovering.** With `CHEMCLAW_CONNECTORS_DIR` pointed at the fleet's `manifests/`, every name is
    already on the surface and this passes without consulting the manifest at all. That lane is not
    unguarded — it is the lane where `test_every_agent_callable_tool_is_probed_or_exempt` has 121
    tools to account for instead of 114 — but the guard against a *typo in a fleet name* is this
    one, and it is the bare checkout that runs it.

    **A skip, loudly, rather than a green line.** With no sibling checkout this cannot tell a fleet
    tool from a typo, and `tests/conftest.py::_report_sibling_skips` counts the skip so the run says
    what it did not look at.
    """
    declaring = [probe for probe in _probes() if probe.needs_bundle is not None]
    if not declaring:
        pytest.skip("no probe declares `needs_bundle:`, so there is no pairing to check")
    surface = available_tool_names()
    unresolved = {
        (str(probe.needs_bundle), name)
        for probe in declaring
        for name in probe.expects_tools
        if name not in surface
    }
    if not unresolved:
        return
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(
            f"{SIBLING_SKIP} the {len(unresolved)} fleet-served tool expectations in the probe "
            f"corpus were NOT checked against the fleet's own manifests: {reason}. Nothing in this "
            "run is evidence about whether those probes name tools that exist."
        )
    declared = fleet_published_tool_names(root)
    missing = sorted(
        f"{bundle}::{tool}"
        for bundle, tool in unresolved
        if tool not in declared.get(bundle, frozenset())
    )
    assert not missing, (
        f"these probes declare a fleet bundle that does not serve the tool they name: {missing}. "
        f"The fleet publishes {sorted(declared)}. Either the tool was renamed in Chemclaw3-mcp and "
        "the probe was not, or the probe names the wrong bundle."
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
    (28 probes) and `delegation` (8) landed in the same merge range, which is
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
    its own merge: the same range added 36 probes — 28 in `process-chemistry.yaml`, 8 in
    `delegation.yaml` — and nothing re-ran the count.
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


def test_a_tool_the_deployment_does_not_bind_is_not_scored_as_a_miss() -> None:
    """The scoring half of `needs_bundle:`, driven rather than trusted.

    `test_every_fleet_served_expectation_names_a_tool_that_bundle_declares` above proves the corpus
    may *name* a fleet tool. This proves the other half does not then punish it: a probe whose tools
    are absent from the surface the run was launched against is not measuring the model, and scoring
    it as a miss is how a corpus comes to penalise capability that exists somewhere else — the same
    defect as the six probes in
    `D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing`, arriving
    from the other direction.

    Three arms, because the middle one is what makes the first mean anything: a name nothing binds
    is not scored, a name that *is* bound and was not called **is** scored as a miss, and a bound
    name that was called passes.
    """
    from chemclaw.evals.live import ProbeOutcome, _tool_expectation_applies

    def probe(tools: list[str], bundle: str | None = None) -> Probe:
        return Probe(
            id="x",
            section=11,
            persona="lab_leader",
            bucket="B",
            question="q",
            direction="d",
            expects_tools=tools,
            needs_bundle=bundle,
        )

    def outcome(degraded: list[str] | None = None) -> ProbeOutcome:
        """A real `ProbeOutcome`, not a stand-in — the gate reads `degraded` off the model."""
        return ProbeOutcome(
            probe_id="x",
            section=11,
            persona="lab_leader",
            bucket="B",
            question="q",
            degraded=degraded or [],
        )

    bound = sorted(available_tool_names())[0]

    assert not _tool_expectation_applies(
        probe(["a_tool_no_deployment_here_binds"], "thermalsafety"), outcome()
    ), "a name absent from the surface must not be scored: the fleet's capability reads as a miss"
    assert _tool_expectation_applies(probe([bound]), outcome()), (
        "a bound tool must still be scored — without this arm the first one would pass even if "
        "the gate always returned False, which would silence the corpus rather than correct it"
    )
    assert not _tool_expectation_applies(
        probe([bound], "thermalsafety"), outcome(["thermalsafety"])
    ), "a bundle whose server did not answer this turn is the deployment's failure, not the model's"


#: A snake_case token — the only shape an `asserts_absent` marker can carry that is unambiguously a
#: tool name rather than prose. Single-word tool names (`task`, `grep`, `ls`, `delete`) are
#: deliberately outside it: a scan that matched those would fire on any sentence containing the
#: word, so the marker arm cannot see a denial of one of *those* capabilities. Stated rather than
#: left to be found, because it is the hole in the half of this control that is already the weaker.
_TOOL_SHAPED = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

#: How close a name has to sit to a bound tool before it is read as a misspelling of that tool
#: rather than as a capability this system genuinely lacks. Measured on the shipped surface:
#: `ich_impurity_limits` scores above this against `ich_impurity_limit` and `screen_genotoxic_alert`
#: against `screen_genotoxic_alerts`, while the corpus's two real tool-name claims — `run_python`,
#: which the fleet's `pyexec` serves and no deployment here binds, and `delete`, the filesystem verb
#: `agent/scratchpad.py` withholds — match nothing at all.
NEAR_MISS_RATIO = 0.85

#: The shortest a `NO-TOOL` marker's phrase may be. Not a quality bar and it cannot be one — a
#: reader is what checks a marker. It is the floor that stops the field being satisfied by the
#: marker alone, which would make "required field" mean nothing at all.
MIN_MARKER_PHRASE = 20


def _absence_claims(probes: list[Probe]) -> list[tuple[str, str]]:
    """Every `(probe id, claim)` pair the corpus declares."""
    return [(probe.id, claim) for probe in probes for claim in probe.asserts_absent]


def absence_claims_the_surface_refutes(
    claims: list[tuple[str, str]], surface: set[str]
) -> list[str]:
    """The claims `surface` contradicts, each as a sentence naming the probe and what is bound.

    Two arms, because there are two ways to assert an absence and both can be false:

    - **A tool name that is bound.** This is the defect
      `D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing` measured,
      stated so that it resolves: a probe claiming `ich_impurity_limit` is absent fails the moment
      the `safety` bundle is declared, which it has been the whole time.
    - **A marker that names a bound tool inside its own prose.** Without this the marker arm is a
      way to launder the first: `"NO-TOOL no ich_impurity_limit here"` would pass a check that only
      looked at bare names. A marker is for a capability *no tool name reaches*, so a tool name
      inside one is either the defect or the wrong arm.

    Shared by the corpus assertion and by the driven one below, which is what stops this being a
    guard nobody has watched refuse.

    Args:
        claims: `(probe id, claim)` pairs, as `_absence_claims` produces them.
        surface: The tool names the agent binds.

    Returns:
        One sentence per refuted claim, empty when the corpus and the surface agree.
    """
    refuted: list[str] = []
    for probe_id, claim in claims:
        if not claim.startswith(ABSENT_MARKER):
            if claim in surface:
                refuted.append(f"{probe_id} asserts `{claim}` is absent and the agent binds it")
            continue
        named = sorted(set(_TOOL_SHAPED.findall(claim)) & surface)
        if named:
            refuted.append(
                f"{probe_id}'s marker names bound tool(s) {named} inside the capability it "
                "claims is missing"
            )
    return refuted


def test_every_bucket_c_probe_names_the_capability_it_asserts_is_missing() -> None:
    """A bucket-C probe's absence claim is a field, so that something other than a reader has it.

    Required on C, permitted on B — gr-27's *"alert screening is available; M7 classification and
    TTC-based control limits are not"* is a B probe with an absence claim in it, and the corpus
    should be able to say which half — and refused on A, where a probe would be asserting both that
    the capability exists and that it does not.

    The shape rules are the whole of what a machine can say about the marker arm: a marker carries a
    phrase rather than standing in for one, and a bare entry looks like a tool name rather than like
    a sentence somebody forgot to prefix. Beyond that the marker **buys a reviewable lie in place of
    an invisible one rather than an impossible one**, which is the sentence
    `docs/planning/BACKLOG.md` asked to survive into the implementation: an author who would write
    the absence claim wrongly will write the marker wrongly too.
    """
    probes = _probes()
    silent = sorted(
        probe.id for probe in probes if probe.bucket == "C" and not probe.asserts_absent
    )
    assert not silent, (
        f"{len(silent)} bucket-C probe(s) assert a capability is absent and name none of it: "
        f"{silent}. Add `asserts_absent:` — a tool name this deployment must not bind, or "
        f"'{ABSENT_MARKER}<what is missing>' where no tool name reaches it."
    )
    contradictory = sorted(
        probe.id for probe in probes if probe.bucket == "A" and probe.asserts_absent
    )
    assert not contradictory, (
        f"{contradictory} are bucket A — the capability exists and the probe should exercise it — "
        "and also declare it absent. One of the two is wrong."
    )
    malformed = sorted(
        f"{probe_id}: {claim!r}"
        for probe_id, claim in _absence_claims(probes)
        if (
            len(claim.removeprefix(ABSENT_MARKER).strip()) < MIN_MARKER_PHRASE
            if claim.startswith(ABSENT_MARKER)
            else not re.fullmatch(r"[a-z][a-z0-9_]*", claim)
        )
    )
    assert not malformed, (
        f"these absence claims are neither a tool name nor a stated capability: {malformed}. A "
        "bare entry is resolved against the surface, so it must be a tool name; a "
        f"'{ABSENT_MARKER}' entry is read by a person, so it has to say what is missing."
    )


def test_no_probe_asserts_a_capability_the_agent_surface_serves() -> None:
    """The point of the field: an absence claim the deployment refutes fails here, not in a run.

    What that failure looked like before this existed: an-28 asked for the ICH Q3D limit for
    palladium, carried `expects_tools: []`, and forbade *"an ICH Q3D PDE value in ug/day"* while
    `ich_impurity_limit` returned exactly that with its Table A.2.1 citation — so a model that
    looked it up and cited it scored as fabricating, and one that refused scored correct. Six sites
    were stale that way and nothing in this file could see any of them, because they named no tool.
    """
    refuted = absence_claims_the_surface_refutes(_absence_claims(_probes()), available_tool_names())
    assert not refuted, (
        "these probes claim a capability is missing that this deployment binds:\n  "
        + "\n  ".join(refuted)
        + "\n\nRe-bucket the probe to B, name the tool in `expects_tools`, and forbid only what is "
        "genuinely still absent — in wording that holds in both lanes, as gr-25's 'a limit "
        "recalled from memory rather than looked up' does."
    )


def test_a_claim_naming_a_bound_tool_is_refused_whichever_arm_it_arrives_on() -> None:
    """Driven against its own defect, because a guard nobody has watched refuse is a claim.

    Four arms. The first two are the defect in each arm — a bare name that is bound, and the same
    name laundered through a marker — and the second two are what stops a check that always
    returned something from passing the first two: a name nothing binds and an ordinary marker are
    both left alone.
    """
    bound = sorted(available_tool_names())[0]
    assert absence_claims_the_surface_refutes([("x", bound)], available_tool_names())
    assert absence_claims_the_surface_refutes(
        [("x", f"{ABSENT_MARKER}nothing here serves {bound} or anything like it")],
        available_tool_names(),
    )
    assert not absence_claims_the_surface_refutes(
        [("x", "no_such_tool_is_bound_anywhere")], available_tool_names()
    )
    assert not absence_claims_the_surface_refutes(
        [("x", f"{ABSENT_MARKER}no equipment booking or instrument calendar interface")],
        available_tool_names(),
    )


def test_an_absence_claim_one_edit_from_a_bound_tool_is_read_as_the_typo_it_is() -> None:
    """The hole in the tool-name arm: a misspelling is absent from the surface and so passes it.

    `ich_impurity_limits` is not bound, so the assertion above has nothing to say about it — and a
    probe that claimed it was missing would be the an-28 defect with a letter added, silently. This
    is the cheap half of that: a claim that close to a bound name is a typo rather than a capability
    this system lacks. The expensive half — a claim that is simply *wrong* about a capability nobody
    named — is what a reader is for, and nothing here pretends otherwise.
    """
    surface = sorted(available_tool_names())
    typos = sorted(
        f"{probe_id}: {claim!r} looks like {near[0]!r}"
        for probe_id, claim in _absence_claims(_probes())
        if not claim.startswith(ABSENT_MARKER)
        and claim not in surface
        and (near := difflib.get_close_matches(claim, surface, n=1, cutoff=NEAR_MISS_RATIO))
    )
    assert not typos, (
        f"these absence claims are a near-miss of a tool this deployment binds: {typos}. A name "
        "the surface does not carry is not evidence the capability is missing — it is equally "
        "evidence the name was mistyped, and a mistyped claim can never fail."
    )
