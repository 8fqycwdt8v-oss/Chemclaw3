"""Deterministic step templates: a procedure whose order is fixed by a file, not by the model.

The counterpart to a profile: a profile lets the model decide the order of work; a template fixes
the order and lets the model fill the gaps. `README.md` says which to reach for.

- `manifest.py` — the validated contract (`Template`, the step kinds, reference checking).
- `resolve.py` — `${inputs.x}` / `${steps.id.result}` substitution, pure so replay is safe.
- `registry.py` — discovery, enablement, and the generated `run_<name>` launcher.

The durable half is `durable/template_job.py` (sequencer) and `durable/template_activities.py` (step
execution).
"""
