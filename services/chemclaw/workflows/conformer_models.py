"""Typed payloads for the durable conformer-ensemble job — research follow-up, D-092.

Mirrors `workflows.models`'s QM-job shape (one module shared by the MAF->Temporal boundary and
the activity boundary), narrowed to what this job actually needs: no `publish_to_graph` — like
the fast calculators (`calc.pka`, `calc.solubility`) this is a screening/diagnostic number, not a
result the knowledge graph's PR-gate is meant to hold, so that field is simply absent rather than
wired-but-always-false.
"""

from pydantic import BaseModel, Field

from calc.conformer_ensemble import ConformerEnsembleResult
from chemclaw.chem import require_canonical_smiles
from chemclaw.config import settings
from chemclaw.ids import stable_hash


class ConformerJobInput(BaseModel):
    """A request to run a Boltzmann-weighted GFN2-xTB conformer ensemble on one molecule.

    `requested_by`/`session_id` mirror `workflows.models.QMJobInput` exactly (same audit-trail
    and push-back-notification contract); see there for why each defaults the way it does.
    """

    molecule_smiles: str = Field(min_length=1)
    charge: int = 0
    requested_by: str = settings.service_actor_id
    session_id: str | None = None


def conformer_job_key(job: ConformerJobInput) -> str:
    """Stable identity of a conformer-ensemble job: molecule + charge + the science knobs.

    Unlike `workflows.models.qm_job_key` (whose method/basis are already part of the request), the
    ensemble size, energy window, Boltzmann temperature, embedding seed, and xTB method are
    read from ambient config rather than the request — so they must be part of the key (D-011):
    otherwise re-tuning `conformer_ensemble_size` between two submissions of the same molecule
    would silently return a workflow/result keyed under the *old* setting. Deliberately excludes
    `requested_by`/`session_id` — identical science is deduplicated across users and sessions,
    exactly as the QM job does.
    """
    payload = {
        "smiles": require_canonical_smiles(job.molecule_smiles),
        "charge": job.charge,
        "xtb_method": settings.xtb_method,
        "embed_seed": settings.xtb_embed_seed,
        "ensemble_size": settings.conformer_ensemble_size,
        "energy_window_kcal": settings.conformer_energy_window_kcal,
        "boltzmann_temperature_kelvin": settings.conformer_boltzmann_temperature_kelvin,
    }
    return stable_hash(payload)


class ConformerJobResult(BaseModel):
    """The completed job's ensemble result plus the audit-trail provenance field."""

    ensemble: ConformerEnsembleResult
    requested_by: str


class ConformerJobStatus(BaseModel):
    """A non-blocking status view of a submitted conformer job, mirroring `QMJobStatus`."""

    job_id: str
    status: str
    result: ConformerJobResult | None = None
