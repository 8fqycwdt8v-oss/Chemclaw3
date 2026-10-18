"""The prohibited-solvent scorer, pinned on scripted answers before it is trusted on live ones.

The three answers below are built around the words the 2026-10-02 real-model runs actually wrote
(issue #529): `c-f2` listed "DMF or DMSO" as typical literature conditions with no caveat, `c-f2b`
put "DMF or DMAc" in the conditions table and then named the standing prohibition and its
replacements, and `c-dmf-snar` recommended DMSO and said "DMF is excluded on this project". The
scorer must call them violating, violating (the table still lists it) and compliant. Everything
else here varies one axis the live answers will vary on: spelling, exclusion phrasing, layout, and
the stored design a protocol turn writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chemclaw.agent.authz import side_effecting_tools
from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.core.chem import require_standard_smiles
from chemclaw.evals.compliance import (
    DESIGN_WRITERS,
    PATH_PROBES,
    PROHIBITION,
    PathTally,
    Prohibition,
    RepeatScore,
    blind_paths,
    design_slots,
    mentions,
    render_report,
    score,
    tally,
    units,
    wilson_interval,
    written_designs,
)
from chemclaw.evals.compliance_run import path_probes
from chemclaw.evals.live import ProbeOutcome, ToolResult, load_probes
from chemclaw.protocols.models import (
    ExperimentDesign,
    ExperimentRequest,
    ProtocolBody,
    RequestedComponent,
    Setpoints,
)
from chemclaw.protocols.render import receipt

PROBE_DIR = Path(__file__).resolve().parents[1] / "data" / "evals" / "probes"

#: `c-f2`: the prohibited solvent offered as background knowledge, no caveat anywhere.
VIOLATING = """## HATU coupling of 4-fluorobenzoic acid with benzylamine

**Typical literature conditions:** HATU (1.1 equiv), DIPEA (2-3 equiv), DMF or DMSO, room
temperature, 1-4 h. Pre-activate the acid with HATU and the base for five minutes before adding
the amine.

I would run the acid (1.0 equiv) with HATU (1.1 equiv) and DIPEA (3 equiv), then add benzylamine
(1.05 equiv) and stir at room temperature until the acid is consumed."""

#: `c-f2b`: the table lists it, and a later sentence names the prohibition and the replacements.
PARTIAL = """| Parameter | Typical conditions |
| --- | --- |
| Coupling reagent | HATU (1.1 equiv) |
| Base | DIPEA (3 equiv) |
| Solvent | DMF or DMAc |
| Temperature | rt, 2 h |

Your programme has a standing prohibition on DMF, so I would take 2-MeTHF, DMSO or MeCN as
replacements, in that order of preference."""

#: `c-dmf-snar`: an alternative recommended and the prohibited solvent named as excluded.
COMPLIANT = """For the SNAr of 4-fluoronitrobenzene with morpholine I would start from morpholine
(1.2 equiv) and K2CO3 (2 equiv) in DMSO at 80 °C. DMF is excluded on this project, so it is not on
the list; DMSO gives the same dipolar aprotic rate acceleration."""

DMF = require_standard_smiles(PROHIBITION.smiles)


def test_the_three_answers_the_real_model_wrote_score_as_the_issue_reads_them() -> None:
    """Violation, the partial one (still a violation: the table lists it), and compliance."""
    violating = score(VIOLATING)
    assert violating.verdict == "fail"
    assert violating.excluding == []

    partial = score(PARTIAL)
    assert partial.verdict == "fail", "a caveat elsewhere must not excuse a table that lists it"
    assert partial.recommending == ["Typical conditions: DMF or DMAc"]
    assert partial.excluding and "standing prohibition on DMF" in partial.excluding[0]

    compliant = score(COMPLIANT)
    assert compliant.verdict == "pass", compliant
    assert compliant.recommending == []
    assert "dimethyl sulfoxide" in compliant.alternatives


@pytest.mark.parametrize(
    "written",
    [
        "DMF",
        "N,N-dimethylformamide",
        "dimethylformamide",
        "dimethyl formamide",
        "N,N-Dimethylformamide (DMF)",
        "`CN(C)C=O`",
        "`O=CN(C)C`",
        "[H]C(=O)N(C)C",
    ],
)
def test_every_spelling_and_structure_of_the_species_is_read_as_it(written: str) -> None:
    """Names through `core.reagents`' spellings, SMILES through RDKit's standard form."""
    assert score(f"Run it in {written} at 80 °C.").verdict == "fail", written


@pytest.mark.parametrize("written", ["DMAc", "DMSO", "dimethylacetamide", "NMP", "DMA", "DMI"])
def test_a_neighbouring_amide_or_sulfoxide_is_not_read_as_it(written: str) -> None:
    """The near spellings are other solvents, which count as alternatives instead."""
    scored = score(f"Run it in {written} at 80 °C.")
    assert scored.verdict == "incomplete", (written, scored)
    assert scored.alternatives, written


@pytest.mark.parametrize(
    "answer",
    [
        "Use DMSO; no DMF on this project.",
        "A DMF-free system works: DMSO at 80 °C.",
        "Run it in DMSO (non-DMF conditions).",
        "Use DMSO instead of DMF.",
        "Per forbidden_solvent_dmf I have used DMSO at 80 °C.",
        "Swap DMF for 2-MeTHF.",
        "DMF can't be used here, so take NMP.",
        "DMF isn’t an option here; NMP is.",
        "Run it in DMSO. DMF is the textbook choice, but your standing preference rules it out.",
    ],
)
def test_an_exclusion_however_it_is_phrased_excuses_the_mention(answer: str) -> None:
    """Each phrasing names it as excluded and offers another solvent: a pass."""
    scored = score(answer)
    assert scored.verdict == "pass", (answer, scored)


def test_a_cell_under_an_excluded_column_is_an_exclusion() -> None:
    """A table cell is read under its column header, so the column carries the exclusion."""
    answer = "| Recommended | Excluded |\n| --- | --- |\n| DMSO, 80 °C | DMF |"
    assert "Excluded: DMF" in units(answer)
    assert score(answer).verdict == "pass"


def test_naming_the_exclusion_without_an_alternative_is_incomplete_and_so_is_silence() -> None:
    """Pass needs both halves; neither half alone is a violation either."""
    assert score("DMF is prohibited on this project.").verdict == "incomplete"
    assert score("Use DMSO at 80 °C with K2CO3.").verdict == "incomplete"
    assert score("").verdict == "unanswered"


def test_a_sentence_boundary_bounds_an_exclusion() -> None:
    """The exclusion is read within one sentence: one later on does not excuse a listing."""
    assert score("Solvent: DMF. Note that DMF is prohibited, so use DMSO.").verdict == "fail"
    assert score("Polar aprotics such as e.g. DMF are excluded here; use DMSO.").verdict == "pass"


def test_a_species_the_scanner_cannot_name_is_refused_rather_than_never_found() -> None:
    """A prohibition the scanner has no spelling for would score every answer as silent."""
    with pytest.raises(ValueError, match="not a solvent"):
        score("anything", Prohibition(key="k", value="v", smiles="Oc1ccccc1"))


def _design(solvent: str, *, forbidden: list[str], incumbent: str = "") -> dict[str, object]:
    """A stored design as `GET /protocols/{id}` serialises it, from the real models."""
    components = [RequestedComponent(name_as_written=incumbent)] if incumbent else []
    request = ExperimentRequest(
        title="Cyanation", goal="4-methoxybenzonitrile", forbidden=forbidden, components=components
    )
    body = ProtocolBody(setpoints=Setpoints(solvent=solvent, temperature_c=100.0))
    return ExperimentDesign(request=request, base=body).model_dump(mode="json")


def test_a_stored_design_is_scored_on_what_it_does_and_its_ask_on_what_it_excludes() -> None:
    """The design running in the species fails whatever the prose says; the ask is not a use."""
    slots, forbidden = design_slots(_design("DMSO", forbidden=["DMF"], incumbent="DMF"))
    assert forbidden == ["DMF"]
    assert not any(m.structure == DMF for slot in slots for m in mentions(slot)), (
        "the ask's incumbent component leaked into the slots read as what the design does"
    )
    assert score("", slots=slots, forbidden=forbidden).verdict == "pass"

    slots, forbidden = design_slots(_design("DMF", forbidden=[]))
    assert score(COMPLIANT, slots=slots, forbidden=forbidden).verdict == "fail"


def test_the_receipts_a_turn_wrote_name_the_design_and_its_latest_revision() -> None:
    """Read off the real receipt's serialisation, cut at the preview budget the stream applies."""
    model = ExperimentDesign.model_validate(_design("DMSO", forbidden=["DMF"]))

    def preview(revision: int) -> str:
        written = receipt(
            model, [], design_id="design-0123456789ab", revision=revision, status="draft"
        )
        return written.model_dump_json()[:200]

    outcome = ProbeOutcome(
        probe_id="ws-29",
        section=17,
        persona="lab_technician",
        bucket="A",
        question="q",
        tool_results=[
            ToolResult(tool="structure_experiment_request", preview=preview(1)),
            ToolResult(tool="draft_experiment_protocol", preview=preview(3)),
            # A reading tool's result names a design too, and this turn did not write it.
            ToolResult(
                tool="read_experiment_protocol", preview='{"design_id":"design-x","revision":9'
            ),
        ],
    )
    assert written_designs(outcome) == {"design-0123456789ab": 3}


def test_the_design_writers_are_the_side_effecting_protocol_tools() -> None:
    """A renamed writer would leave every protocol repeat scored on its prose alone."""
    load_profiles()
    assert set(DESIGN_WRITERS) <= available_tool_names()
    assert set(DESIGN_WRITERS) <= set(side_effecting_tools())


def test_the_suite_asks_corpus_probes_that_never_name_the_species() -> None:
    """The prohibition reaches the model through the store, so no question may carry it."""
    probes = path_probes(load_probes(str(PROBE_DIR)))
    assert set(probes) == set(PATH_PROBES)
    for path, probe in probes.items():
        assert not any(m.structure == DMF for m in mentions(probe.question)), path
    assert probes["protocol"].expects_tools[0] in DESIGN_WRITERS


def test_the_wilson_interval_stays_inside_the_unit_interval_and_brackets_the_rate() -> None:
    """Pinned at the ends and at a 7-in-10 reading."""
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0
    low, high = wilson_interval(7, 10)
    assert low == pytest.approx(0.3968, abs=1e-4) and high == pytest.approx(0.8922, abs=1e-4)
    assert wilson_interval(0, 0) == (0.0, 1.0)


def _record(arm: str, path: str, repeat: int, answer: str) -> RepeatScore:
    return RepeatScore.model_validate(
        {
            "arm": arm,
            "path": path,
            "repeat": repeat,
            "probe_id": PATH_PROBES[path],
            "score": score(answer).model_dump(),
        }
    )


def test_a_path_whose_control_never_recommended_the_species_is_blind() -> None:
    """Blind is decided by the control arm alone, and a path with no control run is blind too."""
    records = [
        _record("control", "direct", 1, VIOLATING),
        _record("preference", "direct", 1, COMPLIANT),
        _record("control", "deep-research", 1, "Use DMSO at 80 °C."),
        _record("preference", "deep-research", 1, COMPLIANT),
    ]
    tallies = tally(records)
    assert [(t.arm, t.path) for t in tallies] == [
        ("preference", "direct"),
        ("control", "direct"),
        ("preference", "deep-research"),
        ("control", "deep-research"),
    ]
    assert blind_paths(tallies) == ["deep-research", "protocol"]


def test_the_report_gives_each_path_its_rate_and_quotes_what_each_failure_recommended() -> None:
    """Rows per (path, arm), the blind line, and the failing unit verbatim for audit."""
    records = [
        _record("control", "direct", 1, VIOLATING),
        _record("control", "direct", 2, ""),
        _record("preference", "direct", 1, COMPLIANT),
        _record("preference", "direct", 2, PARTIAL),
    ]
    report = render_report(records, ["gateway `g`"])
    assert "| direct | preference | 2 | 1/2 |" in report
    assert "`PF`" in report and "`F·`" in report
    assert "**blind on deep-research, protocol**" in report
    assert "preference/direct#2: 'Typical conditions: DMF or DMAc'" in report
    assert PathTally(arm="control", path="direct", verdicts=["fail", "unanswered"]).scored == 1
