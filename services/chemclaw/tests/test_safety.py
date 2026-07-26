"""Behavioral tests for structural hazard screening (D-080), all offline.

Three things must hold for an advisory safety screen to be worth having: the rules fire on real
examples of the motifs they name, they stay quiet on ordinary chemistry (a screen that cries wolf
is switched off), and nothing anywhere renders "no match" as "safe". The rule table is data, so
these tests pin its behavior with named molecules rather than mocking the matcher.
"""

import asyncio
from pathlib import Path

import pytest

from agents.safety_tools import screen_hazards
from chemclaw.config import settings
from kg.note import Note
from safety.notes import hazard_problems, structures_in
from safety.screen import SafetyRulesError, at_least, screen_reaction, screen_structure

# One textbook example per structural rule — the same molecules the eval case pins.
_HAZARDOUS = {
    "organic-azide": "CCCN=[N+]=[N-]",  # 1-azidopropane
    "inorganic-azide": "[Na+].[N-]=[N+]=[N-]",  # sodium azide
    "acyl-azide": "CC(=O)N=[N+]=[N-]",  # acetyl azide
    "diazo": "CC(=[N+]=[N-])C(=O)OC",  # methyl diazoacetate
    "diazonium": "c1ccccc1[N+]#N",  # benzenediazonium
    "peroxide": "CC(C)(C)OOC(C)(C)C",  # di-tert-butyl peroxide
    "nitrate-ester": "CCO[N+](=O)[O-]",  # ethyl nitrate
    "polynitro-aromatic": "O=[N+]([O-])c1ccccc1[N+](=O)[O-]",  # 1,2-dinitrobenzene
    "perchlorate": "OCl(=O)(=O)=O",  # perchloric acid
    "hydrazine": "NN",
    "n-halamine": "ClN1C(=O)CCC1=O",  # N-chlorosuccinimide
}

# Everyday process chemistry that must raise nothing: the false-positive side of the screen.
_BENIGN = [
    "CCO",  # ethanol
    "CC(=O)O",  # acetic acid
    "CCOC(C)=O",  # ethyl acetate
    "c1ccccc1",  # benzene
    "CC(=O)Oc1ccccc1C(=O)O",  # aspirin
    "O=[N+]([O-])c1ccccc1",  # nitrobenzene — one nitro group is not the polynitro motif
    "CC(=O)NN",  # acetohydrazide — an acylated N-N, not free hydrazine
    "CC#N",  # acetonitrile
    "ClCCl",  # dichloromethane
    "OC(=O)c1ccccc1",  # benzoic acid
]


@pytest.mark.parametrize(("rule_id", "smiles"), sorted(_HAZARDOUS.items()))
def test_each_rule_fires_on_its_reference_molecule(rule_id: str, smiles: str) -> None:
    """Every committed rule matches a textbook example of the motif it claims to detect.

    A SMARTS that stops matching fails *silently* — the screen just reports nothing, which reads
    as "no hazard" — so each rule is pinned to a molecule by name.
    """
    result = screen_structure(smiles)
    assert rule_id in {flag.rule_id for flag in result.flags}


def test_organic_and_inorganic_azide_rules_are_disjoint() -> None:
    """`organic-azide` and `inorganic-azide` never both fire on the same structure.

    Regression guard for a live e2e finding: the bare azide anion (as in sodium azide) went
    unflagged entirely, because `organic-azide`'s SMARTS requires the azide's terminal nitrogen
    to be attached to a carbon. The new `inorganic-azide` rule is written to match only the free
    ionic form (both terminal nitrogens unsubstituted) so it complements rather than duplicates
    the existing rule.
    """
    organic = screen_structure(_HAZARDOUS["organic-azide"])
    inorganic = screen_structure(_HAZARDOUS["inorganic-azide"])
    assert {f.rule_id for f in organic.flags} == {"organic-azide"}
    assert {f.rule_id for f in inorganic.flags} == {"inorganic-azide"}


@pytest.mark.parametrize("smiles", _BENIGN)
def test_ordinary_chemistry_raises_no_flag(smiles: str) -> None:
    """Common solvents, reagents and products stay quiet — a screen that cries wolf is ignored."""
    assert screen_structure(smiles).flags == []


def test_a_flag_carries_its_explanation_and_citation() -> None:
    """A flag must be actionable and traceable: severity, why it matters, and a source."""
    flag = screen_structure(_HAZARDOUS["organic-azide"]).flags[0]
    assert flag.severity == "high"
    assert "azide" in flag.explanation.lower()
    assert flag.citation  # every rule rests on a citable source, like every graph claim
    assert flag.matched == _HAZARDOUS["organic-azide"]


def test_an_empty_result_never_says_safe() -> None:
    """The no-match verdict states what was actually checked, never that the chemistry is safe.

    An over-trusted screen is more dangerous than no screen: it converts an absence of knowledge
    into apparent assurance.
    """
    verdict = screen_structure("CCO").verdict.lower()
    assert "no rule" in verdict  # says what was actually checked
    assert "not a safety assessment" in verdict  # and what it is not
    # No phrasing a reader could take as a clearance.
    assert not any(claim in verdict for claim in ("is safe", "no hazard", "safe to"))


def test_incompatible_pair_is_only_visible_at_reaction_level() -> None:
    """An oxidizer and a reducing agent are unremarkable alone and flagged together.

    This is the whole reason `screen_reaction` exists: no per-molecule screen can see it.
    """
    permanganate = "[K+].[O-][Mn](=O)(=O)=O"
    hydride = "[Li+].[AlH4-]"
    assert screen_structure(permanganate).flags == []
    assert screen_structure(hydride).flags == []
    pair = screen_reaction([permanganate, hydride, "CCO"])
    assert [flag.rule_id for flag in pair.flags] == ["oxidizer-with-reductant"]
    assert "+" in pair.flags[0].matched  # names both species, so the chemist sees the combination


def test_flags_are_ordered_worst_first() -> None:
    """The most serious flag leads, so a reader who stops after one line reads the right one."""
    result = screen_reaction(["NN", _HAZARDOUS["organic-azide"]])  # medium + high
    assert [flag.severity for flag in result.flags] == ["high", "medium"]
    assert result.max_severity == "high"


def test_unparseable_smiles_is_a_clear_error() -> None:
    """A bad structure is an error, not an empty (reassuring) result (G4)."""
    with pytest.raises(SafetyRulesError, match="unparseable SMILES"):
        screen_structure("not-a-molecule(((")


def test_missing_rule_table_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing table stops the screen instead of silently reporting no hazards.

    Screening with half a rule table would report "no rule matched" for a hazard the table
    covers — the exact failure this module exists to prevent, so it is fatal, not skipped.
    """
    monkeypatch.setattr(settings, "safety_rules_path", "safety/does-not-exist.yaml")
    with pytest.raises(SafetyRulesError, match="cannot read hazard rules"):
        screen_structure("CCO")


def test_malformed_rule_table_names_the_broken_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable SMARTS names the rule that owns it, so the table is fixable."""
    table = tmp_path / "rules.yaml"
    table.write_text(
        "structural:\n"
        "  - id: broken-rule\n"
        '    smarts: "[not-a-smarts"\n'
        "    severity: high\n"
        "    explanation: x\n"
        "    citation: y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "safety_rules_path", str(table))
    with pytest.raises(SafetyRulesError, match="broken-rule"):
        screen_structure("CCO")


def test_severity_comparison() -> None:
    """`at_least` is the one place "at or above the gate" is decided (no drift between callers)."""
    assert at_least("high", "medium") and at_least("medium", "medium")
    assert not at_least("low", "medium")
    assert not at_least(None, "low")  # nothing matched is never "at or above" anything


# --- the agent tool -------------------------------------------------------------------


def test_tool_screens_one_molecule_and_a_reaction() -> None:
    """The tool screens a single structure alone and a component list as a reaction."""
    single = asyncio.run(screen_hazards([_HAZARDOUS["peroxide"]]))
    assert [flag.rule_id for flag in single.flags] == ["peroxide"]
    reaction = asyncio.run(screen_hazards(["[K+].[O-][Mn](=O)(=O)=O", "[Li+].[AlH4-]"]))
    assert [flag.rule_id for flag in reaction.flags] == ["oxidizer-with-reductant"]


def test_tool_is_registered_for_the_agent() -> None:
    """The tool is advertised to the model — an unregistered screen would never be called."""
    from agents.tool_registry import registered_tool_names

    assert "screen_hazards" in registered_tool_names()  # registered on import (top of this file)


# --- the kg-validate gate -------------------------------------------------------------


def _procedure_note(body_extra: str = "") -> Note:
    """An agent-proposed note with a procedure that uses an azide."""
    body = (
        f"Reaction `CCCN=[N+]=[N-].CCO>>CCOCCC` from ELN entry x.\n\n"
        f"## Procedure\n\n1. Add the azide to ethanol.\n{body_extra}"
    )
    return Note(id="reaction-x", type="reaction", created_by="agent", body=body)


def test_agent_procedure_with_a_hazard_must_document_it() -> None:
    """A flagged agent-proposed procedure without a Hazards section fails the graph gate.

    This is what makes the screen matter: the warning reaches the human reviewing the PR, rather
    than a log nobody reads.
    """
    problems = hazard_problems(_procedure_note())
    assert len(problems) == 1
    assert "organic-azide" in problems[0] and "## Hazards" in problems[0]


def test_documented_hazards_pass_the_gate() -> None:
    """With the section present the note passes — the gate asks for disclosure, not for silence."""
    documented = _procedure_note(
        "\n## Hazards\n\nOrganic azide: energetic; do not isolate neat (Bräse 2005).\n"
    )
    assert hazard_problems(documented) == []


def test_the_gate_is_scoped_to_agent_proposed_procedures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Human notes, non-procedure notes, and a disabled gate are all left alone.

    A gate that fires on the wrong notes is a gate somebody turns off.
    """
    human = _procedure_note()
    assert hazard_problems(human.model_copy(update={"created_by": "human"})) == []
    record = Note(
        id="reaction-y",
        type="reaction",
        created_by="agent",
        body="Reaction `CCCN=[N+]=[N-]>>CCC` from ELN entry y.\n",  # no procedure section
    )
    assert hazard_problems(record) == []
    monkeypatch.setattr(settings, "safety_gate_enabled", False)
    assert hazard_problems(_procedure_note()) == []


def test_a_benign_procedure_is_not_gated() -> None:
    """An ordinary esterification procedure needs no hazards section."""
    benign = Note(
        id="reaction-z",
        type="reaction",
        created_by="agent",
        body="Reaction `CCO.CC(=O)O>>CCOC(C)=O` from ELN entry z.\n\n## Procedure\n\n1. Reflux.\n",
    )
    assert hazard_problems(benign) == []


def test_structures_are_read_from_smiles_and_code_spans() -> None:
    """Structures come from `compound_smiles` and inline code spans; prose noise is ignored."""
    note = Note(
        id="reaction-w",
        type="reaction",
        compound_smiles="CCO",
        body="Ran `CCO.CC(=O)O>>CCOC(C)=O` at `80 °C` for `2 h`, see `docs/runbook.md`.\n",
    )
    found = structures_in(note)
    assert "CCO" in found and "CCOC(C)=O" in found  # reaction SMILES split into components
    assert "docs/runbook.md" not in found  # RDKit is the arbiter of what is a structure


def test_broken_rule_table_blocks_the_gate_instead_of_crashing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rules failure is reported as a problem, so kg-validate blocks the PR cleanly."""
    monkeypatch.setattr(settings, "safety_rules_path", "safety/does-not-exist.yaml")
    problems = hazard_problems(_procedure_note())
    assert len(problems) == 1 and "hazard screening failed" in problems[0]
