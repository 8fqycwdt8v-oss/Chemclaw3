"""The real HPC execution backends behind the QM activities (plan Phase F5).

The QM workflow is untouched: only `connectors/qm/activities.py` dispatches on
`hpc_launch_interface` to either the in-process mock (kept for CI/local, no cluster) or the
`nextflow` adapter here. The adapter speaks the launcher's REST API and is exercised offline
against a fake HTTP transport.

It lives inside the `qm` bundle because the launcher credential and the artifact store are this
capability's own dependencies — nothing outside this bundle's worker may reach them (D-118).
"""
