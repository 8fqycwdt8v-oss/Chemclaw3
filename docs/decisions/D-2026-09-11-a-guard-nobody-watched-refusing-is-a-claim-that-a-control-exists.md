# D-2026-09-11-a-guard-nobody-watched-refusing-is-a-claim-that-a-control-exists — A guard nobody watched refusing is a claim that a control exists

**Status:** accepted · **Date:** 2026-09-11

## Context

Waves 10–14 added 273 test functions and ~95 module-level constants, twelve of which carry
allowlist or exemption semantics. Wave 8 had already found three of its *own* new guards vacuous,
so wave 15 took that as a premise rather than a worry and mutation-tested the guards against their
own subjects: **38 mutations over 30 guards, 30 red and 8 green.**

The premise is partly upheld and the shape of what it upheld is the finding. The heavyweight
machinery is genuinely driven — the migration-rollback registers, the alert rule, the
workflow-replay control, the compaction scaling test, the context-floor ratchet, the grant matrix,
the cross-repo sibling checks and the compaction-default derivations all go red against their own
subjects, several with the exact numbers their docstrings claim. **Every failure was in a small
guard**: an allowlist with a missing second half, a phrase list, a hand-copied tuple, a keyword gate.

Two are the worst class — a guard protecting something this review *claimed to have fixed* that does
not catch that defect:

- **`GUARDED` in `tests/test_jsonb_boundaries.py` was declared and never checked.** The file
  partitions every `Jsonb(` construction into `GUARDED | NOT_YET_MEASURED` and asserts
  `actual <= declared`. A guarded site that *regresses* to a bare `Jsonb` is still declared, so it
  satisfies that assertion exactly as the fixed version does. Measured: reverting wave 11's own fix
  in `publish/outbox.py` left **44 tests across three files green**, this one included. The
  register's comment said each member "has a test beside it that was watched failing against the
  unfixed source" — true of two of three; the outbox's own non-finite test refuses at *projection*
  and never reaches the column.
- **`test_no_docstring_on_the_write_path_still_promises_a_human_reviewer` catches its own three
  files and nothing else, while the defect it names is live across the tree.** It is a
  case-sensitive `phrase not in source` over 3 files and 6 phrases. The identical registered claim
  re-inserted sentence-initially in a file the register *does* name passes; so does the same claim
  in a fourth module. `grep PR-gat src/chemclaw --include=*.py` returned **109 lines across 66
  modules**, and D-005's gate has been gone since
  `D-2026-09-05-the-gate-follows-behaviour-not-knowledge`.

The severity of the second is not the docstrings. `durable/observation_jobs.py` builds the **body of
a note it writes into the knowledge graph**, and that body told every future reader *"this PR is the
first point at which a human is asked to judge either the reading or the evidence"*. There is no PR.
A chemist retrieving that note as evidence was being told a control stood behind it, in the
direction that overstates safety — which is the exact harm the gate's removal was argued as safe
*because* it does not do: knowledge lands readable beside its own citations, and a reader who is
told somebody already checked has no reason to check.

Three lesser findings, each a register missing the half its siblings have:
`_RETIRED_TEST_CITATIONS` had no unspent-entry check (adding a row naming a test no ADR cites left
the file green at 16 passed); the disposal-register erasure guard opened
`if "erasure" not in stated and "erases" not in stated: continue`, so the identical defect worded
*"a leaver sweep removes it per actor"* walked past, and the loop could pass having examined nothing;
the BO gate-claim scan globbed one bundle, so the same sentence in a root `skills/*/SKILL.md` — one
of 28 injected into every prompt — passed. And the skills-prompt pin hand-copied the production
tuple it claimed to check "from the other side", so rewording an entry and adding a fifth both
stayed green at 4 passed.

Separately, the rollback runbook covered **five of `_REVIEWED_ROLLBACK_BREAKS`'s eight entries and
neither of `_REVIEWED_SEMANTIC_BREAKS`'s two**. The three newest breaks were reviewed, exempted, and
never written down where an operator rolling back would look.

## Decision

**A guard is kept only after it has been watched refusing.** Every fix in this wave was driven
against its own mutation before it was kept, and the mutation is named in the guard's docstring, so
the next reader can see what it was measured against rather than what it was intended for.

**A register gets both halves.** An exemption register needs a staleness check (does the row still
name something that exists?) *and* an unspent check (is anything actually asking for this
permission?), because a row is a silencer and a silencer nobody spends goes on silencing whatever
lands at that name next. `_REVIEWED_ROLLBACK_BREAKS`, `KNOWN_OVERSIZED` and `NOT_YET_MEASURED` had
both; `GUARDED`, `_RETIRED_TEST_CITATIONS` and the new `_GATE_CLAIM_HISTORICAL` now do too.

**A guard asserts a property, not a membership.** `actual <= declared` is a membership test and it
is why `GUARDED` could hold a reverted site. `assert not (GUARDED & _jsonb_sites())` is the property.

**A keyword gate is allowed only where a miss costs a check and never a false pass of the
assertion** — and where it decides whether a stricter arm runs, the examined set is asserted
non-empty, because a loop that runs zero times is indistinguishable from one that found nothing
wrong.

**A pin imports what it pins.** A test that copies the production constant it claims to check from
the other side is checking its own copy.

**Model-facing and reader-facing prose is one corpus.** The gate-claim scan now covers every
`connector.yaml` and every `SKILL.md` under both roots, and the note bodies this system writes into
the graph are held to the same rule as the tool descriptions it sends the model — a note is read by
a chemist deciding whether to trust it, which is the same act.

Every present-tense claim that the D-005 gate is live is corrected across `src/`, with the
historical mentions deliberately kept: this repository keeps the reasoning behind a decision, and
sanitizing the past-tense half would delete the argument that makes the present-tense half
checkable.

## Consequences

`tests/test_migrations_are_additive.py::test_every_reviewed_break_tells_the_operator_what_it_costs`
reads the runbook the way `tests/test_schema_inventory.py` reads `infra/sql/README.md`: the register
is the authority and the operator-facing table is checked against it, one direction only. The
table's rows for breaks the patterns catch without review (083, 090) stay, because the register
records that a break was *judged* and the table records what it *costs*, and only the second is
something a register could never hold.

`propose_report` is left named `propose_report` and gets a `docs/planning/BACKLOG.md` row. It
proposes nothing, but the string is a registered Temporal activity name: renaming it means
registering both for one deployment cycle and dropping the old one after the queue drains, which is
a release procedure rather than a commit.

**What this does not establish.** 30 of 38 mutations going red is evidence about the guards wave 15
chose to mutate — the five risk categories, plus the registers' subjects — and says nothing about
the ~240 ordinary behaviour tests waves 10–14 added, which were not touched. Three shallow-history
guards in `tests/test_migrations_are_additive.py` skip in any grafted checkout and could not be
driven here at all; they run under `fetch-depth: 0` on CI's `check` job, and their skip messages say
so. A sweep that reports only what it looked at is the one this ADR is asking for.

## What keeps it true

- `tests/test_jsonb_boundaries.py::test_no_guarded_site_has_reverted`
- `tests/test_decision_log.py::test_no_retired_test_citation_is_unspent`
- `tests/test_bo_knowledge.py::test_no_historical_gate_exemption_is_unspent`
- `tests/test_migrations_are_additive.py::test_every_reviewed_break_tells_the_operator_what_it_costs`
- `tests/test_retention.py::test_no_disposal_entry_offers_actor_erasure_as_what_bounds_a_table`
- `tests/test_upstream_surface.py::test_the_skills_prompt_still_contains_every_sentence_this_deployment_removes`
- `tests/test_bo_knowledge.py::test_no_model_facing_bo_text_claims_a_recommendation_is_reviewed_before_it_lands`
