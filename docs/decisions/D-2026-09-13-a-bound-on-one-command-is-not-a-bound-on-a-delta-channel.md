# D-2026-09-13-a-bound-on-one-command-is-not-a-bound-on-a-delta-channel — the argument the cap was built on, one level up

`D-2026-09-12-a-helpers-scratch-file-crosses-into-its-callers-state` bounded what a helper's `files`
write puts in its caller's checkpointed state, and argued the shape of the bound correctly: *"a
per-file cap times an unbounded number of files is not a bound"*, so the budget is the channel's and
the share is per file. The bound it built is applied to one `Command`.

## What was measured

`files` is a `DeltaChannel` — it merges rather than replaces. `rewritten_command_files` sees one
`task` return, so a caller that delegates N times stored up to N x
`agent_subagent_files_max_chars`. It is a *storage* bound, so the cost is checkpoint rows: at the
10.4x amplification that ADR measured, ten delegations at the shipped setting is ~20 MB of
checkpoint rows per superstep instead of ~2. `tests/test_subagents.py::test_several_files_share_one_budget`
builds one `Command` and cannot see it.

The same review found a third figure for that amplification in the tree: `core/config/agent.py` said
"7,818 kB + 7,815 kB = ~15.6 MB, 7.8x" while the ADR and `.env.example` both said 20,712 kB and
10.4x. That is not a disagreement to split — the ADR records the smaller number as the *discarded*
measurement, taken by padding the 2 MB with `"x"` and measuring TOAST compression rather than the
write. Only the config comment kept it.

## The decision

**What the caller's `files` channel already holds is charged against the same budget.**
`bound_tool_results` reads it off `request.state` — the same place `batch_width` already reads — and
`_bounded_file` divides what is left. An exhausted budget cuts to `bounded_content`'s brief form
rather than to nothing: the floor is 1 rather than 0, because 0 is how this setting is switched off
entirely, which `.env.example` now says.

The config comment carries the ADR's measurement and says why the number it held was the wrong one.

One thing the review asked for and this declines: at a share of 200 a 50,000-character file stores
44 characters, and that is not "all notice against the docstring's *keeps both ends*". It is
`bounded_content`'s brief-form branch, which exists because keeping the explanatory form floors every
result at ~312 characters and makes a batch total grow with the width — argued in place, with its
own measurement.

## What keeps it true

`tests/test_subagents.py::test_a_second_delegation_shares_the_budget_the_first_one_spent`, driven
through `bound_tool_results` — the shipped middleware — rather than on `_bounded_file`, because what
changed is that the bound now reads the caller's state and a test that called the helper directly
could not see whether the middleware passes it.

| mutation | result |
| --- | --- |
| the already-held characters ignored (`budget - 0`) | red, on the named assertion |
