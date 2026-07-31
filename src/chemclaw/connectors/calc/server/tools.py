"""The `calc` connector's MCP tool surface: the fast calculators (plan step 1c.5).

Exposes the cached calculators as MCP tools. Unlike the QM/HPC path, fast calculators run
**inline** (sub-second) — no durable workflow is needed; the calculation store (Phase 1b) already
makes a repeat call free and idempotent, which is why these are tools on a connector rather than a
`jobs:` entry. `default_store` names the production backend and is the seam tests swap for an
in-memory store.

Running here rather than in the agent's process is what takes `tblite` and the calculation store's
driver out of the chat service's image (D-110): a calculation's dependencies are the calculator's
business, and this capability scales on its own.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel
from rdkit import Chem

from chemclaw.core.chem import canonical_smiles, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.science.calc.calibration import (
    Calibration,
    PredictionRecord,
    Residual,
    calibration_for,
    reconciled_for,
    record_observation,
    record_prediction,
)
from chemclaw.science.calc.descriptors import (
    DescriptorInput,
    DescriptorProfile,
    run_cached_descriptor_profile,
)
from chemclaw.science.calc.logd import LogdInput, LogdResult
from chemclaw.science.calc.logd import predict_logd as _predict_logd
from chemclaw.science.calc.pka import PkaInput, PkaResult, run_cached_pka
from chemclaw.science.calc.pka import calc_version as pka_calc_version
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.solubility import (
    SolubilityInput,
    SolubilityResult,
    run_cached_solubility,
)
from chemclaw.science.calc.solubility import calc_version as solubility_calc_version
from chemclaw.science.calc.store import CalculationQuery, ResultStore, StoredResult
from chemclaw.science.calc.structure import structure_from_smiles
from chemclaw.science.calc.xtb import XtbInput, XtbResult, run_cached_xtb
from chemclaw.science.calc.xtb_opt import OptimizationSummary, OptSpec, run_cached_optimization
from chemclaw.science.calc.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
    run_cached_fukui,
    run_cached_properties,
)
from chemclaw.science.calc.xtb_thermo import ThermochemistryResult, ThermoSpec, relax_to_minimum

server = FastMCP("calc")


def default_store() -> ResultStore:
    """Return the production result store (Postgres). Overridden in tests."""
    return PostgresStore()


async def _log_prediction(
    calc_type: str,
    calc_version: str,
    smiles: str,
    value: float,
    uncertainty: float | None,
    unit: str,
) -> None:
    """Record a prediction for later reconciliation against a measurement (gap IDEA-2).

    Hooked at the *tool* layer rather than inside the calculators, because this is the boundary
    where a prediction becomes advice a chemist acts on — a cache hit deep in a workflow does not
    need re-logging, and the ledger is keyed on the input, not on how often it was read.

    The subject key is the canonical SMILES, the same identity the calculation cache uses, so a
    measurement of the same molecule meets its prediction without a second naming scheme.
    """
    canonical = canonical_smiles(smiles)
    await record_prediction(
        PredictionRecord(
            calc_type=calc_type,
            # Without this the unique index `(calc_type, calc_version, input_hash)` degenerated to
            # `(calc_type, input_hash)`, because every row carried the default `""` — so upgrading a
            # calculator silently overwrote the previous version's prediction and `calculator_trust`
            # reported a bias averaged across versions that were never comparable (REV-12).
            calc_version=calc_version,
            input_hash=stable_hash(canonical),
            subject=canonical,
            predicted_value=value,
            predicted_uncertainty=uncertainty,
            unit=unit,
        )
    )


@server.tool()
async def report_measurement(property_name: str, smiles: str, measured_value: float) -> str:
    """Record a *measured* property value, so predictions can be scored against reality.

    Call this when a chemist reports an experimental measurement for a property the system also
    predicts (`solubility` as log S, or `pka`). It closes the prediction loop: `calculator_trust`
    then reports how far that calculator has actually been off, instead of the agent having to
    reason about trust from prose.

    Args:
        property_name: Which predicted property was measured — "solubility" or "pka".
        smiles: The molecule measured, as SMILES.
        measured_value: The experimental value, in the property's own unit (log S, or pKa).

    Returns:
        Whether the measurement matched an existing prediction. "No prediction on file" is a normal
        answer — say so rather than implying the measurement was scored.
    """
    canonical = canonical_smiles(smiles)
    matched = await record_observation(
        property_name, stable_hash(canonical), measured_value, source="chemist-reported"
    )
    if matched:
        return f"Recorded; it reconciled {matched} prediction(s) for {canonical}."
    return (
        f"Recorded for {canonical}, but nothing had predicted {property_name} for it yet, "
        "so no prediction was scored."
    )


class CalculationRecord(BaseModel):
    """One stored calculation, flattened to what an agent can reason about.

    Not `StoredResult` itself: that carries a `CalculationKey` whose two hashes mean nothing to a
    reader and would spend the model's attention on them. `calc_ref` is the same key in the flat
    form a knowledge note cites (`type@version:hash:hash`), so a result found here can be quoted
    as evidence by the reference that resolves back to it.
    """

    calc_ref: str
    calc_type: str
    calc_version: str
    result: dict[str, Any]
    provenance: str
    computed_at: datetime | None = None
    compute_seconds: float | None = None


@server.tool()
async def find_calculations(
    smiles: str = "",
    calc_type: str = "",
    calc_version: str = "",
    since: str = "",
    until: str = "",
    limit: int = 20,
) -> list[CalculationRecord]:
    """Look up calculations this system has already run, instead of running them again.

    Every calculation ever computed is kept forever and keyed by (calculator, version, input,
    parameters) — including the expensive DFT jobs — but until now the only way to reach one was
    to ask for the exact same calculation and get a cache hit. This is the other question: *what
    do we already know about this molecule*, which is what a chemist actually asks before
    committing hours of compute.

    Use it before submitting anything expensive, and to answer "have we looked at this before?".
    An empty result is a real answer — say the store has nothing rather than implying the
    calculation was tried and failed.

    A returned `calc_ref` can be cited directly in a knowledge note, so an answer built on a
    stored value stays traceable to the run that produced it.

    Args:
        smiles: Restrict to one molecule (any valid SMILES for it — matching is on the canonical
            form, so "CCO" and "OCC" find the same rows). Empty means every molecule. This reaches
            the molecule-keyed calculators — pka, solubility, descriptors, dft. The xTB task
            results and geometry pointers are keyed by a 3-D structure, which a molecule alone
            does not determine, so ask for those by `calc_type` with no molecule filter; asking
            for both together is refused rather than answered with a misleading empty list.
        calc_type: Restrict to one kind of calculation, e.g. "xtb", "pka", "dft". Empty means all.
        calc_version: Restrict to one calculator version. Empty means every version — useful
            precisely when asking whether an older version's number is still what is on file.
        since: ISO-8601 date or timestamp; only results computed at or after it.
        until: ISO-8601 date or timestamp; only results computed at or before it.
        limit: How many to return, newest first.

    Returns:
        The matching calculations, newest first. Each carries the result payload the calculator
        produced, so no second call is needed to read a value.
    """
    query = CalculationQuery(
        smiles=smiles or None,
        calc_type=calc_type or None,
        calc_version=calc_version or None,
        since=_timestamp(since),
        until=_timestamp(until),
        limit=max(1, min(limit, settings.calc_find_max_results)),
    )
    return [_record(stored) for stored in await default_store().find(query)]


def _timestamp(value: str) -> datetime | None:
    """Parse an ISO-8601 date or timestamp, or None for an empty string.

    A malformed date raises rather than being dropped: silently ignoring "last Tuesday" would
    answer a question about a window with results from outside it, which reads as an authoritative
    "nothing else exists" — the failure mode this tool is least able to afford.
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def _record(stored: StoredResult) -> CalculationRecord:
    """Flatten one stored result into the agent-facing record."""
    return CalculationRecord(
        calc_ref=stored.key.as_str(),
        calc_type=stored.key.calc_type,
        calc_version=stored.key.calc_version,
        result=stored.result,
        provenance=stored.provenance,
        computed_at=stored.created_at,
        compute_seconds=stored.compute_seconds,
    )


class StoredArtifact(BaseModel):
    """One by-product a calculation left behind, described without being read.

    `artifact_ref` is the same `<calculation key>#<name>` string a knowledge note's `artifact_refs`
    cites, so a listing and a citation name the same thing and `fetch_artifact` takes either.
    """

    artifact_ref: str
    name: str
    media_type: str
    byte_size: int


class ArtifactContent(BaseModel):
    """One artifact's contents, as text, bounded.

    `byte_size` is the artifact's *full* size and `truncated` says whether `text` is all of it —
    together they are what keeps a partial read from being quoted as a complete one.
    """

    artifact_ref: str
    name: str
    media_type: str
    byte_size: int
    text: str
    truncated: bool


@server.tool()
async def list_artifacts(calc_ref: str) -> list[StoredArtifact]:
    """List the files a stored calculation left behind — geometries, Hessians, spectra.

    A calculation's *answer* is a small set of numbers, and `find_calculations` returns it. This is
    everything else the run produced and the system kept: the relaxed coordinates, the second
    derivatives, the raw vibrational spectrum. Those are the inputs that make the *next* question
    cheap — thermochemistry at another temperature, a conformer search seeded from a known
    structure — and until now nothing could see that they existed.

    An empty list is a real answer and usually the right one: most calculations produce no
    by-products worth keeping. It does not mean the calculation is missing — ask
    `find_calculations` for that.

    Args:
        calc_ref: A calculation key as `find_calculations` returns it, or as a note cites it
            (`calc_type@version:input_hash:params_hash`).

    Returns:
        One entry per stored by-product, with its size and type, ordered by name. Read one with
        `fetch_artifact`; check `byte_size` first, because a Hessian is megabytes and is meant to
        seed another calculation rather than to be read.
    """
    refs = await default_artifact_store().list_for(calc_ref)
    return [
        StoredArtifact(
            artifact_ref=ref.as_str(),
            name=ref.name,
            media_type=ref.media_type,
            byte_size=ref.byte_size,
        )
        for ref in refs
    ]


@server.tool()
async def fetch_artifact(artifact_ref: str, max_chars: int = 0) -> ArtifactContent:
    """Read a stored calculation by-product — an optimized geometry, a spectrum, a log.

    Use it to quote a computed structure or spectrum exactly rather than describing it from
    memory: the coordinates of a relaxed geometry, the band positions in a `vibspectrum`, the
    contents of a file a knowledge note cites in its `artifact_refs`.

    Two things it will not do, both deliberately. It refuses a binary artifact (a packed `.npy`
    array, an SCF restart) instead of returning something unreadable — those exist to seed a
    further calculation, not to be read. And it truncates at a configured ceiling, reporting
    `truncated` and the full `byte_size`, so a large file costs a bounded amount of context; if
    `truncated` is set, say the value came from part of the file.

    Args:
        artifact_ref: `<calculation key>#<name>`, as `list_artifacts` returns it and as a note's
            `artifact_refs` cites it.
        max_chars: Read at most this many characters. 0 uses the configured ceiling, which also
            caps any larger request.

    Returns:
        The artifact's text with its type and full size, and whether the text is all of it.
    """
    calc_key, separator, name = artifact_ref.rpartition("#")
    if not separator or not name:
        raise ValueError(
            f"{artifact_ref!r} is not an artifact reference "
            "(expected '<calculation key>#<name>', as list_artifacts returns)"
        )
    store = default_artifact_store()
    stored = {ref.name: ref for ref in await store.list_for(calc_key)}
    ref = stored.get(name)
    if ref is None:
        known = ", ".join(sorted(stored)) or "none"
        raise ValueError(
            f"no artifact {name!r} is stored for calculation {calc_key!r} "
            f"(stored under it: {known}). By-products are eviction-managed, so a reference from "
            "an older note may point at something that has since been reclaimed."
        )
    data = await store.open(ref.content_hash)
    if data is None:  # evicted between the listing and the read
        raise ValueError(f"artifact {artifact_ref!r} is no longer stored")
    try:
        # Decoding is the test, rather than a table of readable media types: the store accepts any
        # producer-given name and `media_type_for` falls back to opaque bytes for one it does not
        # know, so a type-based rule would refuse perfectly readable output from any tool added
        # later. What actually matters is whether the bytes are text, and this asks them.
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{artifact_ref!r} is binary ({ref.media_type}, {ref.byte_size} bytes), not text. "
            "It is stored to seed a further calculation, not to be read."
        ) from exc
    # `max(1, ...)` because a negative argument would otherwise slice from the *end*
    # (`text[:-5]` is all but the last five characters) and report itself as truncated — a
    # near-complete read dressed as a bounded one, which is the reading this tool least affords.
    limit = max(
        1, min(max_chars or settings.calc_artifact_max_chars, settings.calc_artifact_max_chars)
    )
    return ArtifactContent(
        artifact_ref=ref.as_str(),
        name=ref.name,
        media_type=ref.media_type,
        byte_size=ref.byte_size,
        text=text[:limit],
        truncated=len(text) > limit,
    )


# The calculators whose predictions are logged to the ledger, and how to read them back: the
# version that is current *now* and the unit its values are in.
#
# A table rather than a conditional. `calculator_trust` used to be
# `solubility = property_name == "solubility"` followed by two ternaries, so every name that was
# not "solubility" was answered as though it were pKa — an unknown property got a confident report
# about the wrong calculator, in the wrong unit, and the model had no way to tell. Adding a
# calibrated calculator is now one row, and asking about an uncalibrated one is an error that
# names what does exist.
_CALIBRATED: dict[str, tuple[Callable[[], str], str]] = {
    "solubility": (solubility_calc_version, "log S"),
    "pka": (pka_calc_version, "pKa"),
}


def _calibrated(property_name: str) -> tuple[str, str]:
    """The current version and unit for a calibrated property, or raise naming the alternatives."""
    entry = _CALIBRATED.get(property_name)
    if entry is None:
        raise ValueError(
            f"{property_name!r} is not a calibrated property (known: "
            f"{', '.join(sorted(_CALIBRATED))}). Only calculators that log their predictions can "
            "be scored against measurements."
        )
    version, unit = entry
    # The *current* version, not a pooled figure: the chemist is asking how far to trust the
    # calculator that is about to answer them, and a v1 that ran high averaged with a v2 that ran
    # low reads as well-calibrated while neither is (REV-12).
    return version(), unit


@server.tool()
async def calculator_trust(property_name: str) -> Calibration:
    """Report how far a calculator's predictions have actually been off, measured not asserted.

    Use this before leaning on a predicted value in an answer, and quote it: "the solubility model
    has run about 0.4 log units low over 18 measurements" is a far more useful caveat than a generic
    "predictions are uncertain".

    Read `n` first. Below the configured minimum the figures are not yet meaningful — say the
    calculator has not been calibrated rather than quoting a bias from three points.
    `uncertainty_coverage` is the subtle one: a low value means the stated error bars are too
    narrow, so the *uncertainty* is misleading even when the values look close.

    These are averages over every molecule measured. When the answer matters, follow up with
    `calculator_outliers`: a calculator can be well-behaved overall and badly wrong on one class of
    molecule, and an average cannot show that.

    Args:
        property_name: A calibrated property — "solubility" or "pka". Anything else is an error
            rather than a guess, and the message names what is available.

    Returns:
        Bias, mean absolute error, RMSE, and uncertainty coverage, with the observation count.
    """
    version, unit = _calibrated(property_name)
    return await calibration_for(property_name, version, unit=unit)


class OutlierResidual(BaseModel):
    """One molecule where a calculator's prediction and the measurement disagreed."""

    smiles: str
    predicted: float
    observed: float
    # Signed (predicted − observed), matching the reported bias: direction is half the information.
    error: float
    unit: str
    # Whether the measurement fell inside the prediction's own stated ±1σ. `None` when the
    # calculator claimed no uncertainty — deliberately not `False`, which would read as a miss.
    within_uncertainty: bool | None = None


@server.tool()
async def calculator_outliers(
    property_name: str, matching: str = "", limit: int = 10
) -> list[OutlierResidual]:
    """Show where a calculator was most wrong, molecule by molecule — optionally on one class.

    `calculator_trust` answers "how far off is this calculator on average". This answers the
    question a chemist actually acts on: *on what*. A model that is 0.3 log units off overall may
    be fine on neutrals and two units low on every acid, and no aggregate can show that — the two
    populations average into one reassuring number.

    Read it in two passes. Call it with no filter to see the worst misses and look for what they
    have in common; then call it again with `matching` set to that class to test the idea against
    the whole ledger. If the filtered errors are much larger than the unfiltered ones, say so in
    the answer and treat a prediction for that class as weak evidence.

    Every row is a real measurement someone made, so a short list means few measurements, not a
    well-behaved calculator. Check `calculator_trust`'s `n` before concluding anything.

    Args:
        property_name: A calibrated property — "solubility" or "pka".
        matching: Optional SMARTS or SMILES fragment; only molecules containing it are considered.
            Use it to test a hypothesis about a class ("C(=O)O" for carboxylic acids).
        limit: How many to return, largest absolute error first.

    Returns:
        The worst misses, each with what was predicted, what was measured, the signed error, and
        whether the calculator's own uncertainty covered it.
    """
    version, unit = _calibrated(property_name)
    residuals = await reconciled_for(property_name, version)
    if matching:
        residuals = await _only_matching(residuals, matching)
    worst = sorted(residuals, key=lambda r: abs(r.error), reverse=True)
    return [
        OutlierResidual(
            smiles=r.subject,
            predicted=r.predicted,
            observed=r.observed,
            error=r.error,
            unit=unit,
            within_uncertainty=r.within_uncertainty,
        )
        for r in worst[: max(1, min(limit, settings.calc_outliers_max_results))]
    ]


async def _only_matching(residuals: list[Residual], query: str) -> list[Residual]:
    """Keep the residuals whose subject contains `query`, matched off the event loop.

    Bounded by `substructure_match_timeout_seconds` for the same reason the fingerprint index's
    scan is: the ledger is small, but a short adversarial recursive SMARTS matches for minutes
    regardless of corpus size, and this coroutine shares its loop with every other request.
    """
    pattern = substructure_pattern(query)

    def _scan() -> list[Residual]:
        kept = []
        for residual in residuals:
            molecule = Chem.MolFromSmiles(residual.subject)
            # A subject that no longer parses is skipped rather than failing the listing: one bad
            # row must not hide every real outlier.
            if molecule is not None and molecule.HasSubstructMatch(pattern):
                kept.append(residual)
        return kept

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_scan), timeout=settings.substructure_match_timeout_seconds
        )
    except TimeoutError as exc:
        raise ValueError(
            f"substructure match for {query!r} exceeded "
            f"{settings.substructure_match_timeout_seconds}s; use a simpler fragment"
        ) from exc


@server.tool()
async def compute_xtb_energy(smiles: str, charge: int = 0) -> XtbResult:
    """Compute the GFN2-xTB total energy of a molecule (fast, semiempirical).

    Runs a quick semiempirical single point (no HPC). Results are cached, so
    repeating the same molecule and charge is free and returns instantly.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net molecular charge (0 = neutral).

    Returns:
        The method, charge, and total energy in Hartree.
    """
    result, _ = await run_cached_xtb(default_store(), XtbInput(smiles=smiles, charge=charge))
    return result


@server.tool()
async def predict_solubility(smiles: str) -> SolubilityResult:
    """Predict aqueous solubility (log S, mol/L) of a molecule, with uncertainty.

    Uses a fast property model; the result reports an uncertainty that you should
    pass on to the user rather than treating the value as exact. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted log solubility, its uncertainty, and the model used.
    """
    result, _ = await run_cached_solubility(default_store(), SolubilityInput(smiles=smiles))
    await _log_prediction(
        "solubility",
        solubility_calc_version(),
        smiles,
        result.log_s_mol_per_l,
        result.uncertainty_log,
        "log S",
    )
    return result


@server.tool()
async def predict_pka(smiles: str) -> PkaResult:
    """Predict a molecule's pKa via GFN2-xTB — an acid site, or a base's conjugate acid.

    Two domains with different accuracy, and `site` on the result says which one ran.
    **Acids** (`site="acid"`): the most acidic O-H/S-H proton — carboxylic acids, phenols,
    alcohols, thiols — reported with ~1.6 units of uncertainty. **Bases** (`site="base"`),
    when there is no acidic proton: the pKa of the *conjugate acid* (pKaH), the number
    tabulated for amines, reported with +/-1.0. An acid site wins when a molecule has both.

    Base coverage is **aromatic and aryl nitrogen only** — pyridines, imidazoles, azoles,
    anilines. Aliphatic amines raise instead of returning a value, and that refusal is
    load-bearing rather than cautious: over 13 reference amines the method ranks them at
    Spearman -0.17, because a continuum solvent cannot represent the ammonium ion's hydrogen
    bonding to water. Report that the value is not predictable rather than substituting
    another tool's output. Cached.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted pKa, which site it describes, the protonation/deprotonation energy,
        and the uncertainty.
    """
    result, _ = await run_cached_pka(default_store(), PkaInput(smiles=smiles))
    await _log_prediction("pka", pka_calc_version(), smiles, result.pka, result.uncertainty, "pKa")
    return result


@server.tool()
async def compute_electronic_properties(
    smiles: str, solvent: str | None = None
) -> ElectronicProperties:
    """Compute frontier orbitals, dipole, partial charges and bond orders (GFN2-xTB).

    One fast semiempirical calculation gives the HOMO and LUMO energies and their gap
    (eV), the dipole moment (Debye), Mulliken partial charges per atom, and Wiberg
    bond orders per bonded pair. Use it to compare the electronic character of related
    molecules — a smaller gap means a more easily excited/reactive π system, a larger
    dipole a more polar molecule, and the partial charges show where the electron
    density sits. These are semiempirical values on a force-field geometry: compare
    them across similar structures rather than quoting one as an absolute measurement.
    Cached, so repeats are free.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "toluene") for an ALPB
            solvated calculation; omit for gas phase.

    Returns:
        The total energy, HOMO/LUMO/gap in eV, dipole in Debye, per-atom charges and
        the bond orders. Atom indices match the heavy atoms of the canonical SMILES,
        with hydrogens following them.
    """
    result, _ = await run_cached_properties(default_store(), smiles, solvent)
    return result


@server.tool()
async def predict_site_reactivity(
    smiles: str, mode: FukuiMode = "electrophilic", top_n: int = 0
) -> SiteReactivityResult:
    """Rank the atoms of a molecule by how susceptible they are to attack (GFN2-xTB).

    Answers regioselectivity questions — which position of a ring is substituted,
    which site is oxidized, where a nucleophile adds — using condensed Fukui indices
    from three fast semiempirical calculations. Choose `mode` by what attacks the
    molecule: "electrophilic" for attack by an electrophile (e.g. aromatic
    nitration/halogenation), "nucleophilic" for attack by a nucleophile (e.g. addition
    to a carbonyl), "radical" for radical chemistry.

    Read the ranking as a hypothesis, not a prediction of yield: it ranks sites
    *within* this molecule only (never between molecules), it describes electronic
    susceptibility alone — sterics, the specific reagent and the solvent are not in
    the model — and a heteroatom often tops the list because of its lone pair, so for
    a ring-substitution question compare the ring carbons with each other. Cached, and
    asking a second mode for the same molecule is free.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell (no radicals).
        mode: Which attack to rank for.
        top_n: How many atoms to return, most susceptible first. 0 uses the configured
            default; pass a larger number to see the whole molecule.

    Returns:
        The ranked sites with all three Fukui indices per atom, and the total number
        of atoms the ranking was drawn from. Atom indices match the heavy atoms of the
        canonical SMILES, with hydrogens following them.
    """
    result, _ = await run_cached_fukui(default_store(), smiles, mode)
    limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
    return result.model_copy(update={"sites": result.sites[:limit]})


@server.tool()
async def optimize_geometry(smiles: str, solvent: str | None = None) -> OptimizationSummary:
    """Relax a molecule to its nearest stable 3D shape with GFN2-xTB.

    Every other fast calculation here describes whichever conformer was embedded from
    the SMILES and cleaned up with a force field. This one finds an actual minimum of
    the quantum-mechanical surface, which is what the energy and the frequencies are
    computed on. Use it before comparing energies that need to be trustworthy, and to
    see how far a starting guess was from a real structure — a large `relaxation_kcal`
    on a molecule means the unrelaxed numbers for it were describing a strained shape.

    It finds the *nearest* minimum, not the best one: a flexible molecule has many
    conformers and this relaxes into whichever basin it started in. Cached, so repeats
    are free, and the thermochemistry and reaction tools reuse the same result.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "thf"); omit for gas phase.

    Returns:
        The converged energy, how much the relaxation lowered it, how far the atoms
        moved, and the id of the resulting geometry.
    """
    # Embedding is synchronous RDKit (ETKDG + a force-field cleanup), tens of milliseconds for
    # a drug-sized molecule; this coroutine shares its loop with every other in-flight request.
    structure = await asyncio.to_thread(
        structure_from_smiles, smiles, multiplicity=None, optimize=True
    )
    result, _ = await run_cached_optimization(default_store(), structure, OptSpec(solvent=solvent))
    return OptimizationSummary.of(result)


@server.tool()
async def compute_thermochemistry(
    smiles: str,
    solvent: str | None = None,
    symmetry_number: int = 1,
    temperature_k: float = 0.0,
    top_bands: int = 0,
) -> ThermochemistryResult:
    """Compute vibrational frequencies, an IR spectrum, and free energy (GFN2-xTB).

    Optimizes the molecule, then takes its second derivatives. That gives three things:
    whether the structure is a genuine minimum (`is_minimum`, with any imaginary
    frequencies listed), a predicted IR spectrum with band positions and intensities,
    and ideal-gas thermochemistry — zero-point energy, enthalpy, entropy and Gibbs free
    energy. Use the spectrum to test a proposed structure against a measured one, and
    the free energy for equilibrium questions that an electronic energy cannot answer.

    Read it with three limits in mind. Frequencies are semiempirical and systematically
    a few percent off, so compare *patterns and orderings* with a measured spectrum
    rather than expecting positions to match. Everything describes one conformer, not
    the molecule's real population. And the entropy depends on the rotational symmetry
    number, which defaults to 1 — pass the true value (2 for water, 3 for ammonia, 6
    for ethane, 12 for benzene) when the molecule is symmetric, or the entropy comes
    out too high by R·ln(symmetry number).

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name; omit for gas phase.
        symmetry_number: Rotational symmetry number; 1 if the molecule has no symmetry.
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        top_bands: How many IR bands to report, strongest first. 0 uses the configured
            default; imaginary modes are always reported in full.

    Returns:
        Frequencies with IR intensities, whether the geometry is a minimum, and the
        thermochemistry with the uncertainty to quote alongside it.
    """
    # Embedding is synchronous RDKit (ETKDG + a force-field cleanup), tens of milliseconds for
    # a drug-sized molecule; this coroutine shares its loop with every other in-flight request.
    structure = await asyncio.to_thread(
        structure_from_smiles, smiles, multiplicity=None, optimize=True
    )
    spec = ThermoSpec(
        solvent=solvent,
        symmetry_number=symmetry_number,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
    )
    _, result, _ = await relax_to_minimum(
        default_store(), structure, OptSpec(solvent=solvent), spec
    )
    limit = top_bands if top_bands > 0 else settings.xtb_ir_bands_top_n
    # The imaginary mode's 3N-vector is refinement machinery, not something a model can
    # read; the frequency itself is already in `imaginary_frequencies_cm`.
    return result.model_copy(
        update={"modes": result.strongest_bands(limit), "imaginary_displacement": None}
    )


@server.tool()
async def predict_developability_profile(smiles: str) -> DescriptorProfile:
    """Compute a developability descriptor panel: MW, LogP, TPSA, H-bond counts, Ro5/Veber flags.

    Use this to triage a candidate before committing bench time — Lipinski's Rule-of-Five
    (`lipinski_violations`) and Veber's rule (`veber_pass`) are widely used oral-bioavailability
    heuristics, not developability verdicts. Report them as flags to weigh alongside everything
    else known about the molecule, never as a pass/fail gate on their own. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The descriptor panel plus the two rule-of-thumb flags.
    """
    result, _ = await run_cached_descriptor_profile(default_store(), DescriptorInput(smiles=smiles))
    return result


@server.tool()
async def predict_logd(smiles: str, ph: float | None = None) -> LogdResult:
    """Predict the pH-dependent distribution coefficient (logD) of a neutral O-H/S-H acid.

    Answers "how lipophilic is this at the pH I actually work at?" — useful for HPLC
    mobile-phase pH selection, extraction, and formulation, where the pH-independent LogP alone
    is not the number that matters. Built from the same acidic-site model as `predict_pka`, so it
    shares its domain limits: only O-H/S-H acids (carboxylic acids, phenols, alcohols, thiols);
    it raises an error for a base or a molecule with no such site rather than guessing.

    Args:
        smiles: The molecule as a SMILES string.
        ph: The pH to evaluate at. Defaults to 7.4 (physiological pH) if omitted.

    Returns:
        logD at the given pH, plus the LogP and pKa it was derived from and the pKa model's
        uncertainty (state it — this is not an exact value).
    """
    return await _predict_logd(default_store(), LogdInput(smiles=smiles, ph=ph))
