# The knowledge graph

Markdown notes with YAML frontmatter, indexed into a NetworkX graph by `kg/graph.py`. Git is the
source of truth (D-004); agent-authored notes arrive through the PR-gate (D-005).

## What is here, and what it is for

This directory held only `.gitkeep`. `make kg-validate` passed because there was nothing to
validate, and every retrieval, crosslink and conflict property was measured against fixtures in
`evals/retrieval_corpus/` — correct for pinned eval numbers, and no substitute for a corpus with
real shape (STO-10).

These 38 notes are that corpus. They are **seed content, not a record of real experiments**: the
chemistry is textbook-ordinary and the numbers are illustrative. What is real is the *structure* —
every one of the eleven `KNOWN_NOTE_TYPES` appears, every one of the fifteen `KNOWN_RELATIONS` is
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

## What a deployment actually serves

These notes ship *in the image*, and `deploy/knowledge-sync.sh` publishes into the directory the
app reads with `rsync -a --delete`. So:

- **`knowledge.sync.repoUrl` empty** (the chart default) — a pod serves this corpus. That is what
  makes a dev or demo deployment useful now: a graph with real shape rather than one that validates
  because it is empty.
- **`repoUrl` set** — the sidecar publishes the remote branch's `knowledge/` subtree, which
  **replaces** this corpus on the first sync. A deployment that wants to keep these notes has to
  commit them into its own knowledge repository.

The second case is the right default — the remote is the source of truth, and a pod quietly merging
image content into a curated corpus would be worse — but `--delete` makes it silent, so it is worth
knowing before it happens rather than after.

## Conventions

- **State the purification with the yield.** An 88% after recrystallisation and an 88% after a plug
  are different numbers (`playbook-recrystallisation-purity`).
- **Type the edge when the relation is not just a citation.** `[[precursor-of:compound-x]]` is
  queryable; `[[compound-x]]` is a footnote. Both are valid; the first is more useful.
- **Record what failed.** `failure-mode` notes are the only place a negative result survives, and a
  negative result is often the more transferable one.

Run `make kg-validate` after any edit: it checks for dangling links, duplicate ids, unknown note
types, unknown relations, and the hazard gate.
