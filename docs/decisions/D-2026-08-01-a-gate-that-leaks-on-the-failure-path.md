# D-2026-08-01-a-gate-that-leaks-on-the-failure-path — A gate that leaks on the failure path

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** D-005 (the PR-gate as the GxP
"AI proposes, human signs off" line) · **Implements:** the full-codebase review's `git_submitter`
and `proposal_store` findings

## Context

The PR-gate is the line this system is built around: an agent may propose a note, and a human
validates before it merges. Three defects let a record on the failure path contradict that, and none
of them is visible on the success path anybody tests.

**A failed push left the shared checkout on the note branch.** `_return_to_base()` was reached only
on the two success returns — there was no `try/finally`. Every reader resolves
`settings.knowledge_path` into that same checkout and `invalidate_cache()` had already run, so an
unreviewed, agent-authored note was served as *merged knowledge* — and counted as merged by
`ingest/eln/sync._merged_note_bodies` — until some later submission happened to succeed. Retries
land on the same branch and do not repair it.

**A `try/finally` alone would not have fixed it.** A bare `checkout -B` back to base keeps untracked
files, so a submission dying between `write_text` and `git add` still handed the note to readers on
base.

**A proposal that succeeded after a transient failure stayed `failed` forever.** The upsert on
`(note_id, content_hash)` deliberately never updated `state` — a rule written for the
rejected-then-re-proposed case. But the retry (live in three durable jobs, under
`note_publish_retry()`, with byte-identical content) refreshed `reference` and `submitted_at` and
left `state='failed'` with the stale git error as its reason. The branch existed awaiting review
while the row was excluded from every `state='open'` query, `POST /proposals/{id}/decision` returned
409, and the merge webhook's `mark_merged` returned 0 — a permanent GxP record asserting a
submission failed when it succeeded.

## Decision

**The restore is unconditional and it discards.** The post-checkout body moved into
`_write_and_push`, called under `try/finally`; the restore does `reset --hard` + `clean -fd` +
`checkout -B`, with `invalidate_cache()` in its own `finally`. One helper serves both switches, so
the discard cannot be present on one path and absent on the other.

**Exactly one cache invalidation, at the moment the tree is actually restored.** The mid-body call
was removed: its justification ("the authoring loop must see its own write immediately") died with
this change, because `submit()` now always returns with the note absent from the tree — and it
actively widened the remaining transient window by making a cold cache more likely inside it.

**`failed` is the one state a later success may supersede.** `state = CASE WHEN state = 'failed'
THEN EXCLUDED.state ELSE state END`, mirrored in the in-memory store. A **decision** is never
superseded: a rejected proposal must not silently reopen because a retry re-pushed the same bytes.
Both directions are tested.

**The production backend gets a real test.** `PostgresProposalStore` — the store that decides whether
a proposal awaiting human review is visible to the reviewer — had no automated coverage; the
in-memory mirror passing proves nothing about the SQL that runs. Added against the repo's existing
`migrated_db_or_skip` pattern and verified against a live cluster.

## Consequences

- `docs/planning/BACKLOG.md`'s DARK-10 row was wrong. It asserted that `_return_to_base` had already
  fixed the permanent case and only a transient window remained; the permanent case was never fixed,
  because the restore was unreachable on failure. The row is corrected rather than closed — a genuine
  transient window (between commit, fetch and push) remains.
- Two mutations are pinned separately, because they fail differently: success-only restore leaves the
  branch at `note/<id>`, and a non-discarding restore leaves the note file in the tree on base.
- Local development needs pgvector ≥ 0.7 for `bit_jaccard_ops`; the common distribution package is
  0.6.0. Pre-existing, and it bites anyone standing a database up from `apt`.

## Alternatives rejected

- **Catch-and-restore at each failure site.** Four call paths, each needing to remember; the defect
  is precisely that one of them did not.
- **Let the retry write a new row rather than supersede.** Turns one note's history into a sequence
  of contradictory records, and the reviewer's queue still has to decide which is live.
- **Leave `failed` immutable and have the reviewer re-propose.** Puts recovery from a transient
  network error into a human's hands, in the one workflow where a human's attention is the scarce
  resource the gate is spending.
