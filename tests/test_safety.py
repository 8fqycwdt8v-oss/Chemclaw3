"""Behavioral tests for structural hazard screening (D-080), all offline.

Three things must hold for an advisory safety screen to be worth having: the rules fire on real
examples of the motifs they name, they stay quiet on ordinary chemistry (a screen that cries
wolf is switched off), and nothing anywhere renders "no match" as "safe". The rule table is
data, so these tests pin its behavior with named molecules rather than mocking the matcher.

The last two sections cover the package's other two cited tables, which answer *different*
questions and must keep answering them separately: the genotoxicity structural alerts
(`science/safety/genotox.py`) and the transcribed ICH Q3C/Q3D limits
(`science/safety/ich.py`). Both are here rather than in modules of their own because the
property that matters most about them is how they relate to the hazard screen — that a
genotoxicity alert is not a process-safety flag, and that neither is a classification — and
that is only assertable with all three in one place.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from chemclaw.connectors.safety.server.tools import screen_hazards
from chemclaw.core.config import settings
from chemclaw.kg.note import Note
from chemclaw.science.safety.genotox import screen_genotoxic_alerts
from chemclaw.science.safety.ich import impurity_limit
from chemclaw.science.safety.notes import hazard_problems, structures_in
from chemclaw.science.safety.screen import (
    SafetyRulesError,
    at_least,
    screen_reaction,
    screen_structure,
)

# One textbook example per structural rule — the same molecules the eval case pins.
_HAZARDOUS = {
    "organic-azide": "CCCN=[N+]=[N-]",  # 1-azidopropane
    "non-carbon-azide": "[Na+].[N-]=[N+]=[N-]",  # sodium azide
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

# The polynitroarenes, one per substitution pattern. Deliberately more than the one reference
# molecule `_HAZARDOUS` holds — see `test_polynitroarenes_flag_at_every_substitution_pattern`.
_POLYNITRO = {
    "1,2-dinitrobenzene": "O=[N+]([O-])c1ccccc1[N+](=O)[O-]",
    "1,3-dinitrobenzene": "O=[N+]([O-])c1cccc([N+](=O)[O-])c1",
    "1,4-dinitrobenzene": "O=[N+]([O-])c1ccc([N+](=O)[O-])cc1",
    "TNT": "Cc1c(cc(cc1[N+](=O)[O-])[N+](=O)[O-])[N+](=O)[O-]",
    "picric acid": "Oc1c(cc(cc1[N+](=O)[O-])[N+](=O)[O-])[N+](=O)[O-]",
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


@pytest.mark.parametrize(("name", "smiles"), sorted(_POLYNITRO.items()))
def test_polynitroarenes_flag_at_every_substitution_pattern(name: str, smiles: str) -> None:
    """TNT and picric acid must flag, not only the ortho isomer the old pattern happened to match.

    `polynitro-aromatic` shipped as a written six-atom ring chain
    (`[nitro]c1ccccc1[nitro]`), which hangs the second nitro group off the ring-closure atom and
    therefore matches **ortho only**. TNT, picric acid and both the meta and para dinitrobenzenes
    screened clean, and no other rule caught them: the screen answered "no rule in the hazard
    table matched" about high explosives, on the MCP tool and on the `kg-validate` hazard gate,
    which consequently demanded no `## Hazards` section for an agent-authored nitration.

    **The interesting part is why a green test suite allowed it.** D-080's discipline is one
    reference molecule per rule, and both this file and `data/evals/cases/hazard-rule-recall.md`
    picked 1,2-dinitrobenzene — the single arrangement the broken pattern *did* match. One example
    is a complete test of a rule that names a motif, and a blind one for a rule whose own words say
    "multiple" or "on one ring": those semantics are a count and a set of relative positions, so
    the discipline has to be one molecule per arrangement claimed. Hence this table, and the
    matching widening of the eval case.
    """
    assert "polynitro-aromatic" in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_mononitroarene_is_not_polynitro() -> None:
    """A count of two: one nitro group on a ring must not fire the polynitro rule.

    Pinned separately from the benign list because it is what makes the count real: `min_matches`
    wired up as `>= 1` would satisfy every match assertion above while turning the archetypal
    explosive alert into "contains a nitro group", and a flag that fires on nitrobenzene is a flag
    people learn to scroll past.
    """
    flags = {flag.rule_id for flag in screen_structure("O=[N+]([O-])c1ccccc1").flags}
    assert "polynitro-aromatic" not in flags


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


@pytest.mark.parametrize(
    ("smiles", "reagent"),
    [
        ("[N-]=[N+]=[N-]", "bare azide anion"),
        ("[Na+].[N-]=[N+]=[N-]", "sodium azide"),
        ("[K+].[N-]=[N+]=[N-]", "potassium azide"),
        ("[NH4+].[N-]=[N+]=[N-]", "ammonium azide"),
        ("N=[N+]=[N-]", "hydrazoic acid"),
        ("C[Si](C)(C)N=[N+]=[N-]", "trimethylsilyl azide"),
        ("O=P(OC1=CC=CC=C1)(OC1=CC=CC=C1)N=[N+]=[N-]", "diphenylphosphoryl azide"),
    ],
)
def test_azide_not_bonded_to_carbon_is_flagged(smiles: str, reagent: str) -> None:
    """Every azide that is not carbon-bound flags, not just the organic ones.

    Sodium azide is one of the most-reached-for reagents in the building, and it screened *clean*:
    `organic-azide` and `acyl-azide` both open on `[#6]`, so a salt matched neither and the screen
    reported nothing — which a reader takes as "no hazard found" on a compound that is acutely
    toxic and liberates explosive HN3 on contact with acid. The same hole swallowed hydrazoic
    acid and the silyl/phosphoryl azide transfer reagents, so each is pinned here by name.
    """
    flags = {flag.rule_id for flag in screen_structure(smiles).flags}
    assert "non-carbon-azide" in flags, f"{reagent} screened clean"


@pytest.mark.parametrize("smiles", ["CCCN=[N+]=[N-]", "CC(=O)N=[N+]=[N-]"])
def test_carbon_bound_azides_do_not_also_fire_the_non_carbon_rule(smiles: str) -> None:
    """The new rule stays off carbon-bound azides — two flags for one motif is noise, not safety."""
    flags = {flag.rule_id for flag in screen_structure(smiles).flags}
    assert "non-carbon-azide" not in flags
    assert flags & {"organic-azide", "acyl-azide"}  # still caught by the rule that owns them


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


def test_tool_is_advertised_to_the_agent() -> None:
    """The screen is on the agent's surface — one nothing advertises would never be called.

    It lives behind the `safety` connector now, so the check is against the connector surface
    rather than the in-process registry. That the *bundle* declares it is what puts it in front
    of the model; that the *server* serves exactly that name is `test_connector_transport.py`'s
    job.
    """
    from chemclaw.connectors.registry import connector_tool_names

    assert "screen_hazards" in connector_tool_names()


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
        body="Ran `CCO.CC(=O)O>>CCOC(C)=O` at `80 °C` for `2 h`, see `docs/guides/runbook.md`.\n",
    )
    found = structures_in(note)
    assert "CCO" in found and "CCOC(C)=O" in found  # reaction SMILES split into components
    assert "docs/guides/runbook.md" not in found  # RDKit is the arbiter of what is a structure


def test_broken_rule_table_blocks_the_gate_instead_of_crashing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rules failure is reported as a problem, so kg-validate blocks the PR cleanly."""
    monkeypatch.setattr(settings, "safety_rules_path", "safety/does-not-exist.yaml")
    problems = hazard_problems(_procedure_note())
    assert len(problems) == 1 and "hazard screening failed" in problems[0]


def _real_bo_candidate() -> Note:
    """A `bo-candidate` built by the **real** writer, `connectors/bo/knowledge.py`.

    Hand-writing this fixture is what let the gate ship blind. The previous version backticked its
    parameter values, so it exercised `structures_in` against markdown no producer emitted; the
    actual writer emitted `- molecule: CCN=[N+]=[N-]` as plain prose and never set
    `compound_smiles`, so `structures_in` returned `[]` for **every** machine-minted candidate and
    `hazard_problems` passed all of them. A fixture that hand-writes what the producer is supposed
    to produce cannot catch a producer that does not produce it, which is precisely why a defect in
    the one note type the gate's own docstring names as "never screened" survived a suite that
    appears to cover it.

    Both ways a campaign can name a molecule are exercised, because they reach the body by
    different routes: `molecule` is a library-style categorical whose levels are SMILES, `ligand`
    is a featurized categorical carrying a label → SMILES `structures` map.
    """
    from chemclaw.connectors.bo.knowledge import note_from_campaign_result
    from chemclaw.science.bo.problem import (
        CampaignResult,
        CategoricalParameter,
        ContinuousParameter,
        Objective,
        Observation,
        OptimizationProblem,
    )

    problem = OptimizationProblem(
        parameters=[
            CategoricalParameter(name="molecule", categories=["CCCN=[N+]=[N-]", "CCCCO"]),
            CategoricalParameter(
                name="ligand",
                categories=["L1", "L2"],
                structures={"L1": "CCO", "L2": "CCOCC"},
            ),
            ContinuousParameter(name="temperature", lower=20.0, upper=100.0),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )
    best = Observation(
        params={"molecule": "CCCN=[N+]=[N-]", "ligand": "L1", "temperature": 80.0},
        value=61.0,
        provenance="predicted",
        surrogate_sd=3.0,
    )
    return note_from_campaign_result(
        "azide_yield", problem, CampaignResult(best=best, history=[best])
    )


def _agent_proposal_note() -> Note:
    """An `experiment-proposal`: the agent's own free-form proposal of conditions (D-162).

    Hand-written on purpose, unlike the `bo-candidate` above: this type has no dedicated writer —
    the agent drafts the body itself through `propose_knowledge_note` — so prose with the
    structures in code spans *is* the real shape.
    """
    body = (
        "Proposed next run for the azide coupling, argued from reactions x and y.\n\n"
        "- azide source: `CCCN=[N+]=[N-]`\n"
        "- solvent: `CCO`\n"
        "- temperature: 80\n"
    )
    return Note(
        id="experiment-proposal-x", type="experiment-proposal", created_by="agent", body=body
    )


def test_the_real_writer_emits_structures_the_gate_can_see() -> None:
    """The producer's own output must carry its molecules, in the form the extractor reads.

    The gate reads `compound_smiles` and inline code spans. A `bo-candidate` supplied neither, so
    every recommendation a surrogate made — the one note type proposing an experiment nobody has
    run — reached a reviewer with no hazard screening at all, while `screen_reaction` on the very
    same SMILES returned a high-severity organic-azide flag. Asserted on the real note rather than
    on a fixture, since the mismatch between the two is the whole defect.
    """
    found = structures_in(_real_bo_candidate())
    assert "CCCN=[N+]=[N-]" in found  # a library-style level: the SMILES is the category label
    assert "CCO" in found  # a featurized level: the SMILES comes from the `structures` map


@pytest.mark.parametrize(
    ("note_type", "build"),
    [("bo-candidate", _real_bo_candidate), ("experiment-proposal", _agent_proposal_note)],
)
def test_a_proposal_of_conditions_is_screened_without_a_procedure_heading(
    note_type: str, build: Callable[[], Note]
) -> None:
    """The note class that proposes work nobody has run was the one class never screened.

    A `bo-candidate` names conditions a surrogate wants a human to physically run, and it has no
    `## Procedure` heading because it is a parameter table. Under the heading-only gate it passed
    unscreened — the exact inversion of what the gate is for, since here there is no chemist who
    has already stood at the bench and formed their own judgment about the mixture.
    """
    problems = hazard_problems(build())
    assert len(problems) == 1, note_type
    assert "organic-azide" in problems[0] and "## Hazards" in problems[0]


def test_a_documented_proposal_passes_like_any_other_note() -> None:
    """Widening which notes are screened must not change what the gate asks of them."""
    candidate = _real_bo_candidate()
    documented = candidate.model_copy(
        update={
            "body": candidate.body
            + "\n## Hazards\n\nOrganic azide: energetic; do not isolate neat.\n"
        }
    )
    assert hazard_problems(documented) == []


def test_an_ordinary_note_that_merely_names_a_structure_is_still_out_of_scope() -> None:
    """The scoping that keeps the gate credible: it fires on instructions, not on mentions.

    A gate that fired on every note naming a hazardous molecule would be switched off, which is
    the failure mode the module's own docstring names.
    """
    mention = Note(
        id="playbook-x",
        type="playbook",
        created_by="agent",
        body="Azide couplings (`CCCN=[N+]=[N-]`) recur across two projects.",
    )
    assert hazard_problems(mention) == []


def test_a_clean_screen_carries_its_disclaimer_into_the_serialized_result() -> None:
    """The "not a safety assessment" line must survive `model_dump()`, not just exist.

    A bare `property` is dropped by pydantic serialization, so a clean screen reached the model as
    `{"flags": []}` and the caveat never entered the context window the answer was written from.
    Asserting on the dumped payload — not on the attribute — is the whole point: reading
    `result.verdict` in a test passes either way.
    """
    dumped = screen_structure("CCO").model_dump()
    assert "verdict" in dumped, "verdict is not serialized; a clean screen reads as an empty result"
    assert "not a safety assessment" in dumped["verdict"]
    assert "safe" not in dumped["verdict"].lower().replace("safety", "")


def test_a_flagged_screen_serializes_an_advisory_verdict_too() -> None:
    """The matched case must say advisory-only in the payload, for the same reason."""
    dumped = screen_structure("CC(=O)OOC(C)=O").model_dump()
    assert dumped["flags"], "diacetyl peroxide must raise the peroxide rule"
    assert "Advisory only" in dumped["verdict"]


# --- the four blind spots the 2026-08-02 live run confirmed (REV-4) ---------------------

# Each molecule screened clean before its rule was widened, and each is an ordinary bench
# reagent for the hazard class its rule is named after. Kept as (name, SMILES, rule) so a
# future narrowing of any pattern names the compound it would silence.
_PREVIOUSLY_SILENT = [
    ("sodium peroxide", "[O-][O-].[Na+].[Na+]", "peroxide"),
    ("1,1-dimethylhydrazine (UDMH)", "CN(C)N", "hydrazine"),
    ("chloramine-T", "CC1=CC=C(C=C1)S(=O)(=O)[N-]Cl.[Na+]", "n-halamine"),
]


@pytest.mark.parametrize(("name", "smiles", "rule"), _PREVIOUSLY_SILENT)
def test_a_previously_silent_hazard_now_fires(name: str, smiles: str, rule: str) -> None:
    """A textbook member of a covered hazard class must not screen clean.

    Sodium peroxide writes its oxygens as one-coordinate anions, UDMH carries H on only one
    nitrogen, and chloramine-T's nitrogen is anionic and two-coordinate — each fell outside a
    pattern written for the neutral, fully-substituted case. A silent rule is the one failure
    mode this module exists to prevent, because the screen reports it as "nothing matched".
    """
    assert rule in {flag.rule_id for flag in screen_structure(smiles).flags}, name


def test_a_complex_hydride_fires_against_a_vicinal_dichloride_too() -> None:
    """1,2-dichloroethane carries the same incompatibility as DCM and was silent.

    The pair rule matched geminal dichlorides only, so an ordinary process solvent paired with
    LiAlH4 raised nothing.
    """
    flags = screen_reaction(["[Li+].[AlH4-]", "ClCCCl"]).flags
    assert "complex-hydride-with-chlorinated-solvent" in {f.rule_id for f in flags}


@pytest.mark.parametrize(
    ("name", "smiles"),
    [
        # Widening a hazard rule until it fires on everything is worse than the gap it closed:
        # a rule that flags a routine reagent teaches a chemist to skip reading the flags.
        ("1-chlorobutane", "CCCCCl"),
        ("benzyl chloride", "ClCc1ccccc1"),
        ("acetyl chloride", "CC(=O)Cl"),
        ("epichlorohydrin", "ClCC1CO1"),
        ("2-chloroethanol", "OCCCl"),
        ("aniline", "Nc1ccccc1"),
        ("acetohydrazide", "CC(=O)NN"),
        ("azobenzene", "c1ccc(cc1)/N=N/c1ccccc1"),
        ("ethylene glycol", "OCCO"),
        ("1,4-dioxane", "C1COCCO1"),
        ("ethyl acetate", "CCOC(C)=O"),
        ("4-chloroanisole", "COc1ccc(Cl)cc1"),
    ],
)
def test_widening_a_rule_did_not_make_a_routine_reagent_hazardous(name: str, smiles: str) -> None:
    """None of the widened patterns may fire on an everyday, unremarkable reagent."""
    widened = {"peroxide", "hydrazine", "n-halamine"}
    assert widened.isdisjoint({f.rule_id for f in screen_structure(smiles).flags}), name


# --- the genotoxicity alert table -----------------------------------------------------


# One published example per alert, plus the molecule each alert must *not* fire on. The
# negative half is what keeps a widened pattern from turning the list into noise: a table
# that flags every cross-coupling is a table a chemist stops reading.
_ALERTS = {
    "n-nitroso": ("CN(C)N=O", "CN(C)C=O"),  # NDMA vs DMF
    "aromatic-nitro": ("O=[N+]([O-])c1ccccc1", "C[N+](=O)[O-]"),  # nitrobenzene vs nitromethane
    "primary-aromatic-amine": ("Nc1ccccc1", "CC(=O)Nc1ccccc1"),  # aniline vs acetanilide
    "aromatic-azo": ("c1ccccc1N=Nc1ccccc1", "CCN=NCC"),  # azobenzene vs an aliphatic azo
    "epoxide": ("C1CO1", "C1CCOC1"),  # ethylene oxide vs THF
    "aziridine": ("C1CN1", "C1CCNC1"),  # aziridine vs pyrrolidine
    "alkyl-halide": ("CI", "CC(C)(C)Cl"),  # methyl iodide vs a tertiary chloride
    "alkyl-sulfonate-or-sulfate-ester": ("COS(C)(=O)=O", "CS(=O)(=O)O"),  # MeOMs vs MsOH
    "michael-acceptor": ("NC(=O)C=C", "CCC(N)=O"),  # acrylamide vs propionamide
}


@pytest.mark.parametrize(("alert_id", "pair"), sorted(_ALERTS.items()))
def test_each_alert_fires_on_its_example_and_stays_quiet_on_its_counterexample(
    alert_id: str, pair: tuple[str, str]
) -> None:
    """Every alert matches a published example of its motif and not the near miss beside it."""
    hit, miss = pair
    assert alert_id in {a.alert_id for a in screen_genotoxic_alerts([hit]).alerts}
    assert alert_id not in {a.alert_id for a in screen_genotoxic_alerts([miss]).alerts}


def test_a_nitrosating_agent_meeting_an_amine_flags_the_formation_route() -> None:
    """The nitrosamine question the run fabricated: an amine plus a nitrosating agent.

    Neither component is an alert on its own — DIPEA is an everyday base and sodium nitrite is an
    everyday reagent — so this is only visible across a component list, which is why it is a pair
    rule rather than a structural one.
    """
    together = screen_genotoxic_alerts(["CCN(C(C)C)C(C)C", "[Na+].[O-]N=O"])
    assert [a.alert_id for a in together.alerts] == ["nitrosatable-amine-with-nitrosating-agent"]
    assert screen_genotoxic_alerts(["CCN(C(C)C)C(C)C"]).alerts == []
    assert screen_genotoxic_alerts(["[Na+].[O-]N=O"]).alerts == []


def test_an_amide_is_not_treated_as_a_nitrosatable_amine() -> None:
    """DMF with sodium nitrite must stay quiet — an amide nitrogen is not the risk motif.

    The pair rule's value depends on it firing where nitrosation is plausible. Matching every
    nitrogen would fire on most reactions in the corpus and be ignored within a week.
    """
    assert screen_genotoxic_alerts(["CN(C)C=O", "[Na+].[O-]N=O"]).alerts == []


def test_every_alert_carries_a_citation_and_the_motif_it_names() -> None:
    """A flag a chemist cannot trace is a flag they must take on trust — which is the failure."""
    for alert in screen_genotoxic_alerts(["CN(C)N=O", "Nc1ccccc1"]).alerts:
        assert alert.citation.strip() and alert.motif.strip()
        assert alert.explanation.strip()


@pytest.mark.parametrize("smiles", [["CN(C)N=O"], ["CCO"]])
def test_the_result_says_a_flag_is_an_alert_and_not_a_classification(smiles: list[str]) -> None:
    """The disclaimer rides in the payload, on a hit *and* on a miss, not only in a docstring.

    `ScreenResult.verdict` was made a `computed_field` for exactly this reason: a plain property
    is not serialized, so the caveat never reached the model that had to write the answer. The
    four things this system cannot produce are named individually, because "expert assessment
    required" on its own did not stop the live run inventing an ICH M7 class and a worked purge
    factor.
    """
    rendered = screen_genotoxic_alerts(smiles).model_dump()
    verdict = rendered["verdict"]
    assert "ICH M7" in verdict and "purge factor" in verdict and "acceptable intake" in verdict
    assert "expert assessment" in verdict


def test_a_clean_alert_screen_is_not_reported_as_a_negative_prediction() -> None:
    """An empty result is nine patterns not matching, not a (Q)SAR calling the compound clean."""
    verdict = screen_genotoxic_alerts(["CCO"]).verdict
    assert "not a negative mutagenicity prediction" in verdict


def test_the_two_screens_stay_separate() -> None:
    """The genotoxicity table must not leak into the process-safety screen, or vice versa.

    This is the conflation the split exists to prevent, and it is testable in both directions.
    Nitrobenzene is the case that proves it: an ordinary reagent the hazard table is right to pass
    and the alert table is right to flag. Merging them would also make every nitration procedure
    trip `kg-validate`'s `## Hazards` gate, which is a regulatory question answered by a
    process-safety gate.
    """
    assert screen_structure("O=[N+]([O-])c1ccccc1").flags == []
    assert [a.alert_id for a in screen_genotoxic_alerts(["O=[N+]([O-])c1ccccc1"]).alerts] == [
        "aromatic-nitro"
    ]
    # And the other way: an organic azide is a process-safety flag with no genotoxicity alert.
    assert "organic-azide" in {f.rule_id for f in screen_structure("CCCN=[N+]=[N-]").flags}
    assert screen_genotoxic_alerts(["CCCN=[N+]=[N-]"]).alerts == []


def test_the_alert_screen_is_advertised_to_the_agent() -> None:
    """A tool nothing advertises would never be called — the gap this table exists to close."""
    from chemclaw.connectors.registry import connector_tool_names

    assert "screen_genotoxic_alerts" in connector_tool_names()


def test_an_unparseable_component_stops_the_alert_screen() -> None:
    """A component that cannot be parsed must not silently screen as "no alerts"."""
    with pytest.raises(SafetyRulesError, match="unparseable SMILES"):
        screen_genotoxic_alerts(["not-a-molecule"])


# --- the ICH Q3C / Q3D reference tables -----------------------------------------------


@pytest.mark.parametrize(
    ("query", "substance", "basis", "value", "unit"),
    [
        # The exact lookup the live run answered from training instead.
        ("Pd", "Palladium (Pd)", "oral PDE", 100.0, "µg/day"),
        ("palladium", "Palladium (Pd)", "parenteral PDE", 10.0, "µg/day"),
        ("THF", "Tetrahydrofuran", "PDE", 7.2, "mg/day"),
        ("tetrahydrofuran", "Tetrahydrofuran", "concentration limit", 720.0, "ppm"),
        ("C1CCOC1", "Tetrahydrofuran", "PDE", 7.2, "mg/day"),  # resolved from a structure
        ("DMF", "N,N-Dimethylformamide", "PDE", 8.8, "mg/day"),
        ("2-MeTHF", "2-Methyltetrahydrofuran", "PDE", 5.0, "mg/day"),
        ("benzene", "Benzene", "concentration limit", 2.0, "ppm"),  # Class 1: a limit, no PDE
        ("IPA", "2-Propanol", "PDE", 50.0, "mg/day"),  # Class 3, reached via an abbreviation
    ],
)
def test_a_transcribed_limit_comes_back_with_its_number(
    query: str, substance: str, basis: str, value: float, unit: str
) -> None:
    """The number is read off a committed table, and the same substance answers to every spelling.

    A SMILES and an abbreviation both resolve through the identity table, so a chemist does not
    have to know the guideline's own spelling to reach its row.
    """
    limit = impurity_limit(query).limit
    assert limit is not None and limit.substance == substance
    assert {(entry.basis, entry.value, entry.unit) for entry in limit.limits} >= {
        (basis, value, unit)
    }


def test_every_limit_names_the_guideline_its_revision_and_its_table() -> None:
    """A number without provenance is a recalled number wearing a citation's clothes.

    The whole point of transcribing these tables is that someone can open the source document at
    the right page; a citation naming only "ICH" would not let them.
    """
    solvent = impurity_limit("THF").limit
    element = impurity_limit("Pd").limit
    assert solvent is not None and element is not None
    assert solvent.citation == (
        "ICH Q3C(R9), Impurities: Guideline for Residual Solvents, ICH Step 4 (2024), Table 2"
    )
    assert element.citation == (
        "ICH Q3D(R2), Guideline for Elemental Impurities, ICH Step 4 (2022), Table A.2.1"
    )


def test_the_solvent_classes_are_carried_not_inferred() -> None:
    """Class membership is the other half of the Q3C answer, and no limit implies it."""
    assert impurity_limit("benzene").limit.limit_class == "Class 1"  # type: ignore[union-attr]
    assert impurity_limit("DCM").limit.limit_class == "Class 2"  # type: ignore[union-attr]
    assert impurity_limit("DMSO").limit.limit_class == "Class 3"  # type: ignore[union-attr]


@pytest.mark.parametrize("query", ["nickel", "Ni", "tert-butyl alcohol", "water", "unobtainium"])
def test_a_miss_is_a_miss_and_says_what_it_does_not_mean(query: str) -> None:
    """The load-bearing half: an untranscribed substance returns nothing, and explains the nothing.

    Nickel and `tert`-butyl alcohol are the sharp cases — both are genuinely in a guideline, and
    both were left out of the transcription because their values could not be verified against the
    source. A miss that read as "no limit exists" would be worse than the fabrication this replaces.
    """
    lookup = impurity_limit(query)
    assert lookup.limit is None
    assert "not that no limit exists" in lookup.verdict
    assert "do not state one from memory" in lookup.verdict


def test_the_miss_verdict_is_serialized_not_merely_a_property() -> None:
    """The sentence has to reach the model writing the answer, which reads the payload."""
    assert "not that no limit exists" in impurity_limit("unobtainium").model_dump()["verdict"]


def test_the_class_2_concentration_limits_agree_with_their_pdes() -> None:
    """Every Q3C row satisfies the guideline's own ppm = PDE x 100 identity at a 10 g daily dose.

    A transcription's characteristic failure is a mistyped digit, and this is the one internal
    consistency check the table supports — it catches a transposed PDE or ppm without needing the
    source document open.
    """
    for name in ("acetonitrile", "DCM", "toluene", "NMP", "2-MeTHF", "DMSO", "ethyl acetate"):
        limit = impurity_limit(name).limit
        assert limit is not None, name
        by_basis = {entry.basis: entry.value for entry in limit.limits}
        assert by_basis["concentration limit"] == pytest.approx(by_basis["PDE"] * 100.0), name


def test_the_limit_lookup_is_advertised_to_the_agent() -> None:
    """Unadvertised, the model would go on reciting the number it already recites."""
    from chemclaw.connectors.registry import connector_tool_names

    assert "ich_impurity_limit" in connector_tool_names()


@pytest.mark.parametrize("abbreviation", ["EDC", "DMA", "TCE"])
def test_an_ambiguous_abbreviation_is_a_miss_not_a_confident_wrong_row(abbreviation: str) -> None:
    """Three abbreviations that name more than one substance must not resolve to either.

    `EDC` is ethylene dichloride in Q3C and the carbodiimide coupling reagent at the bench — and
    the second is in the identity table, so a chemist asking about their coupling reagent would
    have been handed a Class 1 limit of 5 ppm with a genuine ICH citation on it. A wrong row is
    worse than the miss it replaces precisely because the citation makes it checkable-looking.
    """
    assert impurity_limit(abbreviation).limit is None
