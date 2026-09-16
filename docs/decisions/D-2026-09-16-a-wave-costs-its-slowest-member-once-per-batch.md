# D-2026-09-16-a-wave-costs-its-slowest-member-once-per-batch — the wave scheduler and the resume path, recorded, and their ceiling made honest

**Status:** accepted · **Date:** 2026-09-16 · Supersedes the *"Parallel or fan-out steps"* and
*"Resume from a failed step"* sections of
`D-2026-09-15-a-bound-that-stops-at-the-seam-is-not-a-bound`, which decline what the same pull
request then built.

## Context

Two behaviour changes shipped with no ADR at all, and the only merged decision that discusses them
declines them — in a section headed "What this deliberately does not do", written two commits
before the commits that did them. `docs/decisions/README.md`'s ledger row for that ADR said
`run_ceiling_problems` *"sums a file's steps"*, which it stopped doing in the same PR, and is
corrected here — a ledger row is not an ADR and is editable, so leaving it would have been a second
false present tense rather than a respected immutability.
A merged ADR is immutable here, so the remedy is this one.

What actually shipped:

- **`templates/schedule.py`** groups a template's steps into waves from the `${steps.<id>.result}`
  edges the file already declares, and `TemplateWorkflow` runs a wave together. No new YAML key,
  and seven of the nine shipped templates chain into one-step waves, so they run exactly as they
  did before concurrency existed.
- **`durable/template_activities.completed_steps`** reads back what a previous failed run of the
  same id finished, and `TemplateWorkflow._resume` seeds `scope` with it. Both are gated by one
  `workflow.patched("template-waves-and-resume")` marker, whose drain condition lives in
  `docs/guides/workflow-versioning.md` and `tests/test_workflow_versioning.py`.

Neither is in question here; what is in question is the arithmetic that was left behind, and a
finding a fresh-context review drove afterwards.

## The defect this also fixes

`run_ceiling_problems` was rewritten with the waves, correctly in one direction: a wave costs its
**slowest member** rather than the sum of its members, so two independent `job` steps stop being
refused for a cost the run does not pay. What it did not ask is how wide a wave may be.

Measured: a document of **501 independent `tool` steps** passes the run ceiling as if the whole
procedure cost one 900-second step — `authored_problems: []`, `run_ceiling_problems: []`, one wave,
501 members — and `TemplateWorkflow` then `asyncio.gather`s all 501. Sizing a wave at one slow step
is true only if every member really is in flight together, which no worker anywhere promises. For a
reviewed `data/templates/` file the reviewer is the bound on width; an agent-authored document
reaches this arithmetic with nobody having looked at it.

## Decision

**A wave is dispatched in batches, and its ceiling is its slowest member once per batch.**

`templates/schedule.batches(wave, limit)` splits a wave into runs of at most `limit` steps in
declared order. `TemplateWorkflow._run_wave` dispatches those batches; `run_ceiling_problems` sizes
the wave as `sum(max(batch) for batch in batches(wave, limit))`. **One function, read by both**, and
the limit is `orchestrator_max_parallel_children` pinned into `TemplateRunInput.max_parallel_steps`
at launch — so the bound the run *enforces* and the bound the launch was *checked against* cannot be
two numbers.

**The cost model is part of that, and the first attempt got it wrong.** It charged
`ceil(width / limit) x the whole wave's slowest member`, which shares the limit and not the model:
the slow step is charged to every batch, including batches holding nothing slow. Measured — one
39,330 s `job` step beside eight 900 s `tool` steps is a 40,230 s wave charged at 78,660 s, so a
procedure with 4,200 s of headroom is refused by 34,230 s. That is exactly the over-stating bound
this arithmetic exists to avoid, reintroduced by the commit that was fixing it, and at
`orchestrator_max_parallel_children=1` — a legal value — it refused a three-step composed workflow
that fits. `batches` lives in `templates/schedule.py` rather than beside the dispatcher so that
re-deriving it is not something a reader has to do.

- **A batch and not a semaphore**, the reason `durable/orchestrator.fan_out` gives for the same
  choice: a fixed-size batch does not depend on lock-acquisition order, so it is deterministic
  under Temporal's replay, and it bounds concurrency just the same.
- **Pinned and not read.** A live settings read inside workflow code is nondeterministic on replay
  *and* is not what the ceiling saw. `max_parallel_steps` defaults to `0` — no bound — because that
  is what an input predating the field declares, and every archived history is pre-wave: its waves
  are one step wide, so an unbounded gather over one step is the sequential shape byte for byte.
  No new patch marker is needed for the same reason.
- **No `MAX_COMPOSED_STEPS`.** With the arithmetic honest, the run ceiling bounds both width and
  length, and a second number with its own argument would be a magic one. What that bound actually
  is, measured by binary search at the shipped defaults: **400** `tool` steps in one wave
  (`ceil(400 / 8) x 900 = 45,000 <= 45,330`), or 50 chained ones. Stated rather than left as "the
  ceiling handles it", because 400 is a real number and somebody may decide it is too high — that
  would be a new decision about `template_run_timeout_seconds`, not a second cap.

## The refusal message

The breakdown joined a wave's members with `" + "`, which reads as addition and is wrong for steps
that run together: a two-`job` wave printed `survey=39,330s + survey2=39,330s` beside a total that
counted one of them, so a reader adding the printed numbers got 80,460 where the message said
79,560. In a message whose entire job is to explain a ceiling calculation to somebody who has to fix
a YAML file, that is the one thing it must not do. Concurrent members are now `" | "` inside
brackets carrying the wave's own cost; a wave of one prints as the bare step it is.
`tests/test_templates.py::test_the_refusals_printed_terms_add_up_to_the_printed_total` adds the
printed terms up and compares them to the printed total, rather than asserting a string.

## Consequences

- All nine shipped templates are unaffected: the two with a concurrent wave have width 2, and
  `ceil(2 / 8) = 1`.
- A composed document whose fan-out cannot finish is refused at compose time and at launch, by the
  ceiling that already existed, with a message naming the width and the batch count. It names the
  first four members and then the width: listing all of them made the ADR's own 501-step example a
  6,435-character refusal with 502 `=` terms in which the number 501 never appeared.
- `docs/planning/BACKLOG.md`'s row for the resume path is deleted in this commit, as the rule for
  that file requires; it was closed by `d08f770` two commits after it was written and outlived its
  closure by a PR.

## Alternatives rejected

**Leave the ceiling optimistic and bound the document instead.** A `MAX_COMPOSED_STEPS` would stop
the 501-step case and would leave the arithmetic wrong for every case under it — a 400-step wave
would still be sized at one step. The ceiling is the thing that was lying; fixing the document size
fixes the example rather than the defect.

**Bound concurrency with the worker's own `worker_max_concurrent_activities`.** That is shared
across every workflow on the worker, so it is not a per-run parallelism and could not be used to
size a run in advance. It also could not be pinned, since it is a property of whichever pod happens
to pick the task up.
