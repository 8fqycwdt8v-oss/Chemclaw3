# D-2026-09-12-a-helpers-scratch-file-crosses-into-its-callers-state — A helper's scratch file crosses into its caller's state

**Status:** accepted · **Date:** 2026-09-12 ·
**Extends:** `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread` — which measured
the *thread* and was right about it

## Context

That ADR measured a helper's isolation on a compiled graph: a helper reading ~9.8 kB leaves its
caller a thread of **57 characters**, prompt to answer. It is a real and load-bearing property, and
it is about one of the two things `task` hands back.

Upstream's `_return_command_with_state_update` builds the `Command` from
`{k: v for k, v in result.items() if k not in _EXCLUDED_STATE_KEYS and k not in private_state_keys}`
— **every** non-excluded key of the helper's final state, `files` among them. Driven on the same
fixture with a 2 MB scratch write:

```
caller state keys: ['files', 'messages', 'model_calls']
files in the caller's state: {'/scratch/evidence.md': 2000137}
caller thread chars: 57
```

So the thread number was never wrong and never covered this. Nothing bounded it: the two controls
that go through `agent/tool_result_shape.py` rewrite `update["messages"]`, which is the report.

## Decision

`rewritten_command_files` is the sibling of `rewritten_tool_messages` in the same module, and
`bound_tool_results` applies both. A file's text is cut with `bounded_content` — the one place this
repository cuts text — so a truncated file keeps both ends and carries the same system-marked notice
a truncated tool result does. That matters because the caller **can** read the file back
(`read_file` reaches the file that crossed), and a silent cut hands a chemist a document that simply
stops.

**`agent_subagent_files_max_chars` (200,000) is a separate number from
`agent_max_tool_result_chars`, because it bounds a different resource.** That one is context — what
a model is sent. This is storage. And it is a **total** that several files share, by the same
division `bounded_for_batch` applies across a batch of tool calls: a per-file cap times an unbounded
number of files is not a bound.

Rewritten **in place** rather than through upstream's `create_file_data`, because rebuilding a file
restamps `created_at` — a helper's file would arrive looking newer than it is.

## What it costs, and what it does not fix — measured, on a real `AsyncPostgresSaver`

The first attempt at this measurement was wrong and worth recording: padded with `"x" * 2_000_000`
it showed 552 kB total and no meaningful difference with the cap on, because that payload compresses
to nothing in TOAST. Re-run with incompressible text (`os.urandom(1_000_000).hex()`), one turn that
spawns one helper writing 2 MB, tables truncated between arms:

| arm | `checkpoints` | `checkpoint_blobs` | `checkpoint_writes` | total |
| --- | --- | --- | --- | --- |
| baseline (helper writes a few bytes) | 128 kB | 88 kB | 80 kB | **296 kB** |
| 2 MB write, cap off | 128 kB | 12,528 kB | 8,352 kB | **21,008 kB** |
| 2 MB write, cap on | 128 kB | 12,528 kB | 6,528 kB | **19,184 kB** |

**One 2 MB helper write costs 20,712 kB of checkpoint rows above baseline — 10.4x — and this cap
reclaims 1,824 kB of it, 8.8%.** That is exactly the 1.8 MB cut, appearing once, in the caller's
channel. It is the whole of what the item asked for and it is a minority of the cost, and both
halves belong in the record.

The other 91% is the helper's **own** subgraph checkpoints: `checkpoint_blobs` carries the helper's
`files` and `messages` channels re-serialised per version, and this cap cannot reach them — it runs
in `wrap_tool_call` when `task` *returns*, by which time the helper's own checkpoints are written.
That is a different unbounded surface with a different lever (a bound on `write_file`'s content
argument, or compiling a helper without a checkpointer), and it is on `docs/planning/BACKLOG.md`
with this measurement rather than implied to be fixed here.

**It is a one-off per spawn, not a recurring cost**, which was a hypothesis worth checking and
falsifying: two further turns on the same thread with no helper in them added **56 kB** in both
arms.

## Consequences

- A helper's scratch file over the budget reaches its caller cut, with a notice. The caller's model
  reading it back sees both ends and the notice; nothing is silently lost.
- `chemclaw_subagent_file_truncations_total` counts it, apart from the tool-result truncation series
  because it bounds a different resource.
- A helper writing many files gets a share each. A helper writing one gets the whole budget.
- The `FileData` shape (`{"content": str, …}`) is now an assumption this repository reads, so it is
  asserted in `tests/test_upstream_surface.py` like the six before it. If it moves, the bound goes
  quiet on every file — the exact failure mode that file exists to turn red.

## What keeps it true

- `tests/test_subagents.py::test_a_helpers_scratch_file_is_bounded_on_its_way_into_its_callers_state`
  — driven through a real spawn, with the thread asserted alongside so the bound cannot be achieved
  by routing the file through the messages instead. Mutated by removing the call:
  `800000 characters … against a 200000-character budget`.
- `tests/test_subagents.py::test_a_cut_file_says_it_was_cut` — same mutation, fails on the absent
  system mark.
- `tests/test_subagents.py::test_several_files_share_one_budget` — mutated to a per-file cap:
  `four files … stored 800000 against a 200000 budget`.
- `tests/test_upstream_surface.py::test_a_file_a_helper_hands_back_is_a_mapping_carrying_its_text_under_content`
  — mutated to rebuild through `create_file_data`, fails on the restamped `created_at`; mutated to
  drop the other update keys, fails on the missing `model_calls`.
