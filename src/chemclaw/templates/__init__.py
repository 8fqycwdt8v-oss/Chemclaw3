"""Deterministic step templates: a procedure whose order is fixed by a file, not by the model.

The counterpart to a profile (`agent/profile_discovery.py`). A profile configures an agent and lets
the model decide the order of work; a template fixes the order and lets the model fill the gaps.
See `src/chemclaw/templates/README.md` for which to reach for — using the wrong one is the common
mistake.

- `manifest.py` — the validated contract (`Template`, the three step kinds, reference checking).
- `resolve.py` — the `${inputs.x}` / `${steps.id.result}` substitution, pure so replay is safe.
- `registry.py` — discovery, enablement, and the generated `run_<name>` launcher.

The durable half lives in `durable/template_job.py` (the sequencer) and
`durable/template_activities.py` (the step execution).
"""
