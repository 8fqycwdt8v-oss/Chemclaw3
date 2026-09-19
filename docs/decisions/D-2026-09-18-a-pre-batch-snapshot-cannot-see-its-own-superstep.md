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
middleware's two `Command` rebuilds relay a wrapped tool's *own* update rather than originating
one. A `props` call in the same batch writes no file, so dividing by `batch_width` would have cut
a helper's research note to an eighth on a batch that shares none of its budget — failing closed,
which is safe and is wrong in the direction nobody would ever report as a defect.

**That argument is directionally right and it was overstated, which a fresh-context review caught
before this merged.** `batch_siblings` counts siblings *by name*, not writers, and most `task`
calls read and write nothing — so a batch of eight helpers of which one files a note reaches the
very outcome this paragraph rejects, by a narrower route. Driven at the shipped budget, one writer
beside silent siblings:

| batch width | of a 199,999-character note |
|---|---|
| 1 | 199,999 lands |
| 2 | 100,000 |
| 4 | 50,000 |
| 8 | 25,000 |

What is true is the inequality: `batch_siblings` is never larger than `batch_width`, so it wastes
strictly less than the alternative and strictly less than the defect it replaces. What is false is
"avoids". The bound itself is not endangered — an unclaimed sibling's share is wasted allowance,
never spent, so this fails closed — and what it costs is a note cut for company it did not keep.

`batch_width` and `batch_siblings` now share one walk (`_batch_calls`) rather than two copies of
it, which is what makes "the whole batch" and "this tool's calls" visibly two readings of one fact
instead of two functions that happen to agree.

## What this does not do

**It does not count writers, and the table above is what that costs.** Counting them needs the
siblings' results, which do not exist when this runs. "The only arithmetic available" would be too
strong, though, and is not claimed: `files` is a per-path dict-merge channel, so a trim over the
*merged* channel after the superstep would see every real contribution and over-charge nobody.
That is not a free win, which is why it is a `BACKLOG.md` row rather than this commit — the
exemption keeping a chemist's own documents out of this budget is `rewritten_command_files`
comparing each command against the state *before* it, and a post-merge trim has no such comparison
to make, so buying exact accounting means first giving the channel provenance.
`test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` is what a naive version
of it would break.

A batch mixing **two different** tools that hand back a `Command` carrying `files` would divide
each family by its own count and together exceed the budget. There is one such originator in the
installed `deepagents`, and that is now asserted in `tests/test_upstream_surface.py`, where this
repository keeps every assumption about a library's shape. **This section shipped saying it was
kept in the outcome tests below instead, and those read `deepagents` nowhere** — so the
load-bearing claim was held by this prose and by a walk somebody did once, which is the shape that
file exists to end. It also said "nothing in this tree is a second such tool", and that is wrong
about the same channel one layer over: `write_file` and `edit_file` reach it through
`StateBackend`'s `send(...)` and return a plain `ToolMessage`, so they never pass
`rewritten_command_files` and nothing bounds them. Pre-existing, now queued, and named here because
an ADR saying there is no second writer reads as a bound tighter than the one that ships.

## What keeps it true

- `tests/test_subagents.py::test_a_parallel_fan_out_shares_the_budget_rather_than_multiplying_it`
  drives four concurrent `task` calls through the shipped middleware off one originating
  `AIMessage` and asserts what lands in the channel. It fails with `800000 <= 200000` against the
  unfixed tree.
- `tests/test_subagents.py::test_the_fan_out_divisor_counts_the_tools_that_write_files_not_the_whole_batch`
  is the other direction. **It passes against the unfixed tree too**, said plainly because the rest
  of this list does not: it is a counterfactual guard on the divisor rather than a regression test
  on the defect, and it discriminates — mutating the call site to `batch_width` reds it at
  `25000 == 199999`.
- `tests/test_subagents.py::test_the_file_share_bounds_the_superstep_at_every_width_this_deployment_allows`
  asserts the superstep total. **This bullet shipped claiming it "sweeps past the crossover where
  the per-file floor stops shrinking", and that was false of the test as written**: it passed
  `held=budget`, so the share floored to 1 in every cell and neither parameter influenced an
  assertion — it passed with the whole `concurrent` divisor reverted. Worse, the bound in its own
  name did not hold, because a per-file cut has a floor and N files each at it is 44N.
  `D-2026-09-19-a-cap-on-each-file-is-not-a-cap-on-the-command` caps the count as well and gives
  the test a fresh channel, the total assertion, and widths past `agent_max_parallel_tool_calls`.
- `tests/test_upstream_surface.py::test_only_the_subagent_middleware_returns_a_command_carrying_the_files_channel`
  is the divisor's dependency assumption, held against the installed distribution rather than
  against this record.
- `tests/test_subagents.py::test_a_second_delegation_shares_the_budget_the_first_one_spent` and
  `::test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` are the sequential
  bound and the caller's-own-documents exemption, unchanged, so this arithmetic is held to not
  having moved either.
