"""Approximate normal coordinates: a preconditioner for Cartesian optimization (X9).

Why this exists after X5 already made optimization fast. The `xtb` binary's ANCopt is
8-11x faster than a Cartesian optimizer, and it retired the general case — but two paths
still cannot use it, and they are not small:

- **relaxed scans**, because holding atoms fixed is expressible as optimizer bounds and
  not as an xtb flag without writing a control file, which is precisely the input surface
  `chemclaw.science.calc.xtb_cli` refuses to have. A scan is one constrained optimization *per
  point*, so
  a 24-point profile pays the Cartesian cost 24 times;
- **open-shell species**, which route to the in-process backend because the binary cannot
  apply the spin-polarization term their energy needs (`XtbSpec.for_structure`).

So the remaining work was never "write an internal-coordinate optimizer to replace
ANCopt" — it was "make the fallback path, which handles exactly the cases ANCopt cannot,
stop being the slow one".

**What is actually wrong with Cartesian coordinates.** Nothing about the surface; the
*conditioning*. A bond stretch and a torsion differ in stiffness by two orders of
magnitude, so a step size that is sane for one is absurd for the other, and L-BFGS spends
its iterations learning that curvature from scratch. Optimizing in the eigenbasis of an
approximate Hessian, scaled by the square root of its eigenvalues, makes the surface
nearly isotropic — and because the transform is **linear**, there is no back-transformation
problem. That linearity is exactly why xtb's own optimizer uses approximate normal
coordinates rather than redundant internals.

**The model Hessian** is Lindh's pairwise form: a distance-dependent stretch constant
between every atom pair, which is cheap and needs no connectivity perception. It omits
explicit bending and torsional terms, and measuring what that costs is what made this
work at all.

On ibuprofen the model gives **37 of 99 directions essentially zero curvature**, where the
true Hessian's lower quartile is 0.089 and its median 0.40 Hartree/Angstrom^2. So the
floor is not a numerical safety net — it is *the stand-in for the missing terms*, and its
value is the claim "a direction this model cannot see is about as stiff as a typical one".
Set at 0.005 (a safety-net value) the preconditioner made optimization **10% slower**;
swept against measurement it optimizes near 1.0 and turns over by 1.5. Measured there:

| case                          | Cartesian | preconditioned |
|-------------------------------|-----------|----------------|
| naproxen                      |  44 steps |  19 steps      |
| ibuprofen                     |  71 steps |  24 steps      |
| ibuprofen, 2 atoms frozen     |  57 steps |  27 steps      |
| benzyl radical                |  10 steps |   6 steps      |

About **2x**, consistently, including the two cases this exists for. That is modest beside
ANCopt's 8-11x, and it is honest about why: with the floor this high the scale ratio is
only ~3, so the model is being used to damp the stiff directions it identifies reliably
and trusted for nothing else. A full Lindh model with angle and torsion terms would do
better, at the cost of primitive-internal machinery and a Wilson B matrix — worth it only
if these two paths ever become the common case.
"""

import numpy as np

from chemclaw.core.config import settings

# Lindh's parameters, indexed by periodic-table row (H; Li-Ne; Na-Ar and below). `alpha`
# is in inverse bohr^2 and `r_ref` in bohr, so distances are converted before use.
_ALPHA = np.array(
    [
        [1.0000, 0.3949, 0.3949],
        [0.3949, 0.2800, 0.2800],
        [0.3949, 0.2800, 0.2800],
    ]
)
_R_REF = np.array(
    [
        [1.35, 2.10, 2.53],
        [2.10, 2.87, 3.40],
        [2.53, 3.40, 3.40],
    ]
)
# Lindh's stretch force constant, in Hartree/bohr^2.
_K_STRETCH = 0.45

_ANGSTROM_TO_BOHR = 1.8897259886


# Pairs further apart than this (Angstrom) contribute nothing: the Lindh factor has
# decayed to insignificance and the loop is O(N^2).
_INTERACTION_CUTOFF = 6.0


def _row(atomic_number: int) -> int:
    """Lindh's row index for an element: 0 for H/He, 1 for Li-Ne, 2 for everything else."""
    if atomic_number <= 2:
        return 0
    if atomic_number <= 10:
        return 1
    return 2


def model_hessian(numbers: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Lindh's approximate Cartesian Hessian, in Hartree/Angstrom^2.

    A stretch term between every pair, with a force constant that decays with distance,
    so bonded pairs dominate and nothing has to decide what a bond *is*. Assembled
    directly in Cartesian blocks — the second derivative of a pair stretch is the outer
    product of its unit vector, which is what makes this a few lines rather than a
    coordinate system.
    """
    count = len(numbers)
    hessian = np.zeros((3 * count, 3 * count))
    rows = [_row(int(number)) for number in numbers]
    for i in range(count):
        for j in range(i + 1, count):
            delta = positions[j] - positions[i]
            distance = float(np.linalg.norm(delta))
            if distance > _INTERACTION_CUTOFF or distance < 1e-8:
                continue
            bohr = distance * _ANGSTROM_TO_BOHR
            alpha = _ALPHA[rows[i], rows[j]]
            reference = _R_REF[rows[i], rows[j]]
            force = _K_STRETCH * np.exp(alpha * (reference**2 - bohr**2))
            # Hartree/bohr^2 -> Hartree/Angstrom^2.
            force *= _ANGSTROM_TO_BOHR**2
            unit = delta / distance
            block = force * np.outer(unit, unit)
            a, b = slice(3 * i, 3 * i + 3), slice(3 * j, 3 * j + 3)
            hessian[a, a] += block
            hessian[b, b] += block
            hessian[a, b] -= block
            hessian[b, a] -= block
    return hessian


def basis(
    numbers: np.ndarray, positions: np.ndarray, free: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return `(vectors, scale)` mapping preconditioned steps to Cartesian displacements.

    `free` is a boolean mask over the 3N Cartesian coordinates. A displacement in the
    preconditioned coordinate `s` is `vectors @ (s * scale)`, which lands in the *free*
    subspace — so frozen atoms are excluded by construction rather than by a constraint
    the optimizer has to respect.

    `scale` is the inverse square root of the (floored) curvature, which is what turns an
    anisotropic surface into a nearly isotropic one.
    """
    hessian = model_hessian(numbers, positions)[np.ix_(free, free)]
    eigenvalues, vectors = np.linalg.eigh(hessian)
    floor = settings.xtb_anc_curvature_floor
    return vectors, 1.0 / np.sqrt(np.clip(eigenvalues, floor, None))
