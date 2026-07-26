"""One request model for every xTB task, and the one cache-key derivation (X1).

Why one model. Each xTB task (single point, electronic properties, Fukui indices,
and the optimization/thermochemistry tasks to come) needs the same question answered:
*what identifies this calculation?* Answering it per task is how a cache goes wrong —
someone adds a knob and forgets to key on it, and the next run silently serves a
result computed under the old setting. `XtbSpec` holds every field that can move a
number, and `cache_key` is written once over `model_dump()`, so a new field is keyed
by construction rather than by review (D-011: compute once, never twice).

The structure is *not* part of the spec: it is passed to `cache_key` separately
because it is the calculation's subject, not its settings — and because keying on
`Structure.structure_id` rather than on "this SMILES with that embedding seed" is what
lets an identical geometry from any source hit the same entry.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field

from calc import xtb_cli
from calc.store import CalculationKey
from calc.structure import Structure
from calc.xtb_engine import engine_version
from chemclaw.config import settings

# Which implementation runs a task. `tblite` is the in-process library; `xtb` is the
# binary, which carries ANCopt and GFN-FF (plan X5, `calc.xtb_cli`).
Backend = Literal["tblite", "xtb"]


def resolve_backend(preferred: str | None = None) -> Backend:
    """Pick a concrete backend now, so `auto` never reaches a cache key.

    A key containing "auto" would mean different things on two deployments, and they
    would silently share entries computed by different programs. Resolving at spec
    construction keeps the key honest about what actually ran (D-011).
    """
    choice = preferred or settings.xtb_engine
    if choice in ("xtb", "tblite"):
        return "xtb" if choice == "xtb" else "tblite"
    return "xtb" if xtb_cli.is_available() else "tblite"


def backend_version(backend: Backend) -> str:
    """The build of `backend`, for the cache key. An upgrade must recompute."""
    if backend == "xtb":
        return f"xtb-{xtb_cli.binary_version()}/{engine_version()}"
    return engine_version()


# The xTB tasks this layer can run. `sp` is a plain single point; `properties` reads
# the same SCF's charges, bond orders, dipole and orbitals; `fukui` runs three single
# points (N, N-1, N+1 electrons) for the condensed Fukui indices; `opt` relaxes to a
# minimum; `hess` is the finite-difference Hessian and the thermochemistry over it;
# `scan` is a relaxed scan along one internal coordinate.
# `conformers` is a CREST ensemble search (plan X6, `calc.conformers`).
XtbTask = Literal["sp", "properties", "fukui", "opt", "hess", "scan", "conformers"]


class XtbSpec(BaseModel):
    """The settings of one xTB calculation — everything except its subject structure.

    Defaults come from config via `default_factory` (not a class-definition-time
    snapshot), so an ENV override applies to specs built afterwards, as the rest of
    the settings-driven code expects.

    **Task-specific settings live in subclasses** (`OptSpec`, `ThermoSpec`, `ScanSpec`),
    not in this model. A subclass inherits `cache_key` unchanged and its fields are
    keyed automatically, because the key is derived from `model_dump()` — so the
    invariant survives while a single point's key stays free of a temperature it does
    not have.
    """

    task: XtbTask
    # GFN parametrization, e.g. "GFN2-xTB". Part of `calc_version`, not `params`:
    # it identifies the method, which is what a cache version means.
    method: str = Field(default_factory=lambda: settings.xtb_method)
    # Which implementation runs it. Also part of `calc_version` and for the same reason:
    # two backends produce different numbers for the same request, so they are different
    # calculator versions, not different parameters of one.
    engine: Backend = Field(default_factory=resolve_backend)
    # ALPB implicit solvent name, or None for gas phase. ALPB is the only solvation
    # model tblite exposes here; a model *choice* arrives with the xtb binary (X5).
    solvent: str | None = None

    def for_structure(self, structure: Structure) -> Self:
        """The spec that will actually run for `structure`, backend included.

        **Open-shell systems fall back to the in-process backend**, whatever was
        configured, and the reason is measured. GFN2 without a spin-polarization term
        does not stabilize an open shell at all — it put triplet O2 *above* singlet
        (D-085) — so `calc.xtb_engine` enables that contribution whenever there are
        unpaired electrons. The `xtb` 6.6.1 binary cannot: its `--spinpol` is killed by
        the OOM killer in this build. Running a radical through it would silently
        reintroduce exactly the physics error D-085 removed.

        Resolving here rather than at the call site is what keeps the cache honest: the
        key is derived from the *returned* spec, so an entry computed by tblite is
        recorded as tblite's even when the deployment prefers the binary. Idempotent, so
        callers may apply it more than once.
        """
        if self.engine == "xtb" and structure.uhf:
            return self.model_copy(update={"engine": "tblite"})
        return self

    def cache_key(self, structure: Structure) -> CalculationKey:
        """The versioned identity of running this spec on `structure`.

        `calc_version` carries the method *and* the engine build, so a tblite or RDKit
        upgrade recomputes rather than serving a value the new stack would not
        reproduce. Everything else in the spec lands in `params` automatically — that
        is the whole point of deriving the key from `model_dump()`.
        """
        resolved = self.for_structure(structure)
        if resolved is not self:
            return resolved.cache_key(structure)
        return CalculationKey.build(
            calc_type=f"xtb.{self.task}",
            calc_version=f"{self.method}+{self.engine}+{backend_version(self.engine)}",
            inputs={
                "structure": structure.structure_id,
                "charge": structure.charge,
                "multiplicity": structure.multiplicity,
            },
            params=self.model_dump(exclude={"task", "method", "engine"}),
        )
