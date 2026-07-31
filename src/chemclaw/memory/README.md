# `chemclaw.memory` — what past work taught us

**Responsibility:** the memory layers over completed work, built entirely from pieces that already
existed — fingerprint-keyed structural identity, the canonical reaction schema, and the PR-gate. No
new infrastructure, by design.

- **Episodic** — `campaign.py` and `chains.py` chain experiments where one reaction's product is
  another's reactant, so a synthesis route is recoverable as a route rather than as loose notes.
- **Semantic** — `failure.py` (failure modes), `playbook.py` (distilled procedure), `optimization.py`
  (what a BO campaign converged on), `interaction.py` (what a human decided and why).
- **Plumbing** — `similarity.py` (structural identity via DRFP), `ids.py`, `supersede.py` (a newer
  finding retiring an older one without deleting it), `jobs.py` (the durable side).

## Nothing here writes to the graph directly

A distilled playbook is a *proposal*. It reaches `knowledge/` the same way every other
agent-generated note does — through `kg.pr_gate`, as a pull request a human merges. Memory that
wrote itself into the record would be exactly the thing the GxP line exists to prevent.

## The boundary against `retrieval/`

See `../retrieval/README.md`: retrieval finds what we *have*, memory holds what we *learned*.
Deliberately separate packages (D-154).
