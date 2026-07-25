"""A deterministic hazard screen in front of agent-proposed chemistry (gap TOOL-3).

`BACKLOG.md` has carried "chemical/biological safety layer — distinct from Entra-ID/RBAC" as an
open decision since the research review, with the note: *decide scope before any capability phase
that could propose a hazardous route/procedure.* That phase shipped. `_INSTRUCTIONS` directs the
agent to "help design new conditions/protocols", and `propose_knowledge_note` writes those
proposals into the knowledge graph for later reuse and cross-project distillation — so an unsafe
suggestion does not merely get read once, it becomes a candidate precedent.

Nothing in the stack could flag an energetic intermediate, an incompatible quench, or a
peroxide-former. The PR-gate is the right *final* control and is not a screen: a reviewer sees a
plausible-looking protocol with no hazard annotation on it.

**This is the only gap in the analysis whose failure mode is physical rather than informational**,
which is why it is deterministic by construction:

- structural motifs are RDKit SMARTS, so the same molecule always screens the same way;
- reagent hazards are a committed table keyed by canonical structure (via `chemclaw.reagents`),
  reviewable in a PR like the eval case-set;
- incompatibilities are an explicit symmetric pair table.

No LLM judgment lives here. *Whether a flagged combination is acceptable in context* is chemistry
judgment and belongs in the skill; *whether the motif is present* is a fact and belongs here. The
screen is advisory-by-design — it annotates, it does not veto — because a false negative from an
over-narrow table must not be readable as a safety clearance. `screen_hazards` therefore always
reports what it did **not** cover.
"""

from enum import StrEnum

from pydantic import BaseModel, Field
from rdkit import Chem

from chemclaw.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.reagents import resolve_compound_name


class Severity(StrEnum):
    """How much attention a finding demands. Ordered: `critical` > `high` > `caution`."""

    CRITICAL = "critical"
    HIGH = "high"
    CAUTION = "caution"


_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.CAUTION: 2}


class HazardFinding(BaseModel):
    """One hazard the screen recognised, and what to do about it."""

    severity: Severity
    subject: str
    hazard: str
    guidance: str


class HazardReport(BaseModel):
    """The screen's verdict over a set of species.

    `unresolved` is as important as `findings`: a species the screen could not identify was not
    screened, and saying so is what keeps a clean report from reading as a safety clearance.
    """

    findings: list[HazardFinding] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    screened: list[str] = Field(default_factory=list)

    @property
    def highest_severity(self) -> Severity | None:
        """The most severe finding, or `None` when nothing was flagged."""
        return min((f.severity for f in self.findings), key=lambda s: _ORDER[s], default=None)


# Structural motifs that carry intrinsic process-safety risk regardless of which reagent supplies
# them. SMARTS, so the match is a fact about the structure, not a name lookup — a novel intermediate
# bearing one of these is caught even though no table could list it.
_MOTIFS: list[tuple[str, str, Severity, str]] = [
    (
        "organic azide",
        "[NX2]=[NX2+]=[NX1-]",
        Severity.CRITICAL,
        "Energetic. Keep the carbon:nitrogen ratio above ~3:1, avoid isolation of low-molecular-"
        "weight azides, never distil to dryness, and screen by DSC before scale-up.",
    ),
    (
        "nitro group on an aromatic ring",
        "[$([NX3](=O)=O),$([NX3+](=O)[O-])][c]",
        Severity.HIGH,
        "Polynitro aromatics are energetic; assess thermal stability (DSC/ARC) before heating or "
        "concentrating, and check the accumulated adiabatic temperature rise.",
    ),
    (
        "peroxide / hydroperoxide",
        "[OX2][OX2]",
        Severity.CRITICAL,
        "Shock/heat sensitive and prone to accumulation. Never concentrate to dryness; test for "
        "peroxides before distillation and quench excess before workup.",
    ),
    (
        "diazo / diazonium",
        "[$([CX3]=[NX2+]=[NX1-]),$([NX2+]#[NX1])]",
        Severity.CRITICAL,
        "Highly energetic and often shock sensitive. Keep in solution, keep cold, and do not "
        "isolate; prefer in-situ generation and immediate consumption.",
    ),
    (
        "N-nitroso group",
        "[NX3][NX2]=[OX1]",
        Severity.HIGH,
        "Potential mutagenic impurity (ICH M7 cohort of concern). Assess formation risk wherever a "
        "secondary amine meets a nitrosating agent, and control to the relevant AI limit.",
    ),
    (
        "acyl azide",
        "[CX3](=[OX1])[NX2]=[NX2+]=[NX1-]",
        Severity.CRITICAL,
        "Energetic and Curtius-active. Generate in situ and control the exotherm; do not isolate.",
    ),
    (
        "epoxide",
        "[OX2r3]1[#6r3][#6r3]1",
        Severity.CAUTION,
        "Alkylating agent and potential mutagenic impurity; control residual levels and handle "
        "with appropriate containment.",
    ),
]

# Reagent-specific hazards, keyed by the name `chemclaw.reagents` resolves to a canonical structure.
# These are properties of the *substance* that no structural motif expresses.
_REAGENT_HAZARDS: dict[str, tuple[Severity, str, str]] = {
    "sodium azide": (
        Severity.CRITICAL,
        "Forms explosive heavy-metal azides and toxic HN3 on acidification",
        "Never allow contact with dichloromethane (forms di/triazidomethane) or acid; do not use "
        "metal spatulas or dispose down drains with lead/copper plumbing.",
    ),
    "sodium hydride": (
        Severity.HIGH,
        "Pyrophoric on contact with water; DMF/DMSO runaway risk",
        "Avoid NaH in DMF or DMSO above ~40 °C — a well-documented thermal runaway. Quench "
        "cautiously with isopropanol before water.",
    ),
    "lithium aluminium hydride": (
        Severity.HIGH,
        "Pyrophoric; violent reaction with water",
        "Quench sequentially (Fieser) under inert gas with strict temperature control; never add "
        "water directly to the reaction mass.",
    ),
    "thionyl chloride": (
        Severity.HIGH,
        "Violent hydrolysis releasing SO2 and HCl",
        "Add to substrate, never water to reagent; scrub off-gas and control the gas-evolution "
        "rate on scale.",
    ),
    "meta-chloroperoxybenzoic acid": (
        Severity.HIGH,
        "Peroxy acid — shock and heat sensitive when dry",
        "Keep wet, keep cold, never concentrate the reaction mixture to dryness, and quench "
        "residual peroxide before workup.",
    ),
    "hydrogen peroxide": (
        Severity.HIGH,
        "Strong oxidant; decomposes exothermically and can accumulate",
        "Control addition rate and temperature; test for residual peroxide before concentrating.",
    ),
    "tert-butyl hydroperoxide": (
        Severity.HIGH,
        "Organic hydroperoxide — energetic on concentration",
        "Never concentrate to dryness; quench residual peroxide before workup.",
    ),
    "diphenylphosphoryl azide": (
        Severity.HIGH,
        "Azide transfer reagent; Curtius rearrangement releases N2 and forms isocyanate",
        "Control the exotherm and gas evolution; do not isolate the intermediate acyl azide.",
    ),
    "trifluoroacetic anhydride": (
        Severity.CAUTION,
        "Violent hydrolysis; corrosive",
        "Charge under inert atmosphere with cooling; vent the exotherm.",
    ),
    "1,4-dioxane": (
        Severity.CAUTION,
        "Peroxide-forming ether; also a suspected carcinogen",
        "Check peroxide date and test before distillation; never distil to dryness.",
    ),
    "tetrahydrofuran": (
        Severity.CAUTION,
        "Peroxide-forming ether",
        "Check peroxide date and test before distillation; never distil to dryness.",
    ),
    "diethyl ether": (
        Severity.CAUTION,
        "Peroxide-forming ether; extremely flammable",
        "Check peroxide date and test before distillation; never distil to dryness.",
    ),
}

# Pairs that are individually manageable and dangerous together, by resolved reagent name. Order is
# irrelevant, so the table is written once and matched symmetrically.
_INCOMPATIBLE: list[tuple[str, str, Severity, str, str]] = [
    (
        "sodium azide",
        "dichloromethane",
        Severity.CRITICAL,
        "Sodium azide with DCM forms di- and triazidomethane — explosive and shock sensitive",
        "Substitute the solvent (MeCN, DMF, or 2-MeTHF are usual); this combination has caused "
        "documented laboratory detonations.",
    ),
    (
        "sodium hydride",
        "N,N-dimethylformamide",
        Severity.HIGH,
        "NaH in DMF undergoes an autocatalytic exotherm above ~40 °C",
        "Use THF or 2-MeTHF, or hold below 40 °C with active cooling and thermal screening.",
    ),
    (
        "sodium hydride",
        "dimethyl sulfoxide",
        Severity.HIGH,
        "NaH in DMSO forms dimsyl sodium and can run away exothermically",
        "Limit temperature and charge rate, or select a different base/solvent pair.",
    ),
    (
        "hydrogen peroxide",
        "acetone",
        Severity.CRITICAL,
        "Peroxide plus ketone forms acetone peroxide (TATP/TCAP) — a primary explosive",
        "Never combine; choose a different oxidant or solvent.",
    ),
    (
        "lithium aluminium hydride",
        "dichloromethane",
        Severity.HIGH,
        "LiAlH4 reacts violently with chlorinated solvents",
        "Use an ethereal solvent (THF, 2-MeTHF, Et2O).",
    ),
    (
        "sodium borohydride",
        "dichloromethane",
        Severity.CAUTION,
        "Hydride reducing agents react exothermically with chlorinated solvents on scale",
        "Prefer an alcoholic or ethereal solvent.",
    ),
]


def _compiled_motifs() -> list[tuple[str, Chem.Mol, Severity, str]]:
    """Compile the SMARTS once; a bad pattern fails loudly at import, never silently at runtime."""
    compiled = []
    for label, smarts, severity, guidance in _MOTIFS:
        pattern = Chem.MolFromSmarts(smarts)
        if pattern is None:  # pragma: no cover - a table typo, caught at import
            raise ValueError(f"hazard motif {label!r} has invalid SMARTS: {smarts!r}")
        compiled.append((label, pattern, severity, guidance))
    return compiled


_PATTERNS = _compiled_motifs()


def screen_species(species: list[str]) -> HazardReport:
    """Screen a set of reagents/solvents/intermediates, given as names or SMILES.

    Every entry is resolved through `chemclaw.reagents` first, so `NaN3`, `sodium azide`, and
    `[Na+].[N-]=[N+]=[N-]` all screen identically — the whole point of pairing this with TOOL-2.

    An entry that cannot be resolved is reported in `unresolved` rather than dropped: a species the
    screen never saw was not screened, and a report that hides that reads as a clearance it has not
    earned.
    """
    report = HazardReport()
    resolved: dict[str, str] = {}  # canonical smiles -> recognised name (or the raw query)
    for entry in species:
        match = resolve_compound_name(entry)
        if match is None:
            report.unresolved.append(entry)
            continue
        resolved[match.smiles] = match.name
        report.screened.append(match.name)
        report.findings.extend(_reagent_findings(match.name))
        report.findings.extend(_motif_findings(match.smiles, match.name))
    report.findings.extend(_incompatibility_findings(set(resolved.values())))
    report.findings.sort(key=lambda f: (_ORDER[f.severity], f.subject, f.hazard))
    return report


def _reagent_findings(name: str) -> list[HazardFinding]:
    """Substance-specific hazards the structure alone does not express."""
    entry = _REAGENT_HAZARDS.get(name)
    if entry is None:
        return []
    severity, hazard, guidance = entry
    return [HazardFinding(severity=severity, subject=name, hazard=hazard, guidance=guidance)]


def _motif_findings(smiles: str, name: str) -> list[HazardFinding]:
    """Structural motifs carrying intrinsic risk — catches novel intermediates no table lists."""
    try:
        mol = Chem.MolFromSmiles(require_canonical_smiles(smiles))
    except InvalidSmilesError:  # pragma: no cover - resolution already guaranteed parseability
        return []
    if mol is None:  # pragma: no cover - same
        return []
    findings = []
    for label, pattern, severity, guidance in _PATTERNS:
        if mol.HasSubstructMatch(pattern):
            findings.append(
                HazardFinding(
                    severity=severity,
                    subject=name,
                    hazard=f"contains a {label}",
                    guidance=guidance,
                )
            )
    return findings


def _incompatibility_findings(names: set[str]) -> list[HazardFinding]:
    """Pairs that are individually manageable and dangerous together."""
    findings = []
    for left, right, severity, hazard, guidance in _INCOMPATIBLE:
        if left in names and right in names:
            findings.append(
                HazardFinding(
                    severity=severity,
                    subject=f"{left} + {right}",
                    hazard=hazard,
                    guidance=guidance,
                )
            )
    return findings
