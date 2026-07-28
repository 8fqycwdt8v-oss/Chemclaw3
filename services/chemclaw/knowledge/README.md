# The knowledge graph

Markdown notes with YAML frontmatter, indexed into a NetworkX graph by `kg/graph.py`. Git is the
source of truth (D-004); agent-authored notes arrive through the PR-gate (D-005).

## What is here, and what it is for

This directory held only `.gitkeep`. `make kg-validate` passed because there was nothing to
validate, and every retrieval, crosslink and conflict property was measured against fixtures in
`evals/retrieval_corpus/` — correct for pinned eval numbers, and no substitute for a corpus with
real shape (STO-10).

These 37 notes are that corpus. They are **seed content, not a record of real experiments**: the
chemistry is textbook-ordinary and the numbers are illustrative. What is real is the *structure* —
every one of the ten `KNOWN_NOTE_TYPES` appears, every one of the fourteen `KNOWN_RELATIONS` is
used at least once, and the awkward cases have instances rather than descriptions:

- a **superseded pair** with a closed `valid_to` and a `superseded-by`/`supersedes` edge between
  them, so bi-temporal retrieval has something to exclude;
- a **declared conflict** — a `failure-mode` note that `contradicts` a BO recommendation, which is
  exactly the shape `kg/conflicts.py` exists to surface;
- **calculation crosslinks** — `job-result` notes carrying `calc_refs` and an `artifact_refs`
  entry pointing at a stored Hessian (STO-7);
- **multi-relation edges** — a compound that is both a precursor and a product elsewhere.

## Relationship to `evals/retrieval_corpus/`

They are separate on purpose and must stay separate. That directory's README states why: keeping
the gold corpus outside `knowledge_dir` is what makes recall/precision reproducible and independent
of whatever is in the live graph. These notes are written *alongside* it, sharing shape but not
files. A change here does not move a pinned eval number, and it should not.

## Conventions

- **State the purification with the yield.** An 88% after recrystallisation and an 88% after a plug
  are different numbers (`playbook-recrystallisation-purity`).
- **Type the edge when the relation is not just a citation.** `[[precursor-of:compound-x]]` is
  queryable; `[[compound-x]]` is a footnote. Both are valid; the first is more useful.
- **Record what failed.** `failure-mode` notes are the only place a negative result survives, and a
  negative result is often the more transferable one.

Run `make kg-validate` after any edit: it checks for dangling links, duplicate ids, unknown note
types, unknown relations, and the hazard gate.
