"""What an xTB job may be asked to do — the request half of this bundle's durable contract.

**A leaf module, and that is the whole point.** These models are named by `connector.yaml`'s
`params_model`, and `connectors/jobs.py` resolves that name by *importing* it — on every
`build_agent`, in the chat service's own process, and again in `make connector-validate`. So
whatever this module imports, the chat service imports too.

They used to live in `connectors/calc/specs.py`, which imports `chemclaw.science.calc.complexes`,
`chemclaw.science.calc.conformers`,
`chemclaw.science.calc.reaction` and `chemclaw.science.calc.xtb_scan` for the *result* types
alongside them. Measured on `main`,
building the enabled job tools pulled **`tblite` and fifteen `calc.*` modules** into the agent's
process — the entire quantum-chemistry closure this bundle exists to keep out of it (D-114), let
back in through the one manifest field that resolves an import. Nothing failed; the chat pod simply
carried a compiled QM library it never called.

So the split here is not stylistic. Requests live in this module and import pydantic and config
only; results live in `connectors/calc/results.py`, which is free to import the heavy `calc.*`
types because only this bundle's own worker ever imports it. `cli/validate_connectors.py`
enforces the boundary rather than trusting it, and `tests/test_connector_isolation.py` asserts it
in a fresh interpreter (D-118).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash


class ReactionJobSpec(BaseModel):
    """A durable reaction-energy request (xTB plan X4)."""

    kind: Literal["reaction"] = "reaction"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvent: str | None = None
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"


class SolventScreenJobSpec(BaseModel):
    """A durable solvent-comparison request over one reaction (xTB plan X4)."""

    kind: Literal["solvents"] = "solvents"
    reactants: list[str] = Field(min_length=1)
    products: list[str] = Field(min_length=1)
    solvents: list[str] = Field(min_length=1)
    temperature_k: float | None = None
    level: Literal["quick", "standard", "thorough"] = "standard"


class ScanJobSpec(BaseModel):
    """A durable relaxed-scan request along one internal coordinate (xTB plan X3)."""

    kind: Literal["scan"] = "scan"
    smiles: str = Field(min_length=1)
    atoms: list[int] = Field(min_length=2, max_length=4)
    values: list[float] = Field(min_length=2)
    solvent: str | None = None


class EnsembleJobSpec(BaseModel):
    """A durable conformer/tautomer/protomer search request (xTB plan X6)."""

    kind: Literal["ensemble"] = "ensemble"
    smiles: str = Field(min_length=1)
    search: Literal["conformers", "tautomers", "protomers", "deprotomers"] = "conformers"
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"


class ComplexJobSpec(BaseModel):
    """A durable non-covalent complex search over two molecules (xTB plan X11)."""

    kind: Literal["complex"] = "complex"
    smiles_a: str = Field(min_length=1)
    smiles_b: str = Field(min_length=1)
    solvent: str | None = None
    effort: Literal["quick", "normal", "extensive"] = "quick"


# What an xTB job may be asked to do, discriminated on `kind`. A closed, typed union
# rather than a free-form request is the same boundary rule the proposal sets for the
# expert escape hatch: a model-authored payload can select among calculations we
# defined, and can never describe one we did not.
XtbJobSpec = Annotated[
    ReactionJobSpec | SolventScreenJobSpec | ScanJobSpec | EnsembleJobSpec | ComplexJobSpec,
    Field(discriminator="kind"),
]


class XtbJobInput(BaseModel):
    """A request to run an expensive xTB task as a durable job (xTB plan X3/X4).

    The same shape as `QMJobInput` for the fields that are about *who asked* rather than
    *what to compute*: `requested_by` for the audit trail, `session_id` for the
    completion push-back, both stamped from the turn's ambient context at submit and
    never supplied by the model. Excluded from `xtb_job_key` for the same reason —
    identical science dedupes across users and sessions (D-011).
    """

    spec: XtbJobSpec
    requested_by: str = settings.service_actor_id
    session_id: str | None = None


def xtb_job_key(job: XtbJobInput) -> str:
    """Stable identity of an xTB job: its spec alone.

    Note what this key is *not*: it is not the calculation cache key. Each species,
    optimization and Hessian inside the job is separately content-addressed by
    `chemclaw.science.calc.xtb_spec`, so two different jobs sharing a species still share that
    work. This
    key exists only to make submit idempotent — the same request while it is running, or
    after it finished, returns the existing job rather than starting a second one.
    """
    return stable_hash(job.spec.model_dump())
