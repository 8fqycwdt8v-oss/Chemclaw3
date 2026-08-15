"""What a QM/DFT job may be asked to do — the request half of this bundle's durable contract.

**A leaf module, and that is the whole point.** `QmJobSpec` is named by `connector.yaml`'s
`params_model`, and `connectors/jobs.py` resolves that name by *importing* it — on every
`build_langgraph_agent`, in the chat service's own process, and again in `make connector-validate`.
So whatever this module imports, the chat service imports too, which is why the split here follows
`connectors/calc/specs.py`: pydantic, `chemclaw.core.config`, `chemclaw.core.chem` and
`chemclaw.core.ids` only,
and nothing that reaches the HPC launcher, the knowledge graph or a compiled chemistry library
(`tests/test_connector_isolation.py` asserts it in a fresh interpreter, D-118).

Two models rather than one, because the two boundaries carry different things. `QmJobSpec` crosses
the *model* boundary: it is exactly what the LLM may author. `QMJobInput` crosses the *activity*
boundary and adds the requesting actor, which is ambient to the turn and must never be
model-supplied — the same asymmetry `ConnectorJobInput.requested_by` has one level up. Subclassing
keeps the scientific fields defined once, so the two cannot drift apart.
"""

from pydantic import BaseModel, Field

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash


class QmJobSpec(BaseModel):
    """A request to run a quantum-mechanical calculation on one molecule.

    `method`/`basis_set` name the QM level of theory (e.g. "B3LYP" / "def2-SVP"). Kept as free
    strings: the valid set is a chemistry judgment the `qm-job-submission` skill holds, not
    something to freeze as an enum here.

    Every field carries a description because this model *is* the JSON schema the LLM fills in — a
    referenced `params_model` documents its own fields (there is no `params:` prose to fall back
    on), so an undescribed one is an argument the model has to guess at.
    """

    molecule_smiles: str = Field(min_length=1, description="The molecule as a SMILES string.")
    method: str = Field(min_length=1, description='QM method / level of theory, e.g. "B3LYP".')
    basis_set: str = Field(min_length=1, description='Basis set, e.g. "def2-SVP".')


class QMJobInput(QmJobSpec):
    """The spec plus the identity the HPC side has to record — what the activities receive.

    `requested_by` carries the caller's Entra ID object id (`oid`) for the audit trail (plan step
    1.9) and travels to the real launcher, which submits under the shared HPC *service* identity:
    without it a cluster run could not be attributed to the chemist who asked for it.

    It is deliberately not on `QmJobSpec`, so the model cannot author it. It reaches the workflow on
    the run's memo, stamped by `ConnectorJobWorkflow` from the actor `require_actor` demanded at the
    tool boundary (F4-T3). The default keeps the configured service identity (not a magic
    "unknown") for a run constructed without an actor — tests, system-triggered jobs — matching the
    fallback `require_actor` itself uses off the authenticated path (CON-1).
    """

    requested_by: str = settings.service_actor_id


def qm_job_key(spec: QmJobSpec) -> str:
    """Stable identity of a QM calculation: molecule + method + basis only.

    The SMILES is canonicalized first, so two spellings of the same molecule (`"CCO"` / `"OCC"`)
    share one cache entry and one note rather than running the calculation twice (D-011). Raises
    `InvalidSmilesError` on an unparseable structure, so a malformed request is rejected at the
    durable boundary (G4) instead of flowing through the pipeline into a stored result.

    Deliberately excludes `requested_by` — the result of a calculation does not depend on who asked
    for it, so identical science shares one key across users. Used for the mock scheduler handle,
    the Nextflow run name, and the result note's id. One definition, three callers. Shares
    `chemclaw.core.ids.stable_hash` (SHA-256) with every other identity key in the system.

    Includes the HPC pipeline version **only when one is configured** (plan F5-T3): a real pipeline
    update changes the numbers, so it must be a cache *miss*, not a stale hit (D-011/D-033).
    """
    payload = {
        "smiles": require_canonical_smiles(spec.molecule_smiles),
        "method": spec.method,
        "basis_set": spec.basis_set,
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
    """Structured result parsed from the HPC output (plan step 1.4).

    Echoes the identifying inputs so a stored result is self-describing once it becomes the
    knowledge-graph note this bundle hands back for core to PR-gate.
    """

    molecule_smiles: str
    method: str
    basis_set: str
    total_energy_hartree: float
    converged: bool
    # Provenance for the audit trail (mirrors QMJobInput.requested_by).
    requested_by: str


class QmCacheLookup(BaseModel):
    """What the calculation store holds for one job: its key, and the result if already computed.

    The key travels even on a miss, so the workflow has one shape to handle either way and never
    re-derives it — which it could not do anyway, since deriving the key canonicalizes through
    RDKit and the workflow has to stay deterministic and sandbox-safe.

    `calc_key` is empty when persistence is switched off, which every consumer already reads as
    "no reference to record" (`note_from_qm_result`). Defined after `QMJobResult` rather than beside
    the other request models because it *contains* one — no forward reference to rebuild.
    """

    calc_key: str = ""
    result: QMJobResult | None = None
