# `chemclaw.memory` — what past work taught us

**Responsibility:** the memory layers over completed work, built entirely from pieces that already
existed — fingerprint-keyed structural identity, the canonical reaction schema, and the PR-gate. No
new infrastructure, by design.

- **Episodic** — `campaign.py` and `chains.py` chain experiments where one reaction's product is
  another's reactant, so a synthesis route is recoverable as a route rather than as loose notes.
- **Semantic** — `failure.py` (failure modes), `playbook.py` (distilled procedure), `optimization.py`
  (what a BO campaign converged on), `interaction.py` (what a human decided and why).
- **Plumbing** — `similarity.py` (structural identity via DRFP), `ids.py`, `supersede.py` (a newer
  finding retiring an older one without deleting it), `jobs.py` (the durable side),
  `progression.py` (the order runs were performed in and what each changed).
- **The comparative table** — `comparison.py`. Cells, the empty-column rule and the grid, extracted
  from `optimization.py` when `agent/condense.py` became a second caller. It lives here rather
  than there because this is where the artifact was invented; two copies would be two tables that
  disagree about what `—` means.

## The one thing here that is not a note

`observations.py` and `observation_mining.py` are the ungated tier (D-161), and they are the
exception that proves the rule below: an observation is stored in **Postgres, not Git**, because it
is explicitly not truth. "This transformation has gone badly in three projects" is worth noticing
and is not worth a reviewer's PR — and it is something the graph will never hold, since a playbook
may only be distilled from successes.

The human gate does not disappear; it moves. An observation that crosses both promotion thresholds
opens one ordinary playbook PR through the ordinary `kg.pr_gate`. Two rules keep that safe and both
are enforced rather than documented: support counts distinct *merged* notes (so an observation can
never corroborate itself into a promotion — migration `025` makes that a CHECK), and an observation
never enters the evidence list (`recall_observations` is its own tool, not a bucket inside
`gather_evidence`).

## Nothing here writes to the graph directly

A distilled playbook is a *proposal*. It reaches `knowledge/` the same way every other
agent-generated note does — through `kg.pr_gate`, as a pull request a human merges. Memory that
wrote itself into the record would be exactly the thing the review line exists to prevent.

## The boundary against `retrieval/`

See `../retrieval/README.md`: retrieval finds what we *have*, memory holds what we *learned*.
Deliberately separate packages (D-156).
