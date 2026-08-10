"""Genotoxicity structural alerts — DNA-reactive motifs, never an ICH M7 classification.

Why this exists. `science/safety/screen.py` holds sixteen process-safety rules and not one word
about mutagenicity: grepping this package for ICH M7, Ames, TTC or nitrosamine returned nothing. In
a live run two questions that need exactly that — mutagenic-impurity alerts, and nitrosamine risk —
were answered by fabrication, one of them inventing acceptable-intake limits and a worked purge
factor. Alerts themselves are data with a long published history, so the gap was a missing table,
not a missing model.

**Why a second module and a second table rather than more rows in `rules.yaml`.** The two screens
answer different questions. `screen_hazards` answers "is this safe to run today", and its flags
gate an agent-proposed procedure note through the PR-gate. This one answers "will this need a
control strategy" — a regulatory-toxicology question whose controls are analytical and whose
audience is different. Conflating them is what turns a hazard screen into an ICH M7 verdict, which
is precisely the fabrication above. It would also break the process-safety screen: nitrobenzene is
an ordinary reagent that table is right to pass and this one is right to flag, and a shared flag
list would make every nitration procedure trip the `## Hazards` gate.

**The line the code enforces.** A structural alert is a motif and is encoded here. An ICH M7 class,
a purge factor and an acceptable-intake limit are outputs of a model this system does not have (two
complementary (Q)SARs plus an Ames corpus and expert review). So no alert carries any of the three,
and `AlertResult.verdict` states on *every* result — hit or miss — that a flag is an alert for
expert assessment and not a classification. That sentence lives in the payload rather than only in
a tool docstring for the reason `ScreenResult.verdict` documents: the payload is what is in the
context window when the answer gets written.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, computed_field
from rdkit import Chem

from chemclaw.science.safety.screen import (
    compile_smarts,
    parse_components,
    read_rule_table,
    require_screenable_size,
)

# The alert table ships beside this module. Not a setting, unlike `safety_rules_path`: a site
# extends the process-safety table with its own knowledge, while a published alert set is not
# something a deployment has its own version of — and a swapped-in alert table would change what
# the disclaimer below is attached to.
_ALERTS_PATH = str(Path(__file__).resolve().parent / "genotox_alerts.yaml")

# Repeated on every result, hit or miss. The exact four things the system cannot produce are named,
# because "expert assessment required" alone reads as a formality and did not stop the live run
# from producing a class and a purge factor anyway.
_NOT_A_CLASSIFICATION = (
    "An alert is a DNA-reactive structural motif requiring expert assessment. It is NOT an ICH M7 "
    "class, an acceptable intake, a purge factor, or a (Q)SAR prediction — this system has none of "
    "those and must not state one."
)


class GenotoxAlert(BaseModel):
    """One matched alert: which motif, why it is one, where the claim comes from, and what matched.

    No severity field, unlike `HazardFlag`. Ranking alerts would be the first half of a
    classification, and the published alert sets do not rank them either.
    """

    alert_id: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # A SMILES for a structural alert, or "a + b" for a formation pair.
    matched: str = Field(min_length=1)


class AlertResult(BaseModel):
    """The alerts raised for one molecule or route, in table order, and what was screened.

    `screened` mirrors `ScreenResult.screened` exactly — the canonical SMILES of every structure
    this result covers, deduplicated, in the order given — and it is here for the same two reasons
    and one more. A clean result otherwise names nothing it looked at, which is worst on precisely
    this result type (`verdict` spends three lines saying an empty list is not a negative
    mutagenicity prediction, about molecules the payload never identifies); and a surface that must
    render a genotox alert *distinctly* from a general hazard needs both results to key on the same
    entity, which they cannot do while each carries only the caller's own spelling.
    """

    alerts: list[GenotoxAlert] = Field(default_factory=list)
    screened: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """What a reader must be told about this result, present on a hit *and* on a miss.

        The empty case is the dangerous one: "no alerts" reads as "not mutagenic", which is a
        (Q)SAR conclusion drawn from a nine-row table. So the miss says what the absence actually
        means, in the same words the hit uses about what a flag does not mean.
        """
        if not self.alerts:
            return (
                "No structural alert in the table matched. That is not a negative mutagenicity "
                f"prediction and not an ICH M7 classification — the table is a short, cited alert "
                f"list, not a (Q)SAR. {_NOT_A_CLASSIFICATION}"
            )
        motifs = ", ".join(alert.motif for alert in self.alerts)
        return f"{len(self.alerts)} structural alert(s) matched ({motifs}). {_NOT_A_CLASSIFICATION}"


class _Alert(BaseModel):
    """One structural alert as loaded from the table."""

    id: str = Field(min_length=1)
    smarts: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class _FormationPair(BaseModel):
    """One alert about two components meeting, rather than about a structure that is present."""

    id: str = Field(min_length=1)
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    motif: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class _AlertTable(BaseModel):
    """The parsed alert file: structural motifs plus formation routes."""

    structural: list[_Alert] = Field(min_length=1)
    formation_pairs: list[_FormationPair] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _load_alerts() -> tuple[_AlertTable, dict[str, Chem.Mol]]:
    """Parse and compile the alert table once per process (it is a committed file).

    Patterns are keyed by `<alert id>` for structural alerts and `<pair id>:left` / `:right` for
    formation pairs, the same convention `screen.py` uses, so the two tables read alike.
    """
    table = read_rule_table(_ALERTS_PATH, _AlertTable)
    patterns = {alert.id: compile_smarts(alert.smarts, alert.id) for alert in table.structural}
    for pair in table.formation_pairs:
        patterns[f"{pair.id}:left"] = compile_smarts(pair.left, pair.id)
        patterns[f"{pair.id}:right"] = compile_smarts(pair.right, pair.id)
    return table, patterns


def screen_genotoxic_alerts(component_smiles: list[str]) -> AlertResult:
    """Match one molecule, or every component of a route, against the alert table.

    Pass the whole route rather than one step: the formation-pair alert can only see components
    given to it together, so a nitrosating agent introduced two steps later is structurally
    invisible to a per-step call.

    Args:
        component_smiles: One SMILES per species — the molecule alone, or every reactant, reagent,
            solvent and product whose meeting is being assessed.

    Raises:
        SafetyRulesError: a component does not parse in full — the refusal names the component's
            position in the list given, since a route is a list and "one of these is unusable" is
            not something a chemist can act on — the alert table is malformed, or the list is empty
            or longer than `safety_max_components`.
    """
    require_screenable_size(component_smiles, what="a genotoxicity screen")
    table, patterns = _load_alerts()
    # `parse_components` rather than a bare RDKit parse, and shared with the hazard screen for the
    # reason `require_screenable_size` is shared: RDKit reads `"CCO O=[N+]([O-])c1ccccc1"` as
    # ethanol and discards the nitroarene after the space, so this screen used to answer "no
    # structural alert matched" about a molecule the caller never named. On a result whose verdict
    # spends three lines explaining that an empty list is not a negative mutagenicity prediction,
    # being wrong about *which molecule* the list is empty for is the worse half of the sentence.
    molecules = parse_components(component_smiles)
    alerts = [
        GenotoxAlert(
            alert_id=alert.id,
            motif=alert.motif,
            explanation=alert.explanation,
            citation=alert.citation,
            matched=smiles,
        )
        for alert in table.structural
        for smiles, molecule in molecules.items()
        if molecule.HasSubstructMatch(patterns[alert.id])
    ]
    for pair in table.formation_pairs:
        left = [s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:left"])]
        right = [
            s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:right"])
        ]
        alerts.extend(
            GenotoxAlert(
                alert_id=pair.id,
                motif=pair.motif,
                explanation=pair.explanation,
                citation=pair.citation,
                matched=f"{a} + {b}",
            )
            for a in left
            for b in right
            if a != b
        )
    # Deduplicated after canonicalizing: `molecules` is keyed on the caller's spelling, so a route
    # listing one substance two ways would otherwise appear as two entities.
    canonical = list(dict.fromkeys(str(Chem.MolToSmiles(m)) for m in molecules.values()))
    return AlertResult(alerts=alerts, screened=canonical)
