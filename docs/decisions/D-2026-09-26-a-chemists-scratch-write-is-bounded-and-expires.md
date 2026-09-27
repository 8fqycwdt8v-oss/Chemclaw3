# D-2026-09-26-a-chemists-scratch-write-is-bounded-and-expires — a turn's own file is refused past a cap and kept 90 days by default

**Status:** accepted · **Date:** 2026-09-26 · **Decided by the owner, 2026-09-26.** Closes the
`BACKLOG.md` row *"A chemist's own `/scratch/` writes are unbounded and, by default, permanent"*
(issue #462).

## Context

`agent_subagent_files_max_chars` bounds only what a *helper* hands back. A caller's own `write_file`
and `edit_file` reach `StateBackend`, which writes the `files` channel directly and passes no
middleware, so nothing bounded a turn's own file. The channel is checkpointed under the thread and
accumulates, and in the shipped configuration nothing deleted a file from it: `retention_enabled`
is off, `retention_checkpoints_days` is 0, and `checkpoint_retain_per_thread` prunes superseded
copies only. The row kept this a decision because a cap here acts on a chemist's own document.

## Decision

**A per-write cap that refuses.** `agent_scratch_file_max_chars` (default 200,000, the same number
as the helper budget whose channel every such file is charged against) is enforced by
`agent/scratchpad.BoundedStateBackend` for `/scratch/` and by `BoundedStoreBackend` for
`/memories/`. A write, or an edit whose *result* would be longer, is refused with a message naming
the setting and saying nothing was stored. Never truncated: a cut hands the chemist a document that
simply stops, while a refusal lets the model split the file or write less.

**A default retention of 90 days.** `agent_scratch_retention_days` (0 = keep for ever, set
explicitly) removes any file in the thread's `files` channel whose upstream `modified_at` is older
than the window, at the start of the thread's next turn (`expire_stale_scratch`, a `before_agent`
hook), through the channel's own reducer.

Two routes through "the existing retention machinery" were weighed and not taken:

- **Defaulting `retention_checkpoints_days` to 90.** That disposes of the thread *whole*, and a
  turn's context comes only from the checkpoint — `session_messages` is never read back into the
  graph — so the next turn on that session would run with no history at all. It is also gated on
  `retention_enabled`, off by default, so the default would have deleted nothing.
- **A sweep that deletes files from stored checkpoints.** `files` is a `DeltaChannel` (upstream
  marks its on-disk shape beta), and `update_state` on any graph but the turn's own drops every
  channel that graph does not declare. Neither is a safe thing for a background activity to do.

## Consequences

- A thread nobody resumes keeps its files until the thread is disposed of by
  `retention_checkpoints_days` — a stated policy. That half is not closed here, and it is said in
  the setting's comment and in `.env.example` rather than implied.
- The window is not gated on `retention_enabled`: it disposes of a turn's working surface, not of
  a record, and the owner chose a default rather than a policy each deployment must state.
- `tests/test_scratchpad.py` drives both through the compiled graph: at-cap lands and over-cap is
  refused, an edit growing a file past the cap is refused, the memory route refuses the same way,
  a file at 91 days is removed while one at 89 days and an undated one are kept, and 0 keeps all.

Revisit when: upstream gives `DeltaChannel` a stable on-disk format or a supported way to update one
channel of a stored thread, at which point an idle thread's stale files can be swept without running
a turn.
