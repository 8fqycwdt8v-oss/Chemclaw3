"""Activities for the durable conformer-ensemble job — research follow-up, D-092.

Two steps, mirroring `workflows.activities`'s validate-then-compute shape: `prepare_conformer_input`
is the durable-boundary validation gate (G4, canonicalizes the SMILES so a malformed request fails
fast rather than flowing into the ensemble); `run_conformer_ensemble` is the heavy, CPU-bound
compute (RDKit ETKDG embedding + tblite GFN2-xTB per conformer), offloaded to a worker thread so
the event loop and other activities keep flowing — the same discipline `calc.store.run_cached`
uses for the inline calculators.
"""

import asyncio

from temporalio import activity

from calc.conformer_ensemble import (
    ConformerEnsembleInput,
    ConformerEnsembleResult,
    compute_conformer_ensemble,
)
from chemclaw.chem import require_canonical_smiles
from workflows.conformer_models import ConformerJobInput
from workflows.registry import durable_activity


@durable_activity("background")
@activity.defn
async def prepare_conformer_input(job: ConformerJobInput) -> ConformerJobInput:
    """Validate and canonicalize the request before the ensemble runs (mirrors `prepare_input`).

    Rejects an unparseable SMILES here, at the durable boundary, rather than deep inside RDKit's
    embedding step — and canonicalizes so the same molecule always yields the same downstream
    workflow id / result (D-011).
    """
    smiles = require_canonical_smiles(job.molecule_smiles)
    return job.model_copy(update={"molecule_smiles": smiles})


@durable_activity("background")
@activity.defn
async def run_conformer_ensemble(job: ConformerJobInput) -> ConformerEnsembleResult:
    """Run the Boltzmann-weighted GFN2-xTB ensemble for one already-validated molecule."""
    return await asyncio.to_thread(
        compute_conformer_ensemble,
        ConformerEnsembleInput(smiles=job.molecule_smiles, charge=job.charge),
    )
