# `results` — republishing computed results on demand

**Ordinary publishing is not here.** Every calculation is projected and queued for the external
results store as it completes (`D-2026-08-25-a-cache-is-not-a-record`), automatically and with no
tool call — a chemist never has to remember to save a result, and the audit story is stronger when
they cannot forget.

What this bundle adds is the *deliberate* act, of which there are two:

- a store has just been attached to a deployment that has been running, so the corpus behind it —
  everything in `calculation_results` and `job_records`, neither of which is ever pruned — needs
  queueing;
- a destination that was refusing deliveries has been fixed, so publications that exhausted their
  retries should go back in the queue rather than be re-derived.

## Why a job and not a tool

A tool would be refused. `cli/validate_connectors.py` bans a `write_`/`submit_`/`update_`-prefixed
name on an endpoint outright — *"the agent-facing surface is read/compute only"* — and this
capability is a write. A job is the sanctioned shape, and it inherits everything that rule exists to
protect, with no code in this bundle: `require_actor()`, the expensive-action gate, a mandatory
`rationale`, the plan gate, dry-run refusal, session push-back and a `job_records` row, all through
`ConnectorJobWorkflow`.

`expensive: true` because the cost is bounded by the data rather than by the request — the same
reason `sample_conformers` carries the gate.

## The operator's route

The same walk, from a terminal, with a dry run the job deliberately does not offer:

```
python -m chemclaw.cli.backfill_publications --dry-run
python -m chemclaw.cli.backfill_publications --requeue
```

The workflow calls that module rather than reimplementing it: an operator and a chemist must cover
exactly the same rows, and two walks that agreed today would diverge on the next table.
