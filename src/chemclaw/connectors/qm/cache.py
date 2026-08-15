"""How a finished QM calculation is addressed in the shared calculation store (D-158).

**Why this bundle needs it at all.** `calc` writes every result it computes into
`calculation_results` and never recomputes one (D-011); `qm` wrote nothing. So a two-second xTB
energy was cached forever while a multi-hour DFT run on the cluster was cached nowhere — its only
durable homes were Temporal's event history and a note that exists *only if a human merges the PR*.
Both are conditional, and the deduplication that did exist (a deterministic workflow id) holds only
while Temporal retains that execution, so after retention rolled over the byte-identical request
re-ran the whole cluster job. The economics were exactly inverted.

**Why the key is built here and not in `specs.py`.** That module is a leaf by contract — the chat
service imports it on every `build_langgraph_agent` to resolve `connector.yaml`'s `params_model`, so
it may not reach a compiled library or a database (`tests/test_connector_isolation.py` asserts it in
a fresh interpreter, D-118). `CalculationKey` lives in `chemclaw.science.calc.store`, which is on
the wrong side of that line. This module is imported only by the bundle's own activity, which
already runs on the bundle's own worker.

**What goes where in the key, and why it is not `qm_job_key`.** `qm_job_key` is a bare 16-character
digest — it is not a `calc_refs`-valid reference (`chemclaw.kg.note._CALC_REF` wants the four-part
`type@version:hash:hash` form), and it folds molecule, method, basis and pipeline version into one
opaque hash. The store's own convention splits them, so this follows `calc`: the **molecule** is the
input, the **method and basis set** are parameters (as `run_cached_pka` treats its embedding seed),
and the **backend plus pipeline version** is the calculator version, because those are the things
whose change makes a stored number stale — see `calc_version` for why the backend belongs there.

The pipeline version is also carried in the parameters, which is what makes the readable
`calc_version` slug safe: `method`/`basis_set` are free-text fields the model authors and
`hpc_pipeline_version` is operator config, so any of them may contain a space or a colon that the
reference format forbids. Sanitizing for display could in principle map two distinct pipelines onto
one slug; including the raw value in `params_hash` means the *key* stays distinct even when the slug
does not, so a sanitization collision can never become a wrong cache hit.
"""

import re

from chemclaw.connectors.qm.specs import QmJobSpec
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.science.calc.store import CalculationKey

# The store's `calc_type` for a quantum-mechanical (DFT) calculation. A free string by design —
# `calc_type` has no registry or enum anywhere, so this is the whole registration.
CALC_TYPE = "dft"

# What a deployment that has not pinned a pipeline version records instead. `qm_job_key` already
# omits the version when it is unset, so this preserves that behaviour rather than inventing a
# second rule: without a configured pipeline there is nothing to invalidate a result against.
#
# Reachable under `mock` only: `Settings._hpc_launch_config` refuses an empty `hpc_pipeline_version`
# once the interface is `nextflow`, precisely so two real pipelines cannot both land here and share
# each other's energies.
UNVERSIONED = "unversioned"

# `chemclaw.kg.note._CALC_REF` forbids whitespace and `:` inside a version, because both are
# separators in the flat reference form. Collapse either into a single dash.
_UNSAFE_IN_VERSION = re.compile(r"[\s:]+")


def version_slug(raw: str) -> str:
    """Render a pipeline version safe for the flat `calc_refs` reference form.

    Returns `UNVERSIONED` for an unset (or whitespace-only) version, so the slug is never empty —
    an empty `calc_version` would produce `dft@:…`, which no reference can round-trip.
    """
    slug = _UNSAFE_IN_VERSION.sub("-", raw.strip())
    return slug or UNVERSIONED


def calc_version() -> str:
    """What actually computed a QM row — the backend that ran it, and the pipeline it ran.

    **The backend is part of the version, not merely a parameter, for the reason
    `XtbSpec.calc_version` states: a key that named the wrong program is a key that survives an
    upgrade to the right one (D-011).** `mock` and `nextflow` are not two configurations of one
    calculator. `nextflow` runs DFT on a cluster; `mock` synthesizes an energy from the hex digits
    of a job id (`activities.poll_hpc_status`). With only the pipeline version in the key, a
    deployment that ran the mock and then pointed at a real cluster served that fabricated number
    as a cache hit — carrying a `calc_refs` provenance stamp that says a DFT calculation produced
    it. Naming the backend is what makes the two families of rows unable to reach each other.

    Unlike the free-text pipeline version, `hpc_launch_interface` is a two-value `Literal`, so it
    can contain nothing the reference form forbids and no two values can collide under
    `version_slug`. It therefore needs no second, raw copy in `params`.
    """
    return f"{settings.hpc_launch_interface}-{version_slug(settings.hpc_pipeline_version)}"


def calculation_key(spec: QmJobSpec) -> CalculationKey:
    """The calculation-store identity of one QM job.

    The SMILES is canonicalized first, so two spellings of the same molecule share one entry —
    the same normalization `qm_job_key` and `prepare_input` already apply, so the note id and the
    store key agree on what counts as the same calculation. Raises `InvalidSmilesError` on an
    unparseable structure.
    """
    return CalculationKey.build(
        calc_type=CALC_TYPE,
        calc_version=calc_version(),
        inputs={"smiles": require_canonical_smiles(spec.molecule_smiles)},
        params={
            "method": spec.method,
            "basis_set": spec.basis_set,
            # The raw value, not the slug: this is the half that guarantees distinct pipelines
            # never share a key (see the module docstring).
            "pipeline_version": settings.hpc_pipeline_version or None,
        },
    )
