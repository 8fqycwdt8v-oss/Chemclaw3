# D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep — the concurrent half of the helper file bound

**Status:** accepted · **Date:** 2026-09-18 · Extends
`D-2026-09-13-a-bound-on-one-command-is-not-a-bound-on-a-delta-channel`, which made
`agent_subagent_files_max_chars` a bound on the channel for *sequential* delegation. Supersedes
nothing: `held`, the per-file division and the floor-after-division are all unchanged.

## Context

That record closed the sequential case by charging what the caller's `files` channel already holds
against the same budget — `_files_already_held` reads `request.state["files"]`, so a second `task`
call is charged for the first one's output. It could not close the concurrent case, and the reason
is structural rather than an oversight: `ToolNode` builds every call in a superstep from **one
pre-batch snapshot**. `batch_width` was added for exactly this property one resource over, and its
own docstring named it.

So N concurrent `task` calls each read an identical `held` — zero, on a fresh channel — and each
took the whole of what was left. `general_purpose_helper`'s description invites that shape in so
many words ("Spawn one — or several at once") and `agent_max_parallel_tool_calls` ships at 8.

**Measured** on the commit before this one, through the shipped `bound_tool_results` with a real
originating `AIMessage` carrying four `task` calls, each helper returning a file of twice the
budget:

| | into one `files` channel |
|---|---|
| `agent_subagent_files_max_chars` | 200,000 |
| four concurrent `task` calls, before | **800,000** |
| four concurrent `task` calls, after | 200,000 |

Four WARNING lines each read `the share … across 1 file(s) is 200000, with 0 character(s) of the
budget already held` — the bound reporting itself satisfied, four times, on the same budget.

## Decision

`_bounded_file` takes a `concurrent` count and divides the remaining budget by `sharing *
concurrent`. The count comes from `batch_siblings(request)`: **how many calls in this batch invoke
the same tool as this one**, never below 1.

**Same-name rather than `batch_width`, and that is the one decision here.** `bounded_for_batch`
divides by the whole batch and is right to, because every result in a batch is sent to the same
model in the same request — every call is a producer of that resource. This resource has fewer
producers. Measured against the installed distributions by walking every `Command` construction in
`deepagents`, the only site that copies a non-excluded state key — and so `files` — into a caller's
update is `deepagents.middleware.subagents`'s `**state_update`, which is `task`; the filesystem
middleware's own `Command`s carry `messages` and nothing else. A `props` call in the same batch
writes no file, so dividing by `batch_width` would have cut a helper's research note to an eighth
on a batch that shares none of its budget — failing closed, which is safe and is wrong in the
direction nobody would ever report as a defect.

`batch_width` and `batch_siblings` now share one walk (`_batch_calls`) rather than two copies of
it, which is what makes "the whole batch" and "this tool's calls" visibly two readings of one fact
instead of two functions that happen to agree.

## What this does not do

The divisor assumes each concurrent producer takes an equal slice, because the siblings' results do
not exist yet — there is no number available before them. A batch of one wide `task` and one narrow
one therefore over-charges the narrow one. That is the conservative direction for a storage bound
and the cost is bounded by the setting; the alternative needs the siblings' output, which is the
thing a pre-batch snapshot is defined not to have.

A batch mixing **two different** file-producing tools would still divide each family by its own
count. Nothing in this tree is a second such tool, which is what the measurement above establishes
rather than assumes — and it is a property of the installed `deepagents`, so it is a thing a
dependency bump can change. `tests/test_upstream_surface.py` is where this repository keeps such
assumptions; this one is kept in the test below instead, because what it asserts is an *outcome*
on the channel rather than a shape.

## What keeps it true

- `tests/test_subagents.py::test_a_parallel_fan_out_shares_the_budget_rather_than_multiplying_it`
  drives four concurrent `task` calls through the shipped middleware off one originating
  `AIMessage` and asserts what lands in the channel. It fails with `800000 <= 200000` against the
  unfixed tree.
- `tests/test_subagents.py::test_the_fan_out_divisor_counts_the_tools_that_write_files_not_the_whole_batch`
  is the other direction, and it is what a `batch_width` divisor would fail: one `task` beside
  seven calls that write no file still stores its whole note.
- `tests/test_subagents.py::test_a_second_delegation_shares_the_budget_the_first_one_spent` and
  `::test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` are the sequential
  bound and the caller's-own-documents exemption, unchanged, so this arithmetic is held to not
  having moved either.
