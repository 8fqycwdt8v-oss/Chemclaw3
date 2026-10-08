"""Every agent-callable tool is exercised by a probe, or is exempt with a pointer to what covers it.

Like `tests/test_repo_map.py`, a declaration checked against the tree in both directions: here
`data/evals/probes/` against the tool surface, so new tools cannot arrive unprobed. An exemption
must name the suite that covers the tool instead (e.g. `write_todos` is driven as a conversation
by `data/evals/probes/m12/plan_gate.yaml`).

The second subject runs the other way: a probe may assert a capability is absent, and
`Probe.asserts_absent` makes that claim structured so it can be resolved against the same surface
(`D-2026-09-15-a-probe-that-forbids-the-answer-a-bound-tool-serves-measures-nothing`).
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest
import yaml

from chemclaw.agent.chemclaw_agent import available_tool_names, withheld_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.evals.probe import ABSENT_MARKER, Probe, ProbeSet
from tests.siblings import SIBLING_SKIP, fleet_published_tool_names, sibling_root

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "evals" / "probes"

#: Tools no probe names, each mapped to what covers it instead. The value is the exemption: an entry
#: without a real pointer is a coverage hole with a label.
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
    # Both act on an artefact that already exists, which a single-question probe cannot set up
    # (`kn-31` covers `create_exhibit`). Their refusal and diff rules are driven against a real
    # store.
    "read_exhibit": "an artefact a previous turn made — tests/test_exhibit_tools.py",
    "revise_exhibit": "an artefact a previous turn made — tests/test_exhibit_tools.py",
}


def _probes() -> list[Probe]:
    """Every probe in the corpus, from both the flat files and the M12 suites.

    Parsed through `ProbeSet`, so a malformed probe (bad `bucket` or `section`) fails here too.
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
    """No probe expects a tool that does not exist.

    Such a probe passes for the wrong reason while still counting toward the corpus.
    """
    phantom = sorted(
        _expected_tools() - available_tool_names() - fleet_expected_tools() - withheld_tools()
    )
    assert not phantom, (
        f"these probes expect tools that no longer exist: {phantom}. Either the tool was renamed "
        "and the probe was not, or the probe outlived its capability. A tool the fleet serves and "
        "this tree declares no bundle for is not a phantom — say so with `needs_bundle:` on the "
        "probe, which is checked against the fleet's own manifests separately."
    )


def withheld_tools() -> set[str]:
    """Launchers this tree declares and this deployment withholds: real tools, not phantoms.

    `republish_calculations` is withheld while no result sink is enabled (the default). Public so
    `tests/test_live_probes.py` imports it rather than restating the rule.
    """
    return withheld_tool_names()


def fleet_expected_tools() -> set[str]:
    """Expected tool names that a probe declared a fleet bundle for and this tree cannot resolve.

    Public so `tests/test_live_probes.py` shares one definition. Surface-aware: a `needs_bundle:`
    probe may mix fleet and local tools, so only the unresolvable names are forgiven and a renamed
    local tool is still caught. Whether the bundle really declares them is checked by
    `test_every_fleet_served_expectation_names_a_tool_that_bundle_declares`.
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
    """A `needs_bundle:` expectation names a tool that bundle's fleet manifest declares.

    Reads the fleet's published `manifests/` from the sibling checkout (YAML only, so plausible in
    CI); every expected name must be on this tree's surface or declared by the named bundle. The
    fleet holds declared against served on its side. With the fleet mounted on
    `CHEMCLAW_CONNECTORS_DIR` nothing is unresolved and this returns early, so the bare checkout is
    the lane that catches a typo in a fleet name. Without a sibling checkout it skips, and
    `tests/conftest.py::_report_sibling_skips` counts the skip.
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
    """No probe expects a note that does not exist.

    A gold label pointing at a missing note measures the label, not retrieval. Recall itself is
    scored by `make live-probes`.
    """
    expected = {name for probe in _probes() for name in probe.expects_notes}
    assert expected, "no probe declares `expects_notes`; this test proves nothing"
    phantom = sorted(expected - _knowledge_note_ids())
    assert not phantom, (
        f"these probes expect notes the corpus does not hold: {phantom}. Either the note was "
        "renamed and the probe was not, or the label outlived its note."
    )


def test_every_note_a_direction_names_is_declared_as_data() -> None:
    """Every note a probe's `direction:` names is also declared as data.

    Otherwise labelled pairs drift back into prose only a human grader reads. A label may be more
    precise than the direction, so only the converse is refused.
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

    Same rule `BACKLOG.md` runs on — a row that outlives its closure reads as
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
    """No tool's only coverage is a bucket-C probe.

    A C probe is satisfied by the system declining, so a tool covered only by one is never called.
    No count of single-probe tools is ratcheted, since that would tax adding capability.
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

    A corpus dominated by one retrieval path reports broad coverage while checking narrow coverage.
    The bound is loose: a retrieval tool should be the most common, just not a majority.
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
    """A tool the deployment does not bind is not scored as a miss.

    Three arms: an unbound name is not scored, a bound name not called is a miss, and a bound name
    called passes. The middle arm makes the first meaningful.
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


#: A snake_case token: the only shape in an `asserts_absent` marker unambiguously a tool name.
#: Single-word names (`task`, `grep`, `ls`, `delete`) are outside it, so the marker arm cannot see a
#: denial of those.
_TOOL_SHAPED = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

#: How close a name must be to a bound tool to read as a misspelling of it rather than a capability
#: this system lacks: `ich_impurity_limits` and `screen_genotoxic_alerts` score above it, while
#: `run_python` and `delete` match nothing. In a lane binding the fleet's `pyexec`, probes denying
#: `run_python` correctly go red.
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

    Two arms: a bare tool name that is bound, and a marker whose prose contains a bound tool name
    (otherwise markers would launder the first arm). Shared by the corpus assertion and the driven
    test.

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
    """Every bucket-C probe names the capability it asserts is missing.

    Required on C, permitted on B, refused on A (which would assert the capability both exists and
    does not). A marker must carry a phrase and a bare entry must look like a tool name. A marker
    makes a wrong claim reviewable, not impossible.
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
    """No probe asserts a capability the agent surface serves.

    Otherwise a model that looked up and cited a served value would score as fabricating, and one
    that refused would score as correct.
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
    """A claim naming a bound tool is refused on either arm, and benign claims are left alone.

    Four arms: a bound bare name, the same name in a marker, an unbound name and an ordinary marker,
    so a helper that always returns something cannot pass.
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


def absence_claims_that_near_miss_a_bound_tool(
    claims: list[tuple[str, str]], surface: set[str]
) -> list[str]:
    """The claims that misspell a bound tool, on either arm, each as a sentence naming the probe.

    A tool-shaped token inside a marker gets the same near-miss reading as a bare claim, so neither
    arm lets a one-letter variant of a bound tool through. `_TOOL_SHAPED` requires an underscore, so
    ordinary prose cannot trigger it.

    Args:
        claims: `(probe id, claim)` pairs, as `_absence_claims` produces them.
        surface: The tool names the agent binds.

    Returns:
        One sentence per near-miss, empty when nothing in the corpus misspells a bound tool.
    """
    ordered = sorted(surface)

    def near(token: str) -> str | None:
        if token in surface:
            return None  # exactly bound is the other guard's finding, not a typo.
        hit = difflib.get_close_matches(token, ordered, n=1, cutoff=NEAR_MISS_RATIO)
        return hit[0] if hit else None

    found: list[str] = []
    for probe_id, claim in claims:
        if not claim.startswith(ABSENT_MARKER):
            if (match := near(claim)) is not None:
                found.append(f"{probe_id}: {claim!r} looks like {match!r}")
            continue
        for token in sorted(set(_TOOL_SHAPED.findall(claim))):
            if (match := near(token)) is not None:
                found.append(f"{probe_id}: {token!r} inside a marker looks like {match!r}")
    return found


def test_an_absence_claim_one_edit_from_a_bound_tool_is_read_as_the_typo_it_is() -> None:
    """An absence claim one edit from a bound tool is read as a typo.

    A claim simply wrong about an unnamed capability is left to a reader.
    """
    typos = absence_claims_that_near_miss_a_bound_tool(
        _absence_claims(_probes()), available_tool_names()
    )
    assert not typos, (
        f"these absence claims are a near-miss of a tool this deployment binds: {typos}. A name "
        "the surface does not carry is not evidence the capability is missing — it is equally "
        "evidence the name was mistyped, and a mistyped claim can never fail."
    )


def test_a_near_miss_is_caught_on_the_marker_arm_as_well_as_the_bare_one() -> None:
    """A near miss is caught on the marker arm as well as the bare one.

    Same four-arm shape as the adjacent driven test.
    """
    surface = available_tool_names()
    bound = sorted(surface)[0]
    typo = bound + "s" if not bound.endswith("s") else bound[:-1]
    assert typo not in surface, "the constructed near-miss must not itself be a bound name"

    assert absence_claims_that_near_miss_a_bound_tool([("x", typo)], surface)
    assert absence_claims_that_near_miss_a_bound_tool(
        [("x", f"{ABSENT_MARKER}nothing like {typo} anywhere in this system")], surface
    )
    assert not absence_claims_that_near_miss_a_bound_tool(
        [("x", "no_such_tool_is_bound_anywhere")], surface
    )
    assert not absence_claims_that_near_miss_a_bound_tool(
        [("x", f"{ABSENT_MARKER}no equipment booking or instrument calendar interface")], surface
    )


def test_no_probe_names_one_tool_in_both_expects_tools_and_asserts_absent() -> None:
    """No probe names one tool in both `expects_tools` and `asserts_absent`.

    For a bound name the absence check already fails. For an unbound fleet tool under
    `needs_bundle:` both checks pass while the probe grades opposite behaviour in the two lanes. No
    surface is consulted, so this is lane-independent.
    """
    contradictory = sorted(
        f"{probe.id}: {sorted(set(probe.expects_tools) & set(probe.asserts_absent))}"
        for probe in _probes()
        if set(probe.expects_tools) & set(probe.asserts_absent)
    )
    assert not contradictory, (
        f"these probes name the same tool in `expects_tools` and in `asserts_absent`: "
        f"{contradictory}. One of the two is wrong — either the probe expects the capability or it "
        "asserts the capability is missing, and it cannot grade both."
    )
