# Recurring feeder pipelines — the recurring work that keeps a corpus fresh, run outside ChemClaw3

## The ask

> Instructions for setting up recurring pipelines that are **not in ChemClaw** but facilitate it:
> daily cron jobs that pull from an upstream database over a URL, or new reaction SMILES plus a
> reaction identifier, into the chemical database, so the vectors for reactions and molecules can be
> computed there. Check whether this fits the codebase, then prepare super-detailed MD files to be
> implemented directly on OpenShift or Databricks.

## Does it fit? — yes, and the seam it fits is already load-bearing

Verified against the tree, not assumed:

- `chemclaw.ingest.sources` (D-120) attaches a corpus with **zero core edits**: one folder holding a
  `datasource.yaml`, one name in `CHEMCLAW_DATA_SOURCES`.
- `chemclaw.ingest.eln.warehouse` names **no table and no column**; the site's schema is a binding in
  that manifest (`D-2026-08-04-the-schema-is-a-file`), and `connection:` is the driver's own keyword
  arguments (`D-2026-08-26-the-driver-s-signature-is-the-schema`).
- A `corpus:` block is drained by `ReactionCorpusWorkflow` on the `reaction-corpus` Schedule
  (`corpus_sync_schedule_minutes`, default 1440) into `reaction_labels`, `reaction_species` and
  `corpus_molecules`; `ReactionLabelWorkflow` (`reaction-labels`, hourly) then fills the atom map and
  the named reaction through `Chemclaw3-mcp:servers/rxnlabel`.
- A `vector:` block searches an embedding the warehouse **already holds** — ChemClaw3 embeds the
  *query* and never the corpus. `pistachio/datasource.yaml` is the worked example at patent scale.

So everything from the landing relation inwards exists. **What has no owner is everything upstream of
it**: fetching the release, normalising it, and computing the per-reaction embedding. That is the
pipeline being asked for, and it belongs outside this repository for a reason the tree already
states twice (`Chemclaw3-mcp`'s no-egress posture; D-089 on third-party runtime dependencies).

## Plan

- [x] Verify the seam end to end — the manifest, the binding models, the two Schedules, the
      Postgres tables, the vector store adapter, and the settings each one reads.
- [x] Write the ADR that fixes the boundary, so a later session does not build the feeder inside a
      connector: `D-2026-08-28-a-feeder-writes-a-table-and-nothing-else.md` + its ledger row.
- [x] `docs/guides/feeder-pipelines/README.md` — the index, the division of labour, the fit verdict.
- [x] `01-contract.md` — the normative target contract: relations, columns, casing, key identity,
      embedding parity, unit normalisation, the three index columns, keyset stability, and the
      ChemClaw3-side setting each one pairs with.
- [x] `02-acquisition.md` — pulling from an upstream URL database: idempotency, watermarks,
      checksums, licence, resume, canonicalisation, dedup.
- [x] `03-databricks.md` — the Databricks implementation: bundle, DDL, MERGE, embed task, Vector
      Search index, schedule, sizing, secrets, monitoring.
- [x] `04-openshift.md` — the OpenShift implementation: CronJob, Secret, NetworkPolicy, RBAC,
      concurrency, resources, alerts, and the Postgres-target variant.
- [x] `05-operations.md` — bring-up order, the verification probes, the failure modes as they look
      from ChemClaw3's side, re-embedding after a model change, cost and scale.
- [x] `docs/README.md` — the `guides/` row names the new set.
- [x] `make lint type test` (documentation-only change; the repo-map and decision-log tests are the
      ones that can fail on it).

## Verification

Documentation only — no `src/` change, so the behavioural suite is unaffected. What must be green is
what checks *declarations*: `tests/test_decision_log.py` (the ADR id, its heading, its ledger row and
their order) and `tests/test_repo_map.py` (no shipped document naming a bundle that is gone).

Every normative claim in `01-contract.md` is cited to the file it was read from, so a reviewer can
check it without trusting this text — the standard `CLAUDE.md` asks for ("prose is evidence about
what its author believed").

## Review

Written and pushed on `claude/recurring-pipeline-setup-14kia5`.

Six guide files plus one ADR, no `src/` change. Three findings that only came out of reading the
code and that a naively-built feeder would have got wrong:

1. **The corpus drain keeps no `sync_cursors` row** (`corpus_sync.py`'s own docstring, and
   `BACKLOG.md` records the two ADRs that claimed otherwise as falsified). Its cursor is intra-run.
   So a daily Schedule re-walks the whole relation, and the feeder — not ChemClaw3 — is what keeps
   that affordable, by maintaining a `load_date` the binding's `where:` narrows on.
2. **A Databricks-hosted corpus index needs `group_key` and it must equal the binding's `key:`.**
   `DatabricksVectorStore.search` always requests `columns=[id, group_key]` and filters eligibility
   as `{group_key: [...]}`, where the eligible values are warehouse keys resolved from
   `filter_columns`. An index built with only `id` and `embedding` fails every search; one whose
   `group_key` is anything else returns nothing for every filtered search.
3. **Vectors written by a feeder must be L2-normalised.** The adapter normalises what *it* upserts
   and what it queries with, and inverts Databricks' `1/(1+d²)` assuming unit length on both sides —
   so an un-normalised corpus vector is not a crash, it is a wrong cosine fused into hybrid ranking.

And one thing deliberately *not* built: a molecule-vector table. `corpus_molecules` (ECFP4 bits plus
the RDKit pattern screen) is written by ChemClaw3's own drain from the corpus `smiles:` column, so a
feeder that computed molecule fingerprints would be computing something nothing reads.
