"""The Hessian as a cached calculation in its own right (STO-2).

A Hessian is the expensive half of every vibrational question and it depends on exactly two
things: the geometry, and the method that produced the second derivatives. It does **not** depend
on the temperature, the pressure, the rotational symmetry number, or the quasi-RRHO cutoff — those
are arithmetic applied afterwards.

`ThermoSpec` nonetheless carried all of them in one cache key, so asking for thermochemistry at
350 K after 298 K missed the cache and recomputed a matrix that could not have changed. Measured in
D-092, that matrix costs 26 s on 76 atoms through the binary and 218 s through finite differences.
The one question a stored Hessian answers trivially was the exact question that forced a full
recomputation.

Splitting it out gives two caches with honest keys: this one on `(geometry, method,
displacement)`, and the thermochemistry on all of that *plus* the state variables — because the
free energy genuinely does depend on the temperature. A second temperature is a miss on the cheap
cache and a hit on the expensive one.

**The matrix lives in the artifact store, not in the result row.** A 76-atom Hessian is 228x228
float64 — 416 kB, and JSONB is the wrong home for it. The row holds the content addresses; the
arrays are `.npy` blobs beside the raw `hessian`/`vibspectrum` files the binary wrote (D-124).

**A cached row whose artifact is gone is a miss, not a hit.** Artifacts are optional by
construction — the store can be disabled, an artifact can exceed the cap, and the eviction sweep
may reclaim a blob. So the read path verifies it can actually load the matrix before it claims a
hit, which is what keeps that contract literally true instead of quietly making artifacts
mandatory. A deployment with `artifact_store_enabled=False` therefore caches no Hessians at all
and recomputes exactly as it did before this module existed.
"""

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.science.calc import xtb_cli
from chemclaw.science.calc.artifacts import ArtifactRef, ArtifactStore, put_all
from chemclaw.science.calc.store import CalculationKey, ResultStore, StoredResult
from chemclaw.science.calc.structure import Structure
from chemclaw.science.calc.xtb_engine import AU_TO_DEBYE, evaluate_point, make_calculator
from chemclaw.science.calc.xtb_spec import XtbSpec

logger = logging.getLogger(__name__)

# Artifact names. The `.npy` suffix is load-bearing: it is what `calc.artifacts` maps to the numpy
# media type, and what tells a human reading a `calculation_artifacts` row how to open the blob.
HESSIAN_ARTIFACT = "hessian.npy"
DIPOLE_ARTIFACT = "dipole_derivatives.npy"


class HessianSpec(XtbSpec):
    """Settings of one second-derivative calculation — everything that moves the matrix.

    Deliberately narrower than `ThermoSpec`: `temperature_k`, `pressure_pa`, `symmetry_number` and
    `rrho_cutoff_cm` are absent because a Hessian does not depend on them. That absence *is* the
    fix — `XtbSpec.cache_key` keys on `model_dump()`, so a field that is not here cannot force a
    recomputation.
    """

    task: Literal["hess"] = "hess"
    displacement_angstrom: float = Field(
        default_factory=lambda: settings.xtb_hessian_displacement, gt=0
    )


@dataclass(frozen=True)
class Hessian:
    """The second derivatives of one geometry, plus what the run collected alongside them.

    Not a pydantic model: it holds numpy arrays and is never persisted in this form — the
    persisted form is `HessianResult` (content addresses) plus the blobs those address.

    Exactly one of `ir_intensities` and `dipole_derivatives` is populated, and which one says which
    backend ran. The `xtb` binary computes intensities itself and reports one per Cartesian mode
    (translations and rotations included, which the caller reconciles); the in-process path returns
    the dipole derivatives it collected while displacing, from which intensities are derived once
    the normal modes are known.
    """

    matrix: np.ndarray
    electronic_energy_hartree: float
    ir_intensities: np.ndarray | None = None
    dipole_derivatives: np.ndarray | None = None


class HessianResult(BaseModel):
    """The cache row: content addresses and the small scalars, never the matrix itself.

    `atom_count` is not decoration — it is checked against the loaded array's shape, so a blob
    that was truncated or collided into the wrong row is caught on read rather than producing
    silently wrong frequencies.
    """

    electronic_energy_hartree: float
    atom_count: int
    hessian_artifact: str
    dipole_derivatives_artifact: str | None = None
    # The binary's raw per-mode intensities, as it reported them. Small enough to live in the row,
    # and kept unaligned so the projection reconciliation stays in one place (`calc.xtb_thermo`).
    ir_intensities: list[float] | None = None


def _pack(array: np.ndarray) -> bytes:
    """Serialize a float array to `.npy` bytes — the portable, self-describing numpy format."""
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def _unpack(data: bytes) -> np.ndarray:
    """Read `.npy` bytes back into an array.

    `allow_pickle=False` because these bytes come out of a database: pickle deserialization is
    arbitrary code execution, and nothing this module stores needs it.
    """
    return np.asarray(np.load(io.BytesIO(data), allow_pickle=False))


def _finite_difference(
    spec: HessianSpec, structure: Structure
) -> tuple[np.ndarray, np.ndarray, float]:
    """Central-difference Hessian and dipole derivatives at `structure`'s geometry.

    Returns `(hessian, dipole_derivatives, energy)` — the Hessian in Hartree/Angstrom^2, shape
    (3N, 3N), the dipole derivatives in Debye/Angstrom, shape (3N, 3), and the electronic energy at
    the undisplaced geometry.

    **The energy comes back from here because this function already holds the calculator.** The
    caller used to build a *second* calculator over the same system to get it — a second Hamiltonian
    assembly, measured at 2 per Hessian against 1 now. The single point itself is not saved and was
    never duplicated: one runs either way, here instead of there. Naming that precisely matters,
    because "a second SCF" would be the interesting claim and it is not the true one.

    Cost is 6N + 1 single points: the gradient is analytic, so only *first* derivatives need
    differencing. The Hessian is symmetrized afterwards — central differences of an exact gradient
    give a nearly symmetric matrix, and forcing the symmetry removes the small asymmetry that would
    otherwise put a spurious imaginary component into the eigenvalues.
    """
    numbers, positions = structure.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    size = positions.size
    hessian = np.zeros((size, size))
    dipole_derivatives = np.zeros((size, 3))
    step = spec.displacement_angstrom
    energy, _, _ = evaluate_point(calc, positions)
    for index in range(size):
        shifted = positions.copy().ravel()
        shifted[index] += step
        _, gradient_plus, dipole_plus = evaluate_point(calc, shifted.reshape(-1, 3))
        shifted[index] -= 2 * step
        _, gradient_minus, dipole_minus = evaluate_point(calc, shifted.reshape(-1, 3))
        hessian[index] = (gradient_plus.ravel() - gradient_minus.ravel()) / (2 * step)
        dipole_derivatives[index] = (dipole_plus - dipole_minus) * AU_TO_DEBYE / (2 * step)
    return 0.5 * (hessian + hessian.T), dipole_derivatives, energy


def compute_hessian(spec: HessianSpec, structure: Structure) -> tuple[Hessian, dict[str, bytes]]:
    """The second derivatives at `structure`, plus every by-product worth keeping.

    The `xtb` binary computes both the Hessian and the IR intensities itself and is far faster at
    it — measured, a 76-atom Hessian in 26 s against 218 s of finite differences. What it does
    *not* get to supply is the thermochemistry over them: that stays in
    `chemclaw.science.calc.xtb_thermo`, so the
    symmetry number remains an explicit input and the quasi-RRHO treatment is identical whichever
    backend ran, which is what keeps free energies from the two comparable.

    Raises `ValueError` above `settings.xtb_hessian_max_atoms`: the in-process cost is 6N single
    points, and blocking an agent turn for minutes is a worse failure than saying the calculation
    needs the durable job path. Checked here rather than in the caller because this is where the
    cost is actually paid.
    """
    if len(structure.elements) > settings.xtb_hessian_max_atoms:
        raise ValueError(
            f"a Hessian on {len(structure.elements)} atoms exceeds the inline limit of "
            f"{settings.xtb_hessian_max_atoms}: submit it as a durable QM job instead"
        )
    if spec.for_structure(structure).engine == "xtb":
        outcome = xtb_cli.run(structure, task="hess", method=spec.method, solvent=spec.solvent)
        matrix = np.asarray(outcome.hessian)
        hessian = Hessian(
            matrix=matrix,
            electronic_energy_hartree=outcome.energy_hartree,
            ir_intensities=np.asarray(outcome.ir_intensities),
        )
        # The binary's own `hessian` and `vibspectrum` files ride along beside the packed array:
        # they are the format every other quantum chemistry program reads, so keeping them costs a
        # few kilobytes and saves a conversion nobody has written.
        return hessian, {**outcome.artifacts, HESSIAN_ARTIFACT: _pack(matrix)}

    matrix, dipole_derivatives, energy = _finite_difference(spec, structure)
    hessian = Hessian(
        matrix=matrix,
        electronic_energy_hartree=energy,
        dipole_derivatives=dipole_derivatives,
    )
    return hessian, {
        HESSIAN_ARTIFACT: _pack(matrix),
        DIPOLE_ARTIFACT: _pack(dipole_derivatives),
    }


async def _load(artifacts: ArtifactStore, row: HessianResult) -> Hessian | None:
    """Rebuild a `Hessian` from a cache row, or `None` if its blobs are no longer retrievable.

    Every reason this returns `None` is an ordinary one — the store is disabled, the blob was
    evicted, a deployment restored a database without its artifact table — so it is a miss to
    recompute from, never an error. A shape that disagrees with `atom_count` is treated the same
    way: recomputing costs minutes, while trusting a mismatched matrix costs wrong frequencies.
    """
    packed = await artifacts.open(row.hessian_artifact)
    if packed is None:
        return None
    matrix = _unpack(packed)
    expected = 3 * row.atom_count
    if matrix.shape != (expected, expected):
        logger.warning(
            "stored hessian %s has shape %s, expected (%d, %d) — recomputing",
            row.hessian_artifact,
            matrix.shape,
            expected,
            expected,
        )
        return None

    derivatives = None
    if row.dipole_derivatives_artifact is not None:
        blob = await artifacts.open(row.dipole_derivatives_artifact)
        if blob is None:
            # The intensities cannot be derived without them, and a Hessian without its IR data is
            # not what this row promised. Recompute rather than return a half result.
            return None
        derivatives = _unpack(blob)

    return Hessian(
        matrix=matrix,
        electronic_energy_hartree=row.electronic_energy_hartree,
        ir_intensities=(np.asarray(row.ir_intensities) if row.ir_intensities is not None else None),
        dipole_derivatives=derivatives,
    )


async def _persist(
    results: ResultStore,
    artifacts: ArtifactStore,
    key: CalculationKey,
    hessian: Hessian,
    files: dict[str, bytes],
    compute_seconds: float,
) -> bool:
    """Store the blobs and, only if the matrix landed, the row that addresses them.

    Returns whether the calculation was cached. The ordering matters and is the whole reason this
    is hand-rolled rather than written on a generic wrapper: a row whose `hessian_artifact` points
    at nothing would be served as a hit forever and rejected on every read. So the artifacts are
    written first and the row is written only if the matrix is genuinely retrievable.
    """
    try:
        stored = await put_all(artifacts, key.as_str(), files, compute_seconds=compute_seconds)
    except Exception:
        # An artifact is optional by construction: losing a by-product costs a future
        # recomputation, never the calculation in hand, which this function's caller already
        # holds.
        logger.warning("could not store hessian artifacts for %s", key.as_str(), exc_info=True)
        return False

    by_name: dict[str, ArtifactRef] = {ref.name: ref for ref in stored}
    matrix_ref = by_name.get(HESSIAN_ARTIFACT)
    if matrix_ref is None:
        logger.debug("hessian for %s was not stored, so it is not cached", key.as_str())
        return False
    derivatives_ref = by_name.get(DIPOLE_ARTIFACT)
    if hessian.dipole_derivatives is not None and derivatives_ref is None:
        # An in-process Hessian is unusable without them (see `_load`), so caching the row would
        # only guarantee a rejected read later.
        logger.debug("dipole derivatives for %s were not stored, so it is not cached", key.as_str())
        return False

    row = HessianResult(
        electronic_energy_hartree=hessian.electronic_energy_hartree,
        atom_count=hessian.matrix.shape[0] // 3,
        hessian_artifact=matrix_ref.content_hash,
        dipole_derivatives_artifact=(
            derivatives_ref.content_hash if derivatives_ref is not None else None
        ),
        ir_intensities=(
            [float(value) for value in hessian.ir_intensities]
            if hessian.ir_intensities is not None
            else None
        ),
    )
    await results.put(
        StoredResult(key=key, result=row.model_dump(), compute_seconds=compute_seconds)
    )
    return True


async def run_cached_hessian(
    results: ResultStore,
    artifacts: ArtifactStore,
    structure: Structure,
    spec: HessianSpec | None = None,
) -> tuple[Hessian, bool]:
    """Return the Hessian at `structure`, reusing the store on a repeat.

    Not built on `run_cached`, and the reason is specific rather than stylistic: it decides
    hit-versus-miss from the result row alone, and here the row is only half the result. The
    matrix lives in the artifact store, so a hit is only a hit if the blob comes back — see the
    module docstring.

    Returns `(hessian, was_cached)`, matching every other cached calculator's shape.
    """
    spec = spec or HessianSpec()
    # Off the event loop for the same reason every other `cache_key` call is: `calc_version()`
    # shells out to `xtb --version` on its first call in a process, and the hash walks every atom.
    key = await asyncio.to_thread(spec.cache_key, structure)

    hit = await results.get(key)
    if hit is not None:
        loaded = await _load(artifacts, HessianResult.model_validate(hit.result))
        if loaded is not None:
            logger.debug("hessian cache hit: %s", key.as_str())
            return loaded, True
        logger.info("hessian %s is cached but its matrix is gone; recomputing", key.as_str())

    logger.debug("hessian cache miss, computing: %s", key.as_str())
    started = time.perf_counter()
    hessian, files = await asyncio.to_thread(compute_hessian, spec, structure)
    elapsed = time.perf_counter() - started
    await _persist(results, artifacts, key, hessian, files, elapsed)
    return hessian, False
