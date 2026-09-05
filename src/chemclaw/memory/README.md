# `chemclaw.memory` — what past work taught us

**Responsibility:** the memory layers over completed work, built entirely from pieces that already
existed — fingerprint-keyed structural identity, the canonical reaction schema, and the one note-write path. No
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

The threshold is what separates the two tiers, and since
`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` it is the *only* thing that does: an
observation that crosses both promotion thresholds becomes an ordinary playbook note, written
straight into `knowledge/` like every other agent-authored note. Two rules keep that safe and both
are enforced rather than documented: support counts distinct *cited runs* — `reaction-<id>`
references into the transcription store since D-2026-08-25, plus the `interaction` notes — and an
observation can never corroborate itself into a promotion (migration `025` makes the self-reference
a CHECK). The promoted note says which kind of evidence the count is, because nobody reads it before
a chemist does at the point of use. And an observation never enters the evidence list
(`recall_observations` is its own tool, not a bucket inside `gather_evidence`).

## Nothing here writes to the graph directly

Everything that reaches `knowledge/` goes through `kg.record.record_note`, the one write path, so a
promoted playbook is subject to the same path validation, the same `created_by: agent` stamp and
the same write ordering as anything else. What this package must not do is hold a second way in.

## The boundary against `retrieval/`

See `retrieval/README.md`: retrieval finds what we *have*, memory holds what we *learned*.
Deliberately separate packages (D-156).
