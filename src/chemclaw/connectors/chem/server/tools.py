"""The `chem` connector's MCP tool surface: bench chemistry (gaps TOOL-2, TOOL-4, TOOL-5).

Four capabilities that were absent from the tool surface even though the chemistry for three of
them already existed somewhere in the repo:

- `resolve_compound` — every other chemistry tool takes SMILES; chemists write names (TOOL-2).
- hazard screening (TOOL-3) — landed independently on `main` as `safety/`, and now served by the
  `safety` connector (D-110);
  this branch's named-substance and named-pair knowledge was contributed to `safety/rules.yaml`
  rather than kept as a second screen (see D-086).
- `stoichiometry_table` — mass balance exists in `chemclaw.ingest.eln.validate` (for validation)
and E-factor/PMI
  in `chemclaw.evals.metrics` (for scoring), but the agent could not answer "what do I weigh out?"
  (TOOL-4).
- `render_structure` — RDKit is already a dependency and the UI showed SMILES strings (TOOL-5).

All are pure and synchronous: no network, no durable state, no store. They are the cheap half
of the chemistry surface — the expensive half (xTB, BO, fingerprint search) already existed.

"Cheap" is relative to xTB, not to an event loop. RDKit parsing, `Descriptors.MolWt` and
especially 2D-coordinate generation plus SVG rendering are CPU-bound C++ that holds the GIL for
milliseconds to tens of milliseconds, and this server answers every connected chat turn on one
loop — a load test measured throughput flat from 10 to 50 concurrent users, the signature of
exactly this. So each tool does its RDKit work in a worker thread (`asyncio.to_thread`, the
idiom `chemclaw.science.calc.store` and `chemclaw.retrieval.retrievers` already use) and the
coroutine only awaits it. RDKit
releases the GIL for the heavy passes, so the threads are real parallelism on a multi-CPU pod.
"""

import asyncio
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from rdkit import Chem
from rdkit.Chem import Descriptors, Draw, rdChemReactions
from rdkit.Chem.Draw import rdMolDraw2D

from chemclaw.core.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.reagents import ResolvedCompound, density_of, resolve_compound_name

server = FastMCP("chem")


class ChargeRow(BaseModel):
    """One row of a charge table: what to weigh or measure out for a given species.

    Solvents share the row shape rather than living in a second list, and that is deliberate:
    `green_metrics`' own docstring points at "`stoichiometry_table`, whose `mass_g` column is
    exactly this input", and a separate list would invite the model to pass the reagent masses
    alone — which is precisely how E-factor and PMI get flattered on the term that dominates them.
    Every row therefore carries a real mass and real moles, however the charge was expressed.
    """

    name: str
    smiles: str
    # Which quantity the chemist actually specified for this species, so a reader can see whether
    # a number was given or derived. Solvent moles and equivalents are always derived.
    role: Literal["basis", "reagent", "solvent"]
    equivalents: float
    molecular_weight: float
    moles_mmol: float
    mass_g: float
    # Populated for solvents only — a reagent charged by mass has no volume to measure out.
    density_g_per_ml: float | None = None
    volume_ml: float | None = None


class ChargeTable(BaseModel):
    """A charge table for one batch: the limiting reagent plus every other species scaled to it."""

    basis_name: str
    basis_mass_g: float
    rows: list[ChargeRow] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


@server.tool()
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
    # An unrecognised name falls through to an RDKit canonicalisation attempt, so this is not the
    # dictionary lookup it looks like.
    return await asyncio.to_thread(resolve_compound_name, name)


@server.tool()
async def stoichiometry_table(
    basis: str,
    basis_mass_g: float,
    reagents: list[str],
    equivalents: list[float],
    solvents: list[str] | None = None,
    volumes: list[float] | None = None,
) -> ChargeTable:
    """Build a charge table: what to weigh and measure out for a batch, scaled to the basis.

    Answers the everyday bench question — "for 250 g of the starting material at 1.2 equiv of base
    in 10 volumes of THF, what do I charge?" — deterministically, from molecular weights and
    densities.

    **A charge expressed in volumes goes in `solvents`/`volumes`.** Expressing "THF/water 4:1 at 10
    volumes" as molar equivalents is not a rounding error: done once on a 2 kg basis it put the
    principal solvent out by a factor of 2.17, and the answer then certified the figures as
    self-consistent. Converting volumes yourself is the mistake this argument pair exists to remove.

    **A substance is not owned by one of the two paths, and the tool does not police which you
    use.** Acetic acid at 1.5 equiv, water in a hydrolysis, methanol in an esterification, DMSO as
    the Swern oxidant and DMF as the Vilsmeier reagent are all charged by molar equivalent and all
    have a density on file. Only the chemist knows which reading was meant, so pass the charge in
    the units it was *specified* in and the table reports which those were on each row's `role`.

    Args:
        basis: The limiting reagent (name or SMILES); its mass sets the scale.
        basis_mass_g: How much of the limiting reagent is charged, in grams.
        reagents: The other species charged by molar equivalent (names or SMILES), in order.
        equivalents: Molar equivalents for each entry of `reagents`, same order and length.
        solvents: The species charged by volume (names or SMILES), in order.
        volumes: Process "volumes" for each entry of `solvents` — millilitres per gram of basis,
            same order and length. A 4:1 THF/water mixture at 10 total volumes is `[8.0, 2.0]`.

    Returns:
        One row per species with its molar amount and the mass to weigh out, and for solvents the
        density and the volume to measure. Reagent names that cannot be resolved are listed in
        `unresolved` and carry no row — never a guessed mass. A solvent that cannot be resolved, or
        whose density is not on file, is an error instead: a silently dropped solvent looks like a
        complete table while flattering every mass metric derived from it.
    """
    if len(reagents) != len(equivalents):
        raise ValueError(
            f"{len(reagents)} reagents but {len(equivalents)} equivalents; they must match"
        )
    charged_solvents, charged_volumes = solvents or [], volumes or []
    if len(charged_solvents) != len(charged_volumes):
        raise ValueError(
            f"{len(charged_solvents)} solvents but {len(charged_volumes)} volumes; they must match"
        )
    if basis_mass_g <= 0:
        raise ValueError("basis_mass_g must be positive")
    if any(volume <= 0 for volume in charged_volumes):
        raise ValueError("every entry of volumes must be positive")
    # One offload for the whole table rather than one per species: a 10-reagent charge table is
    # 11 RDKit parses, and hopping to a worker thread per parse would cost more than it saves.
    return await asyncio.to_thread(
        _charge_table, basis, basis_mass_g, reagents, equivalents, charged_solvents, charged_volumes
    )


def _charge_table(
    basis: str,
    basis_mass_g: float,
    reagents: list[str],
    equivalents: list[float],
    solvents: list[str],
    volumes: list[float],
) -> ChargeTable:
    """The charge table's RDKit-bound body, run in a worker thread (arguments already validated)."""
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
            role="basis",
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
                role="reagent",
                equivalents=equiv,
                molecular_weight=weight,
                moles_mmol=mmol,
                mass_g=mmol * weight / 1000.0,
            )
        )
    for solvent, solvent_volumes in zip(solvents, volumes, strict=True):
        table.rows.append(
            _solvent_row(solvent, solvent_volumes, basis_mass_g, basis_mmol),
        )
    return table


def _solvent_row(solvent: str, volumes: float, basis_mass_g: float, basis_mmol: float) -> ChargeRow:
    """One solvent charge, converted from volumes to a real mass — or an honest refusal.

    Both refusals are errors rather than an `unresolved` entry, unlike an unrecognised reagent. The
    asymmetry is deliberate: a chemist reads a charge list line by line and sees a missing reagent,
    whereas a missing solvent leaves a table that looks complete and quietly halves the E-factor
    and PMI computed from its masses. Neither a zero nor a guessed 1 g/mL is an acceptable stand-in.
    """
    match = resolve_compound_name(solvent)
    if match is None:
        raise ValueError(f"could not resolve the solvent {solvent!r}")
    density = density_of(solvent)
    if density is None:
        raise ValueError(
            f"no density on file for {match.name!r}, so its volume cannot be converted to a mass — "
            "convert the volume to molar equivalents yourself and pass it in `reagents`, or add "
            "its density to the reagent table"
        )
    volume_ml = volumes * basis_mass_g
    mass_g = volume_ml * density
    weight = _molecular_weight(match.smiles)
    mmol = mass_g / weight * 1000.0
    return ChargeRow(
        name=match.name,
        smiles=match.smiles,
        role="solvent",
        equivalents=mmol / basis_mmol,
        molecular_weight=weight,
        moles_mmol=mmol,
        mass_g=mass_g,
        density_g_per_ml=density,
        volume_ml=volume_ml,
    )


class GreenMetrics(BaseModel):
    """Green-chemistry mass metrics for one batch, with the masses they were derived from."""

    total_input_kg: float
    product_kg: float
    waste_kg: float
    e_factor: float
    pmi: float


@server.tool()
async def green_metrics(input_masses_g: list[float], product_mass_g: float) -> GreenMetrics:
    """Compute the E-factor and PMI of a set of conditions (gap IDEA-3).

    Use this to compare routes or condition sets on waste, not only on yield — "comparable yield at
    half the PMI" is a real process-development goal and the agent had no way to answer it. Pair it
    with `stoichiometry_table`, whose `mass_g` column is exactly this input.

    E-factor is kg waste per kg product (Sheldon); PMI is total input mass per kg product, and the
    two differ by exactly 1 by construction. Lower is better for both.

    Args:
        input_masses_g: Every charged species' mass in grams — reagents, catalyst, and solvent.
            Omitting solvent is the usual way these numbers get flattered; include it. Take the
            `mass_g` of *every* row of the charge table, including the `solvent` ones, which is
            why they share one list there.
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


@server.tool()
async def render_structure(smiles: str) -> str:
    """Draw a molecule or reaction as an SVG the chat surface can show inline.

    Use this when a structure is the answer, or when naming several related structures in prose
    would be ambiguous — a chemist reads a drawing far faster than a SMILES string.

    Args:
        smiles: A molecule SMILES, or a reaction SMILES (`reactants>>products`).

    Returns:
        An inline SVG document.
    """
    return await asyncio.to_thread(_render_svg, smiles)


def _render_svg(smiles: str) -> str:
    """The depiction's RDKit body, run in a worker thread.

    Coordinate generation and rasterising to SVG are the most expensive synchronous work in this
    module (tens of milliseconds for a drug-sized molecule), and a chat turn that renders a
    structure would otherwise stall every other turn on the process for that long.
    """
    size = settings.structure_render_size_px
    if ">>" in smiles:
        # **Each side is validated before the reaction is built, and the `is None` check below is
        # not what catches a bad one.** `ReactionFromSmarts` does not return `None` for the inputs
        # that matter — it returns a *parsed reaction containing garbage*, which is then drawn.
        # Measured against the installed RDKit:
        #
        #   "°C>>CC=O"  -> a reaction whose reactant is **methane**. A stray temperature annotation
        #                  becomes a molecule, and a chemist is shown chemistry nobody wrote, with
        #                  no error anywhere.
        #   ">>"        -> an empty reaction, and `DrawReaction` on it **segfaults** — a hard crash
        #                  of the connector process, not an exception a caller can handle. That is
        #                  the whole worker gone, taking every other in-flight tool call with it,
        #                  reachable from one malformed string.
        #
        # So the guard has to be the strict parser the molecule branch already uses, applied per
        # side. `require_canonical_smiles` refuses "°C" and the empty string outright, which is
        # exactly the discrimination `ReactionFromSmarts` declines to make. Reassembling from the
        # canonical halves also means the drawn reaction is the one that was validated, rather than
        # a second parse of the raw text.
        sides = smiles.split(">>")
        if len(sides) != 2:
            raise InvalidSmilesError(
                f"a reaction SMILES has exactly one '>>' separator: {smiles!r}"
            )
        validated = ">>".join(require_canonical_smiles(side) for side in sides)
        reaction = rdChemReactions.ReactionFromSmarts(validated, useSmiles=True)
        if reaction is None:  # pragma: no cover - both sides already parsed individually
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
