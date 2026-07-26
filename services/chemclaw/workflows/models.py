"""Typed payloads for the durable calculation jobs (plan Phase 1, xTB plan X3/X4).

These pydantic models are the single shared contract crossing two boundaries:
the MAF→Temporal boundary (the agent tool submits a `QMJobInput`) and the
activity boundary (handles and results passed between activities). One module so
no shape is duplicated between the agent, the workflow, and the activities.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from calc.complexes import InteractionResult
from calc.conformers import ConformerEnsemble
from calc.reaction import ReactionEnergyResult, SolventComparisonResult
from calc.xtb_scan import ScanResult
from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings
from chemclaw.ids import stable_hash


class QMJobInput(BaseModel):
    """A request to run a quantum-mechanical calculation on one molecule.

    `method`/`basis_set` name the QM level of theory (e.g. "B3LYP" / "def2-SVP").
    Kept as free strings: the valid set is a chemistry concern for a later Skill,
    not something to hardcode as an enum here.

    `requested_by` carries the caller's Entra ID object id (`oid`) for the audit
    trail (plan step 1.9). The submit tool populates it via `require_actor`
    (F4-T3): under Entra it is the authenticated user and a run without one is
    rejected before submission; in local dev it is the configured service
    identity. The field keeps a safe default so tests and system-triggered runs
    can construct the input without a tenant. Excluded from `qm_job_key`: the
    science does not depend on who asked, so identical work dedupes across users.
    """

    molecule_smiles: str = Field(min_length=1)
    method: str = Field(min_length=1)
    basis_set: str = Field(min_length=1)
    # Defaults to the configured service identity (not a magic "unknown"): a run constructed
    # without an actor — tests, system-triggered jobs — is attributed to `service_actor_id`,
    # the same fallback `require_actor` uses off the authenticated path (CON-1).
    requested_by: str = settings.service_actor_id
    # When true, the completed result is proposed as a PR-gated graph note (2.8).
    # Opt-in, so a calculation is only published to the graph when deliberately asked.
    publish_to_graph: bool = False
    # The conversation session to notify on completion (plan F3-T3), stamped from the turn's
    # ambient context (`agents.session_context`) at submit, never by the model. `None` off the
    # front-door path (tests/CLI) — then the job simply records no push-back. Deliberately excluded
    # from `qm_job_key`: identical science is still deduplicated across sessions (D-011), so a
    # completion notifies the session that actually started the workflow.
    session_id: str | None = None


def qm_job_key(job: QMJobInput) -> str:
    """Stable identity of a QM calculation: molecule + method + basis only.

    The SMILES is canonicalized first, so two spellings of the same molecule
    (`"CCO"` / `"OCC"`) share one workflow id and one cache entry rather than
    running the calculation twice (D-011). Raises `InvalidSmilesError` on an
    unparseable structure, so a malformed request is rejected at the durable
    boundary (G4) instead of flowing through the pipeline into a stored result.

    Deliberately excludes `requested_by` — the result of a calculation does not
    depend on who asked for it, so identical science shares one workflow id and
    one cache entry across users. Used for the workflow id (dedup), the mock
    scheduler handle, and the result cache key (plan step 1.10). One definition,
    three callers. Shares `chemclaw.ids.stable_hash` (SHA-256) with every other
    identity key in the system.

    Includes the HPC pipeline version **only when one is configured** (plan F5-T3):
    a real pipeline update changes the numbers, so it must be a cache *miss*, not a
    stale hit (D-011/D-033). An empty version (the mock/dev default) leaves the key
    byte-identical to before F5, so existing cached results and ids are unaffected.
    """
    payload = {
        "smiles": require_canonical_smiles(job.molecule_smiles),
        "method": job.method,
        "basis_set": job.basis_set,
    }
    if settings.hpc_pipeline_version:
        payload["pipeline_version"] = settings.hpc_pipeline_version
    return stable_hash(payload)


class HpcJobHandle(BaseModel):
    """Opaque handle the (mock) scheduler returns from submit, used to poll.

    A real backend would carry the SLURM job id and cluster; the mock carries a
    deterministic id derived from the input so runs are reproducible in tests.
    """

    scheduler_job_id: str = Field(min_length=1)


class QMJobResult(BaseModel):
    """Structured result parsed from the (mock) HPC output (plan step 1.4).

    Echoes the identifying inputs so a stored result is self-describing when it
    later becomes a knowledge-graph note (Phase 2).
    """

    molecule_smiles: str
    method: str
    basis_set: str
    total_energy_hartree: float
    converged: bool
    # Provenance for the audit trail (mirrors QMJobInput.requested_by).
    requested_by: str


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
    `calc.xtb_spec`, so two different jobs sharing a species still share that work. This
    key exists only to make submit idempotent — the same request while it is running, or
    after it finished, returns the existing job rather than starting a second one.
    """
    return stable_hash(job.spec.model_dump())


class XtbJobResult(BaseModel):
    """The outcome of a durable xTB job: exactly one of the three result shapes.

    Three optional fields rather than a union, because each result model is a rich
    domain type with no field in common to discriminate on, and a wrong smart-union
    match would be a silent data corruption rather than a loud error.
    """

    kind: str
    summary: str
    reaction: ReactionEnergyResult | None = None
    solvents: SolventComparisonResult | None = None
    scan: ScanResult | None = None
    ensemble: ConformerEnsemble | None = None
    interaction: InteractionResult | None = None


class JobStatus(BaseModel):
    """A non-blocking status view of any durable calculation job (plan 1.6, xTB X3/X4).

    One model across both job kinds, because "how is my calculation doing" is one
    question and the agent should not have to know which engine answered it. `status` is
    Temporal's execution status name (RUNNING/COMPLETED/FAILED/…); exactly one of the
    result fields is populated, and only once the job has completed.

    Two typed fields rather than one polymorphic one: a QM result and an xTB result are
    genuinely different shapes, and naming both keeps the model reading a field whose
    contents it can predict from the name.
    """

    job_id: str
    kind: Literal["qm", "xtb"]
    status: str
    qm_result: QMJobResult | None = None
    xtb_result: XtbJobResult | None = None
