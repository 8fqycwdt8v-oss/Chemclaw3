# D-2026-10-01-exact-helper-file-accounting-waits-for-a-helper-that-writes — the per-sibling share stays

**Status:** accepted · **Date:** 2026-10-01 · Closes the `BACKLOG.md` row *"The helper file budget is
charged to siblings that wrote nothing"* (issues #463, #489) by declining it with a trigger; the
item moves to `docs/planning/DEFERRED.md` (*Gated on a scale not yet reached*). Supersedes nothing:
`D-2026-09-18-a-pre-batch-snapshot-cannot-see-its-own-superstep`'s divisor stands as shipped.

## Context

`agent/tool_result_size._files_budget` divides what is left of `agent_subagent_files_max_chars` by
`batch_siblings` — the batch's calls naming `task` — because the siblings' results do not exist when
it runs. A helper filing a note beside silent siblings is therefore charged for company it did not
keep. The exact alternative is a trim over the **merged** `files` channel after the superstep, and
it is not free: the exemption that keeps a chemist's own documents out of this budget is
`agent/tool_result_shape.rewritten_command_files` comparing each command against the state *before*
it, so a post-merge trim would first need provenance on every channel entry (which `task` call, or
the chemist, put it there) — a reducer change on a checkpointed `DeltaChannel`, and
`test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with` is what a naive version
breaks.

The row asked for a measurement before that cost was paid: how often does a realistic batch
actually hit the per-sibling share at shipped widths?

## Measured

**On the live runs' own checkpoints** — every root-namespace thread in the live lane's Postgres
(612 threads, 1,891 assistant messages, including all three `make live-delegation` runs of
`D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model`), walked with
`AsyncPostgresSaver.alist`:

| | count |
|---|---:|
| assistant messages that called any tool | 1,232 |
| … of which named `task` at all | **7** (0.6%) |
| … of which named `task` more than once (the only case the divisor is > 1) | **2**, both width 3 |
| `task` calls in those batches | 11 |
| `task` calls whose `Command` carried a **non-empty** `files` update | **0** |
| threads whose `files` channel is non-empty at their last checkpoint | **0** |

The last row but one is re-runnable in one statement against the same database, and it is the
instrument this ADR's trigger names:

```sql
SELECT thread_id, length(blob) FROM checkpoint_writes WHERE channel = 'files';
```

All 11 rows are one byte — msgpack's empty map. Every helper the model spawned returned nothing to
the channel, so the per-sibling share was **reached zero times**: not "rarely cut", never consulted
with anything to cut.

**What it would cost when it is consulted**, driven through the shipped `bound_tool_results` at
the shipped 200,000-character budget with one writer beside silent siblings: the median note in
`knowledge/` (629 characters) and the largest (8,254) land whole at every width 1–8; a note has to
exceed ~66,000 characters before width 3 — the widest fan-out the live runs produced — cuts it, and
the largest helper output this repository has ever measured is the 70,048-character *report* of
`D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread`, which crosses
as a `ToolMessage` and is not charged to this channel at all.

## Decision

**Declined: exact post-merge accounting of the helper `files` channel, and the channel provenance
it would have to buy first.** The per-sibling divisor stays. It fails closed (an unclaimed share is
wasted allowance, never spent), it is exact whenever one helper runs, and the measured population
it could be unfair to is empty: no helper on the live lane wrote a file, and on this model a `task`
fan-out is itself a 2-in-1,232 event. Paying a reducer change on a checkpointed channel, plus the
risk to the chemist's-own-documents exemption, to recover allowance nobody has claimed is the
`D-2026-08-15-a-capability-that-ships-off-is-not-a-capability` shape — machinery whose only caller
is its own test.

Revisit when: a `files` write from a `task` call is non-empty — the `checkpoint_writes` query above
returns a row with `length(blob) > 1` on a deployment's or the live lane's database — **and**
`chemclaw_subagent_file_truncations_total` has moved on a batch where `batch_siblings` was above 1
(its WARNING line reads a share below `agent_subagent_files_max_chars`), or
`D-2026-09-27-delegation-does-not-pay-on-the-measured-gateway-model`'s own trigger fires and the
re-run delegation lane shows helpers writing files in parallel.

## Consequences

- `BACKLOG.md` loses the row; `DEFERRED.md` gains it under *Gated on a scale not yet reached*, with
  the measured value so the trigger is checkable rather than a feeling.
- `batch_siblings`' docstring stops pointing at a `BACKLOG.md` design and points here.
- Nothing in `src/` changes behaviour.
