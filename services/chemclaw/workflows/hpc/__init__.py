"""The real HPC execution backends behind the QM activities (plan Phase F5).

The QM workflow is untouched: only `workflows/activities.py` dispatches on `hpc_launch_interface`
to either the in-process mock (kept for CI/local, no cluster) or the `nextflow` adapter here. The
adapter speaks the launcher's REST API and is exercised offline against a fake HTTP transport.
"""
