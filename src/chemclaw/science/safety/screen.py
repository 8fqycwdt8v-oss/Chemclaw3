"""Deterministic structural hazard screening (D-080) — advisory flags, never a clearance.

Why this exists: the agent already proposes procedures — BO recommendations (1d.5) and development
reports (5b) both publish agent-authored experimental content through the PR-gate — with no hazard
awareness anywhere in the system. A chemist reviewing such a proposal deserves the obvious
structural alerts up front rather than having to spot an azide in a SMILES string.

What this is: SMARTS matching against a committed, cited rule table (`safety/rules.yaml`), plus a
pairwise incompatibility check across a reaction's components. Deterministic, offline, no model,
no external database — a flag is reproducible and traceable to a literature source, which is what
makes it usable in a GxP review.

**What this is not, and must never be presented as:** a hazard assessment. No rule matching means
*no rule in the table matched* — it says nothing about toxicity, exposure, thermal stability of the
specific compound, scale, or the process around it. `ScreenResult.verdict` deliberately renders
that as "no rule matched" rather than any word resembling "safe": an over-trusted screen is more
dangerous than no screen, because it converts an absence of knowledge into apparent assurance.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, Field, computed_field
from rdkit import Chem

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)

Severity = Literal["high", "medium", "low"]

# Ordered worst-first: used to rank flags and to compare against the configured gate severity.
_SEVERITY_ORDER: dict[str, int] = {"high": 3, "medium": 2, "low": 1}


class SafetyRulesError(ChemclawError):
    """The hazard rule table is missing, malformed, or contains an unparseable SMARTS (G4).

    Fatal rather than skip-and-continue, unlike a broken note or export file: silently screening
    with half a rule table would report "no rule matched" for a hazard the table covers — the one
    failure mode this module exists to prevent.
    """


class HazardFlag(BaseModel):
    """One matched hazard rule: what fired, how serious, why, and where the claim comes from."""

    rule_id: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # Which input the rule matched — a SMILES for a structural rule, or "a + b" for a pair rule.
    matched: str = Field(min_length=1)


class ScreenResult(BaseModel):
    """The flags raised for one molecule or reaction, worst first."""

    flags: list[HazardFlag] = Field(default_factory=list)

    @property
    def max_severity(self) -> Severity | None:
        """The most serious severity present, or None when nothing matched."""
        return self.flags[0].severity if self.flags else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """A one-line summary for a human — never the word "safe" (see the module docstring).

        `computed_field`, not a bare `property`, and the difference is the whole point of the
        sentence. A plain property is not serialized: `model_dump()` on a clean screen returned
        exactly `{"flags": []}`, so the disclaimer had **zero** production callers and never
        reached the model that had to write the answer. A live run then showed a chemist saying
        they wanted to sign a risk assessment being told "no hazards detected" six times — the
        precise phrasing the `safety-screening` skill forbids in bold.

        The tool docstring already said all of this. A docstring is read once when the tool is
        defined; the result payload is what is in the context window when the answer is written,
        and only one of those two was carrying the caveat.
        """
        if not self.flags:
            return "No rule in the hazard table matched. This is not a safety assessment."
        return (
            f"{len(self.flags)} hazard rule(s) matched (most serious: {self.max_severity}). "
            "Advisory only — a human must assess the procedure."
        )


class _StructuralRule(BaseModel):
    """One structural alert as loaded from the rule table."""

    id: str = Field(min_length=1)
    smarts: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    # How many *distinct* matches of `smarts` a molecule must contain before the rule fires.
    #
    # CLAUDE.md forbids an abstraction with a single caller, and this has one. The exemption is
    # that a count is not expressible as a substructure boolean: "polynitro" means "two or more
    # nitro groups", and SMARTS can only say "this arrangement is present", so a single pattern
    # has to enumerate every relative arrangement — ortho, meta, para, then every ring size, then
    # every fused system. `polynitro-aromatic` tried to inline the count into the pattern by
    # spelling the ring out, and therefore matched *only* 1,2-dinitroarenes: TNT and picric acid
    # screened clean. There is no pattern-only fix; the count has to live beside the pattern.
    #
    # Counted with `GetSubstructMatches` at its default `uniquify=True`, and deliberately *not*
    # with RDKit's `maxMatches` short-circuit: `maxMatches` caps the raw embeddings collected
    # before uniquification, so a symmetric pattern (`[OX2][OX2]` embeds into HOOH twice, once
    # each way) could be truncated to fewer unique matches than the molecule really has. That
    # would be a silent false negative, which is the one failure mode this module exists to
    # prevent — and no amount of speed is worth buying it.
    min_matches: int = Field(default=1, ge=1)


class _PairRule(BaseModel):
    """One incompatibility between two components of the same reaction."""

    id: str = Field(min_length=1)
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    severity: Severity
    explanation: str = Field(min_length=1)
    citation: str = Field(min_length=1)


class _RuleTable(BaseModel):
    """The parsed rule file: structural alerts plus pairwise incompatibilities."""

    structural: list[_StructuralRule] = Field(default_factory=list)
    incompatible_pairs: list[_PairRule] = Field(default_factory=list)


_Table = TypeVar("_Table", bound=BaseModel)


def read_rule_table(path: str, model: type[_Table]) -> _Table:
    """Read one committed SMARTS table off disk and validate it into `model`.

    Public, and generic over the table model, because `science/safety/genotox.py` loads a second
    committed table with the same failure modes and the same required response to them: a table
    that cannot be read must stop the screen rather than yield an empty rule set, since an empty
    rule set reports "nothing matched" — indistinguishable from a clean molecule, and the one
    outcome both screens exist to prevent.

    The message names the file rather than calling every table "hazard rules". This package works
    hard to keep the process-safety screen and the genotoxicity screen distinct — they answer
    different questions and one must never be reported as the other — and a malformed
    `genotox_alerts.yaml` announcing itself as a hazard-rule fault sends the reader to the wrong
    table.
    """
    name = Path(path).name
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SafetyRulesError(f"cannot read the rule table {name} at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SafetyRulesError(
            f"the rule table {name} at {path} must be a mapping, got {type(raw).__name__}"
        )
    try:
        return model.model_validate(raw)
    except ValueError as exc:
        raise SafetyRulesError(f"invalid rule table {name} at {path}: {exc}") from exc


def compile_smarts(smarts: str, rule_id: str) -> Chem.Mol:
    """Compile one rule's SMARTS, failing loudly with the rule id that owns it.

    Public for the same reason `read_rule_table` is: the genotoxicity alert table needs the
    identical "name the rule that owns the broken pattern" behaviour, and two copies of it would
    drift.
    """
    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        raise SafetyRulesError(f"hazard rule {rule_id!r} has unparseable SMARTS: {smarts!r}")
    return pattern


@lru_cache(maxsize=4)
def _load_rules(path: str) -> tuple[_RuleTable, dict[str, Chem.Mol]]:
    """Parse and compile the rule table at `path` (cached per path — it is a committed file).

    Returns the table and a pattern map keyed by `<rule id>` for structural rules and
    `<rule id>:left` / `:right` for pair rules, so every SMARTS is compiled exactly once per
    process rather than on every screened molecule.
    """
    table = read_rule_table(path, _RuleTable)
    if not table.structural and not table.incompatible_pairs:
        raise SafetyRulesError(f"hazard rules at {path} contain no rules")
    patterns = {rule.id: compile_smarts(rule.smarts, rule.id) for rule in table.structural}
    for pair in table.incompatible_pairs:
        patterns[f"{pair.id}:left"] = compile_smarts(pair.left, pair.id)
        patterns[f"{pair.id}:right"] = compile_smarts(pair.right, pair.id)
    return table, patterns


def parse_molecule(smiles: str) -> Chem.Mol:
    """Parse a SMILES, raising the module's error type so a caller handles one exception (G4).

    Public because the genotoxicity alert screen must fail the same way on the same input; a
    second parser there would be a second place for "unparseable" to mean "clean".
    """
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise SafetyRulesError(f"unparseable SMILES for hazard screening: {smiles!r}")
    return molecule


def require_screenable_size(component_smiles: list[str], *, what: str) -> None:
    """Refuse a component list too large to screen, before any matching starts.

    Both screens in this package check their pair rules as a *cross-product*: every component
    matching one side against every component matching the other. So the results grow with the
    square of the input while the request itself stays small — 13 KiB of SMILES was measured
    producing 251,000 hazard flags and blocking the serving connector's event loop for 2.48 s, and
    the genotoxicity screen has the same shape (640 components, 102,400 alerts, 933 ms). The
    connector's request-size cap is no bound on this, because the amplification is in the response.

    Public because both screens must refuse identically. Refused rather than truncated: a hazard
    screen that silently dropped components would report "no rule matched" for chemistry it never
    looked at, and every tool description in this package says an empty result means no rule
    matched — never that something is safe.

    Raises:
        SafetyRulesError: more than `safety_max_components` components were given.
    """
    limit = settings.safety_max_components
    if len(component_smiles) > limit:
        raise SafetyRulesError(
            f"{what} accepts at most {limit} components, got {len(component_smiles)}. "
            "Screen a reaction's own species, not a library: pair rules are checked between "
            "every pair, so the work grows with the square of the list."
        )


def _sorted(flags: list[HazardFlag]) -> list[HazardFlag]:
    """Worst severity first, then by rule id, so a result is deterministic and reads top-down."""
    return sorted(flags, key=lambda f: (-_SEVERITY_ORDER[f.severity], f.rule_id))


def screen_structure(smiles: str) -> ScreenResult:
    """Flag hazardous structural motifs in one molecule (advisory — see the module docstring).

    Raises:
        SafetyRulesError: the SMILES is unparseable, or the rule table is missing/malformed.
    """
    molecule = parse_molecule(smiles)
    table, patterns = _load_rules(settings.safety_rules_path)
    flags = [
        HazardFlag(
            rule_id=rule.id,
            severity=rule.severity,
            explanation=rule.explanation,
            citation=rule.citation,
            matched=smiles,
        )
        for rule in table.structural
        if len(molecule.GetSubstructMatches(patterns[rule.id])) >= rule.min_matches
    ]
    return ScreenResult(flags=_sorted(flags))


def screen_reaction(component_smiles: list[str]) -> ScreenResult:
    """Screen every component of a reaction, plus incompatibilities *between* components.

    A reaction is more than its parts: an oxidizer and a reducing agent are each unremarkable
    alone and dangerous together, which no per-molecule screen can see. Structural flags from the
    components are deduplicated per (rule, molecule) so a reagent listed twice is reported once.

    Args:
        component_smiles: Every species in the reaction (reactants, reagents, solvents, products).

    Raises:
        SafetyRulesError: any component is unparseable, the rule table is missing/malformed, or
            more than `safety_max_components` components were given.
    """
    require_screenable_size(component_smiles, what="a hazard screen")
    table, patterns = _load_rules(settings.safety_rules_path)
    molecules = {smiles: parse_molecule(smiles) for smiles in dict.fromkeys(component_smiles)}
    flags = [flag for smiles in molecules for flag in screen_structure(smiles).flags]
    for pair in table.incompatible_pairs:
        left = [s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:left"])]
        right = [
            s for s, m in molecules.items() if m.HasSubstructMatch(patterns[f"{pair.id}:right"])
        ]
        matches = [(a, b) for a in left for b in right if a != b]
        flags.extend(
            HazardFlag(
                rule_id=pair.id,
                severity=pair.severity,
                explanation=pair.explanation,
                citation=pair.citation,
                matched=f"{a} + {b}",
            )
            for a, b in matches
        )
    return ScreenResult(flags=_sorted(flags))


def at_least(severity: Severity | None, threshold: Severity) -> bool:
    """Whether `severity` is at or above `threshold` (None — nothing matched — never is).

    One place decides what "at or above the gate" means, so the agent tool, the `kg-validate` gate,
    and any future consumer cannot drift apart on it.
    """
    return severity is not None and _SEVERITY_ORDER[severity] >= _SEVERITY_ORDER[threshold]
