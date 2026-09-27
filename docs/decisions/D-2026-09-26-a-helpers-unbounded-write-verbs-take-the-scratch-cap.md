# D-2026-09-26-a-helpers-unbounded-write-verbs-take-the-scratch-cap — `write_file` and `edit_file` are bounded by the scratch cap; the over-charge stays open

**Status:** accepted · **Date:** 2026-09-26 · **Decided by the owner, 2026-09-26.** Closes the
*unbounded* half of the `BACKLOG.md` row *"The helper file budget is charged to siblings that wrote
nothing, and two write verbs are charged to nobody"* (issue #463); the over-charge half stays a row.

## Context

The row has two halves over one resource, the caller's `files` channel. `write_file` and
`edit_file` reach it through `StateBackend`'s channel write and return a plain `ToolMessage`, so
they never pass `tool_result_shape.rewritten_command_files` and nothing bounded them — while
everything they store is charged into `held` against every later helper's share of
`agent_subagent_files_max_chars`. The other half is that `batch_siblings` divides the budget by the
batch's `task` calls rather than by its writers, which is lost allowance rather than a hole.

## Decision

**The two verbs take the same bound as a chemist's own scratch write**
(`D-2026-09-26-a-chemists-scratch-write-is-bounded-and-expires`): `agent_scratch_file_max_chars`,
enforced in `agent/scratchpad.BoundedStateBackend`, the backend both verbs reach, so a helper's
write and a caller's are one code path and one number. A second, helper-specific cap was not added:
the resource is the channel, the verbs are the same verbs in either frame, and two numbers for one
write would be a choice nobody could explain.

**The over-charge is not done here.** Exact accounting needs a trim over the *merged* channel after
the superstep, and that trim has nothing to compare against to keep a chemist's own documents out
of the budget — it has to buy channel provenance first. That remains the row, rewritten to the half
that is still open.

## Consequences

- A single write can no longer spend more than the cap of the helper budget; a turn can still fill
  the channel with many files under it, which the channel budget's own `held` subtraction charges.
- `tests/test_scratchpad.py` holds both verbs through the compiled graph.

Revisit when: the channel carries per-key provenance (which frame wrote each file), at which point the
exact post-merge trim becomes buildable and the over-charge row can close.
