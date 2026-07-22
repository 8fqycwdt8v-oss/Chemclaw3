"""The HPC/Nextflow identity bridge (plan F4-T6, §7.2): the second non-Entra transport.

HPC/Nextflow is not an Entra relying party, so a user's job cannot run under the user's own Entra
identity there. Instead every user job runs under **one shared HPC service identity**, while the
requesting Entra `oid` is carried inside the workflow payload (`requested_by`, F4-T3) — and *every*
oid→HPC-identity mapping is logged, so the audit trail can always answer "which real user drove this
HPC run" even though the cluster only ever saw the service identity. Logging the mapping is the
compliance requirement (§7.2), not an implementation detail.
"""

import logging

from chemclaw.config import settings

_logger = logging.getLogger(__name__)


def map_to_hpc_identity(entra_oid: str) -> str:
    """Map a requesting Entra `oid` to the HPC service identity, logging the mapping.

    Args:
        entra_oid: The requesting user's Entra object id (the workflow's `requested_by`).

    Returns:
        The shared HPC service identity (`settings.hpc_bridge_identity`) the job runs under.
    """
    hpc_identity = settings.hpc_bridge_identity
    # The audit record: the cluster sees only `hpc_identity`, so this line is the sole link back to
    # the real user. Logged at INFO like the tool-audit trail, for the same GxP reason.
    _logger.info("hpc identity bridge: entra oid %s -> hpc identity %s", entra_oid, hpc_identity)
    return hpc_identity
