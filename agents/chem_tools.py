"""Bench-chemistry tools the agent was missing (gaps TOOL-2, TOOL-3, TOOL-4, TOOL-5).

Four capabilities that were absent from the tool surface even though the chemistry for three of
them already existed somewhere in the repo:

- `resolve_compound` — every other chemistry tool takes SMILES; chemists write names (TOOL-2).
- `screen_hazards` — the agent is instructed to design protocols and had nothing between its
  proposal and the knowledge graph but a human reading prose (TOOL-3).
- `stoichiometry_table` — mass balance exists in `eln.validate` (for validation) and E-factor/PMI
  in `evals.metrics` (for scoring), but the agent could not answer "what do I weigh out?" (TOOL-4).
- `render_structure` — RDKit is already a dependency and the UI showed SMILES strings (TOOL-5).

All four are pure and synchronous: no network, no durable state, no store. They are the cheap half
of the chemistry surface — the expensive half (xTB, BO, fingerprint search) already existed.
"""

from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Descriptors, Draw, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.config import settings
from chemclaw.hazard import HazardReport, screen_species
from chemclaw.reagents import ResolvedCompound, resolve_compound_name


class ChargeRow(BaseModel):
    """One row of a charge table: what to weigh out for a given species."""

    name: str
    smiles: str
    equivalents: float
    molecular_weight: float
    moles_mmol: float
    mass_g: float


class ChargeTable(BaseModel):
    """A charge table for one batch: the limiting reagent plus every other species scaled to it."""

    basis_name: str
    basis_mass_g: float
    rows: list[ChargeRow] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


async def resolve_compound(name: str) -> ResolvedCompound | None:
    """Resolve a reagent name, abbreviation, or SMILES to its canonical structure.

    Use this whenever the chemist names a reagent in words ("DIPEA", "Pd(dppf)Cl2", "2-MeTHF")
    before calling any tool that needs a SMILES — the property calculators, the similarity search,
    and the substructure search all take structures, not names.

    Returns `None` when the name is not recognised. That is a real answer: say the reagent is not
    in the known set rather than guessing a structure, because a wrong structure would silently
    corrupt every downstream calculation and search.

    Args:
        name: What the chemist wrote — a trivial name, an abbreviation, or a SMILES string.

    Returns:
        The canonical structure with the name it was recognised as, or `None` if unknown.
    """
    return resolve_compound_name(name)


async def screen_hazards(species: list[str]) -> HazardReport:
    """Screen reagents/solvents/intermediates for process-safety hazards. Call before proposing.

    **Call this before proposing any protocol, route, or set of conditions**, and fold what it
    returns into the proposal. It catches three things a plausible-looking procedure hides:
    energetic structural motifs (azides, peroxides, diazo, polynitro), substance-specific hazards
    (NaH/DMF runaway, LiAlH4 quenching), and pairs that are safe apart and dangerous together
    (sodium azide with dichloromethane forms explosive diazidomethane).

    The screen is deterministic and advisory — it annotates, it never clears. Anything listed in
    `unresolved` was **not screened**, so a report with no findings is not a safety statement about
    those species; say so explicitly rather than implying the combination is safe.

    Args:
        species: Reagents, solvents, and intermediates, as names or SMILES (both work).

    Returns:
        Findings ordered most severe first, plus what was screened and what could not be resolved.
    """
    return screen_species(species)


async def stoichiometry_table(
    basis: str, basis_mass_g: float, reagents: list[str], equivalents: list[float]
) -> ChargeTable:
    """Build a charge table: what to weigh out for a batch, scaled to the limiting reagent.

    Answers the everyday bench question — "for 250 g of the starting material at 1.2 equiv of base,
    what do I charge?" — deterministically, from molecular weights.

    Args:
        basis: The limiting reagent (name or SMILES); its mass sets the scale.
        basis_mass_g: How much of the limiting reagent is charged, in grams.
        reagents: The other species to scale (names or SMILES), in order.
        equivalents: Molar equivalents for each entry of `reagents`, same order and length.

    Returns:
        One row per species with its molar amount and the mass to weigh out. Species that cannot
        be resolved are listed in `unresolved` and carry no row — never a guessed mass.
    """
    if len(reagents) != len(equivalents):
        raise ValueError(
            f"{len(reagents)} reagents but {len(equivalents)} equivalents; they must match"
        )
    if basis_mass_g <= 0:
        raise ValueError("basis_mass_g must be positive")
    anchor = resolve_compound_name(basis)
    if anchor is None:
        raise ValueError(f"could not resolve the limiting reagent {basis!r}")
    anchor_mw = _molecular_weight(anchor.smiles)
    basis_mmol = (basis_mass_g / anchor_mw) * 1000.0
    table = ChargeTable(basis_name=anchor.name, basis_mass_g=basis_mass_g)
    table.rows.append(
        ChargeRow(
            name=anchor.name,
            smiles=anchor.smiles,
            equivalents=1.0,
            molecular_weight=anchor_mw,
            moles_mmol=basis_mmol,
            mass_g=basis_mass_g,
        )
    )
    for reagent, equiv in zip(reagents, equivalents, strict=True):
        match = resolve_compound_name(reagent)
        if match is None:
            table.unresolved.append(reagent)
            continue
        weight = _molecular_weight(match.smiles)
        mmol = basis_mmol * equiv
        table.rows.append(
            ChargeRow(
                name=match.name,
                smiles=match.smiles,
                equivalents=equiv,
                molecular_weight=weight,
                moles_mmol=mmol,
                mass_g=mmol * weight / 1000.0,
            )
        )
    return table


async def render_structure(smiles: str) -> str:
    """Draw a molecule or reaction as an SVG the chat surface can show inline.

    Use this when a structure is the answer, or when naming several related structures in prose
    would be ambiguous — a chemist reads a drawing far faster than a SMILES string.

    Args:
        smiles: A molecule SMILES, or a reaction SMILES (`reactants>>products`).

    Returns:
        An inline SVG document.
    """
    size = settings.structure_render_size_px
    if ">>" in smiles:
        reaction = rdChemReactions.ReactionFromSmarts(smiles, useSmiles=True)
        if reaction is None:
            raise InvalidSmilesError(f"not a drawable reaction SMILES: {smiles!r}")
        drawer = rdMolDraw2D.MolDraw2DSVG(size * 2, size)
        drawer.DrawReaction(reaction)
    else:
        mol = Chem.MolFromSmiles(require_canonical_smiles(smiles))
        if mol is None:  # pragma: no cover - require_canonical_smiles already guarantees parsing
            raise InvalidSmilesError(f"not a drawable molecule: {smiles!r}")
        # Compute 2D coordinates so the depiction is laid out, not collapsed on the origin.
        Draw.rdDepictor.Compute2DCoords(mol)
        drawer = rdMolDraw2D.MolDraw2DSVG(size, size)
        drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return str(drawer.GetDrawingText())


def _molecular_weight(smiles: str) -> float:
    """Average molecular weight in g/mol, for the charge-table arithmetic."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:  # pragma: no cover - callers pass already-resolved structures
        raise InvalidSmilesError(f"cannot compute a molecular weight for {smiles!r}")
    return float(Descriptors.MolWt(mol))
