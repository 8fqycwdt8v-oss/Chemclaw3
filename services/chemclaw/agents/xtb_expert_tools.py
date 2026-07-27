"""The expert escape hatch: run a fully specified xTB task (xTB plan X7).

Built last, deliberately. The proposal's argument was that an escape hatch built early
becomes the path of least resistance and the shaped tools stop growing; built after real
usage, it only has to cover what the shaped tools genuinely cannot express. What is left
after X1-X6 is a short list: a non-default GFN parametrization (GFN1 for a system GFN2
handles badly, GFN0 or GFN-FF for something too large to be worth a quantum method), a
tightened or loosened numerical accuracy, and a single point at a geometry the caller
already has.

**The security boundary, which is the whole reason this file is short.** The argument is
a **typed spec**, never a string: no argv, no flags, no `$...` control file, no file
paths. That matters concretely rather than theoretically — a SMILES, an ELN record and a
retrieved document all reach this tool through the model, and xtb's control-file syntax
can reference external files and point charges. With a typed spec the worst a prompt
injection achieves is an expensive but well-formed calculation, which the authorization
gate and the cost router already bound. `calc.xtb_cli` enforces the same rule one level
down: argv list, `shell=False`, scrubbed environment, temp directory, timeout.

Gated to a privileged role through `authorize_trigger` and listed in
`DEFAULT_WRITE_TOOL_GATES`, like `submit_qm_job`: closed unless an operator grants the
role. That is the gate — deliberately not a second on/off setting, because two
independent switches for one capability is how a deployment ends up believing something
is disabled when it is not.
"""

from typing import Literal

from pydantic import BaseModel, Field

from agents.authz import authorize_trigger
from agents.tool_registry import tool
from calc import xtb_cli
from calc.postgres_store import default_store
from calc.structure import structure_from_smiles
from calc.xtb_opt import OptimizationSummary, OptSpec, run_cached_optimization
from calc.xtb_spec import resolve_backend


class ExpertXtbRequest(BaseModel):
    """A fully specified xTB calculation. Every field is typed; none is a command line."""

    model_config = {"extra": "forbid"}

    smiles: str = Field(min_length=1)
    task: Literal["sp", "opt"] = "sp"
    # Restricted to the parametrizations the backend actually implements, so an unknown
    # value is a validation error here rather than a subprocess failure later.
    method: Literal["GFN2-xTB", "GFN1-xTB", "GFN0-xTB", "GFN-FF"] = "GFN2-xTB"
    solvent: str | None = None
    # xtb's numerical accuracy: smaller is tighter. Bounded rather than free, because an
    # accuracy of 1e-8 on a large molecule is a denial of service with extra steps.
    accuracy: float = Field(default=1.0, ge=0.01, le=10.0)


class ExpertXtbResult(BaseModel):
    """What an expert run produced, with the settings it actually ran under."""

    smiles: str
    task: str
    method: str
    solvent: str | None
    accuracy: float
    energy_hartree: float
    homo_lumo_gap_ev: float | None
    optimization: OptimizationSummary | None = None


@tool
async def run_xtb_task(request: ExpertXtbRequest) -> ExpertXtbResult:
    """Run an xTB calculation with a non-default method or accuracy (expert use only).

    Prefer the question-shaped tools — `compute_xtb_energy`, `optimize_geometry`,
    `compute_thermochemistry`, `compute_reaction_energy`. Reach for this one only when
    none of them expresses the calculation, which in practice means one of:

    - a different GFN parametrization: "GFN1-xTB" where GFN2 is known to behave badly for
      a system, "GFN0-xTB" or "GFN-FF" for something large enough that a full quantum
      treatment is not worth it (GFN-FF is a force field: no orbitals, no gap);
    - a tightened or loosened numerical accuracy.

    The result is not more trustworthy for having been requested this way. Every caveat
    of the shaped tools still applies, and a non-default method is *less* validated here
    than GFN2, not more — say which method produced a number when it was not the default.

    Args:
        request: The typed calculation request.

    Returns:
        The energy and HOMO-LUMO gap, plus the optimization summary when `task` is "opt".
    """
    authorize_trigger("run_xtb_task")
    structure = structure_from_smiles(request.smiles, multiplicity=None, optimize=True)
    if request.task == "opt":
        spec = OptSpec(method=request.method, solvent=request.solvent, engine=resolve_backend())
        optimization, _ = await run_cached_optimization(default_store(), structure, spec)
        return ExpertXtbResult(
            smiles=request.smiles,
            task=request.task,
            method=request.method,
            solvent=request.solvent,
            accuracy=request.accuracy,
            energy_hartree=optimization.energy_hartree,
            homo_lumo_gap_ev=None,
            optimization=OptimizationSummary.of(optimization),
        )
    outcome = xtb_cli.run(
        structure,
        task="sp",
        method=request.method,
        solvent=request.solvent,
        accuracy=request.accuracy,
    )
    gap = outcome.properties.get("HOMO-LUMO gap/eV")
    return ExpertXtbResult(
        smiles=request.smiles,
        task=request.task,
        method=request.method,
        solvent=request.solvent,
        accuracy=request.accuracy,
        energy_hartree=outcome.energy_hartree,
        homo_lumo_gap_ev=None if gap is None else float(gap),
    )
