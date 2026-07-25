"""Bench-chemistry tools the agent was missing (gaps TOOL-2, TOOL-3, TOOL-4, TOOL-5).

Four capabilities that were absent from the tool surface even though the chemistry for three of
them already existed somewhere in the repo:

- `resolve_compound` — every other chemistry tool takes SMILES; chemists write names (TOOL-2).
- hazard screening (TOOL-3) — landed independently on `main` as `safety/` + `agents.safety_tools`;
  this branch's named-substance and named-pair knowledge was contributed to `safety/rules.yaml`
  rather than kept as a second screen (see D-081).
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

from agents.tool_registry import tool
from chemclaw.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.config import settings
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


@tool
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


@tool
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


class GreenMetrics(BaseModel):
    """Green-chemistry mass metrics for one batch, with the masses they were derived from."""

    total_input_kg: float
    product_kg: float
    waste_kg: float
    e_factor: float
    pmi: float


@tool
async def green_metrics(input_masses_g: list[float], product_mass_g: float) -> GreenMetrics:
    """Compute the E-factor and PMI of a set of conditions (gap IDEA-3).

    Use this to compare routes or condition sets on waste, not only on yield — "comparable yield at
    half the PMI" is a real process-development goal and the agent had no way to answer it. Pair it
    with `stoichiometry_table`, whose `mass_g` column is exactly this input.

    E-factor is kg waste per kg product (Sheldon); PMI is total input mass per kg product, and the
    two differ by exactly 1 by construction. Lower is better for both.

    Args:
        input_masses_g: Every charged species' mass in grams — reagents, catalyst, and solvent.
            Omitting solvent is the usual way these numbers get flattered; include it.
        product_mass_g: Isolated product mass in grams. Must be positive.

    Returns:
        Both metrics plus the masses behind them, so the number can be checked rather than trusted.
    """
    if product_mass_g <= 0:
        raise ValueError("product_mass_g must be positive")
    if any(mass < 0 for mass in input_masses_g):
        raise ValueError("input masses must not be negative")
    total = sum(input_masses_g)
    if total < product_mass_g:
        # Mass cannot appear from nowhere; a total below the product is a data error, and silently
        # reporting a negative E-factor would read as an implausibly green process (the same
        # unsound-mass-balance trap CHECKMATE 2b fixed in the eval metric).
        raise ValueError(
            f"total input {total:g} g is below the product mass {product_mass_g:g} g — "
            "the mass balance is unsound (is a reagent or the solvent missing?)"
        )
    waste = total - product_mass_g
    return GreenMetrics(
        total_input_kg=total / 1000.0,
        product_kg=product_mass_g / 1000.0,
        waste_kg=waste / 1000.0,
        e_factor=waste / product_mass_g,
        pmi=total / product_mass_g,
    )


@tool
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
