# D-2026-09-19-a-cap-on-each-file-is-not-a-cap-on-the-command — the floor the per-file share cannot get under

**Status:** accepted · **Date:** 2026-09-19 · Extends
`D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep`, and corrects one of its
"What keeps it true" bullets. Supersedes nothing: `held`, `batch_siblings` and the share arithmetic
are unchanged.

## Context

That record closed the concurrent fan-out by dividing the remaining `agent_subagent_files_max_chars`
by `sharing * concurrent`. A fresh-context review of it found two things, and the second is the
defect.

**The sweep test it shipped was degenerate.** It called `_bounded_file(content, sharing, budget,
concurrent)` — `held = budget` — so the numerator was 0 in all sixteen cells, `max(0 // D, 1)`
floored the share to 1, and neither parameter influenced a single assertion. Driven: it passes with
the entire `concurrent` divisor reverted. Its docstring claimed to sweep "past the crossover where
the per-file floor stops shrinking"; at an exhausted budget every cell is already past it.

**And the bound in its own name did not hold.** `bounded_content` never returns less than the
notice saying it cut — a bound paid for by saying nothing is not what that module is for — so N
files each cut to that notice is 44N, and the superstep total grows linearly in N again. Measured
through the shipped `bound_tool_results` against a 200,000-character budget:

| concurrent | files per call | landed |
|---|---|---|
| 8 | 600 | 206,400 |
| 8 | 5,000 | 1,720,000 |
| 1 | 5,000 | 215,000 |
| 20 | 600 | 516,000 |

`(5,000, 8)` was literally a cell in that test's own grid, and it passed.

The `concurrent=1` row is the important one: **this is not a regression that divisor introduced.**
The floor is older, and the sibling resource has it identically — `bounded_for_batch` exceeds its
own ceiling at width ≥ 2,000, where its sweep test stops at 1,000. What the divisor did was move
the crossover down by up to `agent_max_parallel_tool_calls`, and what the test did was claim the
bound held.

## Decision

**Cap the count as well as each file's size.** Dividing the share further cannot help once it has
floored; what is left is to store fewer files and say so once. `rewritten_command_files` takes a
`capacity`, `_representable_files` derives it as `remaining // (floor * batch_siblings)`, and files
past it are **omitted** with one entry at `/scratch/_files_the_budget_could_not_hold.md` naming the
count and a bounded sample.

**Omitted rather than stored empty, and that is the one decision here.** Reading a dropped path back
fails with "no such file"; an empty one hands a chemist a document that simply stops, which is the
silent cut the module exists to prevent. One notice for the whole set is what keeps the total
bounded — a marker per dropped file is the 44N this cap exists to stop.

The floor is **derived** from `_brief_notice`'s own length rather than written down, so rewording
the notice moves the capacity and no constant here goes stale. `capacity` is `None` when
`agent_subagent_files_max_chars` is 0, which is the documented way to switch the cap off and must
not become a cap of zero files.

## What this does not do

**It does not fix the sibling.** `bounded_for_batch` has the same floor over tool *results*, and
exceeds its ceiling past width ~1,333. That needs a model emitting 1,333 tool calls in one message,
where this needed a helper writing 5,000 files, so it is the less reachable of the two — filed as
its own row rather than folded in here, because the fix shape is different: a result cannot be
omitted the way a file can, since the model is waiting for it.

**The capacity divides by `batch_siblings`, so it inherits that divisor's known over-charge** —
siblings are counted by name, not by whether they write. A lone writer beside silent siblings gets
a smaller capacity than it needed. Same trade as the share, same reason, and the same BACKLOG row
covers both.

## What keeps it true

- `tests/test_subagents.py::test_the_file_share_bounds_the_superstep_at_every_width_this_deployment_allows`
  rewritten: a fresh channel so the share varies, the **superstep total** asserted rather than
  per-file properties, and widths past `agent_max_parallel_tool_calls` because that setting is
  LangGraph's `max_concurrency` and nothing clamps a batch. It fails without `capacity` at
  `215,000 <= 200,000`.
- `tests/test_subagents.py::test_a_parallel_fan_out_shares_the_budget_rather_than_multiplying_it`
  and `::test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` are the
  fan-out bound and the caller's-own-documents exemption, unchanged, so this is held to not having
  moved either.
