# The knowledge graph

Markdown notes with YAML frontmatter, indexed into a NetworkX graph by `kg/graph.py`. Git is the
source of truth (D-004); agent-authored notes arrive through the PR-gate (D-005).

## What is here, and what it is for

This directory once held only `.gitkeep` — `make kg-validate` passed because there was nothing to
validate, and every retrieval, crosslink and conflict property was measured against fixtures in
`evals/retrieval_corpus/`. Correct for pinned eval numbers, and no substitute for a corpus with
real shape (STO-10).

These notes are that corpus. They are **seed content, not a record of real experiments**: the
chemistry is textbook-ordinary and the numbers and dates are illustrative. What is real is the
*structure* — every note type in the effective vocabulary (`known_note_types()`: core's
`KNOWN_NOTE_TYPES` plus what the enabled bundles declare — `bo-candidate` comes from the `bo`
bundle, so a deployment that disables `bo` must also drop `bo-candidate/` from its corpus) appears,
every relation in `known_relations()` is used at least once **in its declared direction**
(`kg/relations.py::RELATION_SIGNATURES` — the validator refuses an inverted edge), and the awkward
cases have instances rather than descriptions:

- a **superseded pair** with a closed `valid_to` and a `superseded-by`/`supersedes` edge between
  them, so bi-temporal retrieval has something to exclude;
- a **declared conflict** — a `failure-mode` note that `contradicts` a BO recommendation, which is
  exactly the shape `kg/conflicts.py` exists to surface;
- **calculation crosslinks** — `job-result` notes carrying `calc_refs` and an `artifact_refs`
  entry pointing at a stored Hessian (STO-7);
- a **multi-relation edge** — `compound-acetic-anhydride` stands in two relations to one run
  (`reagent-in` and `solvent-for` `rxn-aspirin-acetylation`: the neat anhydride is both the
  reagent and the medium), which is the case `note.cited_links` dedupes on the *pair* for.

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

## Authoring a note

There is no separate template file; the corpus is the template, and these are the rules the
validator cannot fully check. A note is `<type>/<id>.md` — the directory **must** match the
frontmatter `type` and the filename the `id` (both validator-enforced). Frontmatter keys are
closed: a key `Note` does not declare is a refused note, not ignored metadata, so a typo fails
loudly at the gate.

Per type, the fields that make a note useful rather than merely valid:

- **`reaction`** — `conditions:` with every figure the prose states (`temperature_c`, `time_h`,
  `yield_percent`, `outcome`), `valid_from:` as the date the run was performed (D-162), plain
  `[[links]]` to its participants, and `[[part-of:campaign-…]]` when it belongs to one. Typed
  participant edges (`precursor-of`, `product-of`, `catalyzes`, `solvent-for`, `reagent-in`) run
  **from the compound notes toward the reaction/compound** — see `RELATION_SIGNATURES`; writing
  them from the reaction is the inversion the validator refuses.
- **`compound`** — `compound_smiles:`, the "also written" synonym line, and the typed edges above
  toward the runs and products this compound participates in.
- **`failure-mode`** — `conditions.outcome: failure` plus whatever figures the prose gives; a
  failure that reads as an ordinary run is the row a chemist must not misread. `contradicts`
  edges where the failure argues with a recommendation.
- **`job-result`** — `calc_refs:` naming the calculation keys the numbers came from (and
  `artifact_refs:` for stored by-products). A computed figure with an empty `calc_refs` is
  unauditable and must say in prose why.
- **`campaign` / `optimization-campaign` / `report`** — plain `[[links]]` to members and
  evidence; members point back with `part-of`.
- **`playbook`** — the distilled rule, `[[cites:…]]` to the evidence it was distilled from, and
  a `supersedes` edge (plus the old note's `superseded-by` and closed `valid_to`) when it
  replaces one.

## Conventions

- **State the purification with the yield.** An 88% after recrystallisation and an 88% after a plug
  are different numbers (`playbook-recrystallisation-purity`).
- **Type the edge when the relation is not just a citation** — in the declared direction.
  `[[precursor-of:…]]` is queryable; a bare `[[…]]` is a citation. Both are valid; the first is
  more useful.
- **Record what failed.** `failure-mode` notes are the only place a negative result survives, and a
  negative result is often the more transferable one.

Run `make kg-validate` after any edit: it checks schema validity, duplicate ids, filename and
directory against id and type, dangling links, malformed link targets, unknown note types, unknown
relations, relation direction against `RELATION_SIGNATURES` — and, when a database is reachable
(`python -m chemclaw.cli.validate_kg`), that every `[[reaction-…]]` citation names a record the
transcription store holds. The hazard gate it once ran was retired with
`D-2026-08-15-safety-is-a-tool-not-a-gate`; procedure safety is the reviewing human's judgment,
assisted by the `safety` MCP server as a tool.
