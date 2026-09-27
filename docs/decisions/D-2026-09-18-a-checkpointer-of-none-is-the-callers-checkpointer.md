# D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer — A checkpointer of `None` is the caller's checkpointer

**Status:** accepted · **Date:** 2026-09-18 ·
**Closes:** the `BACKLOG.md` row attributing 20,712 kB per helper spawn ·
**Corrects:** `D-2026-09-12-a-helpers-scratch-file-crosses-into-its-callers-state`, which named the
right cause and said it could not be reached

## Context

`D-2026-09-12` measured one 2 MB helper write at **20,712 kB of checkpoint rows above baseline**,
bounded 1,824 kB of it (8.8%) with `agent_subagent_files_max_chars`, and attributed the other 91%
to "the helper's **own** subgraph checkpoints". A later `BACKLOG.md` row rejected that attribution
in both halves — the helper graph is compiled with no checkpointer, and `messages` is in upstream's
`_EXCLUDED_STATE_KEYS` — and concluded that "there are no helper checkpoints to account for
anything", that "compiling a helper with no checkpointer" was **a lever already spent**, and that
"looking for that object is a dead end".

Both of that row's premises are true. Its conclusion is false, and the difference is one keyword
argument.

**`None` is not "no checkpointer" to LangGraph.** A subgraph compiled without one *inherits* its
parent's through the run config — `CONFIG_KEY_CHECKPOINTER: checkpointer or
configurable.get(CONFIG_KEY_CHECKPOINTER)` in `langgraph/pregel/_algo.py`. `False` is the documented
opt-out, and `find_subgraph_pregel` skips "subgraphs that disabled checkpointing" by exactly that
test. `agent/langgraph_agent.py` passed neither: the helper branch omitted `checkpointer=`, so every
helper this repository has ever spawned checkpointed its own thread onto its caller's saver.

**The evidence was already in the tree, in a module that had measured it.**
`agent/checkpointer.py`'s prune carries a `PARTITION BY checkpoint_ns` whose comment reads: "A turn
that spawns the `task` helper writes a subgraph namespace beside the root one on the *same*
`thread_id` — measured, one `tools:<uuid>` namespace per `task` call". `tests/test_checkpointer_prune.py`
drove a real `task` call to exercise it. Three other places said the opposite, and the one that made
the belief durable was a test: `test_the_helper_graph_is_compiled_without_a_checkpointer` parsed the
AST, found no `checkpointer=` keyword, and concluded there was no checkpointer — reading the very
absence that causes the inheritance as proof of its impossibility. That is the failure
`tests/test_context_floor.py`'s docstring already names: *a basis that is re-derived rather than
observed will agree with itself forever*.

## Decision

The helper graph is compiled with **`checkpointer=False`**.

## What it costs and what it reclaims — measured, on a real `AsyncPostgresSaver`

One turn spawning one helper that writes 2 MB of incompressible text (`os.urandom(1_000_000).hex()`,
because the first attempt at this measurement in `D-2026-09-12` padded with `"x"` and measured TOAST
compression), tables truncated between arms:

| arm | `checkpoints` | `checkpoint_blobs` | `checkpoint_writes` | total |
| --- | --- | --- | --- | --- |
| baseline, before | 112 kB | 80 kB | 80 kB | **272 kB** |
| 2 MB write, before | 112 kB | 12,368 kB | 6,464 kB | **18,944 kB** |
| baseline, after | 88 kB | 48 kB | 48 kB | **184 kB** |
| 2 MB write, after | 88 kB | 48 kB | 288 kB | **424 kB** |

**A spawn cost 18,672 kB above baseline and now costs 240 kB — 78x less; on table totals, 18,944 kB against 424 kB, 45x.** (Both ratios, because the two divide differently and quoting one as the other is this record's own recurring defect: 18,672/240 is 77.8, 18,944/424 is 44.7.) Grouping the rows by
`(checkpoint_ns, channel)` — the step neither the ADR nor the row took — is what attributes it:

```
ns='tools:3780b296-…'  messages        n=6   9,888 kB   (blobs)
ns='tools:3780b296-…'  __pregel_tasks  n=4   1,953 kB   (blobs)
ns='tools:3780b296-…'  messages        n=6   2,013 kB   (writes)
ns='tools:3780b296-…'  __pregel_tasks  n=2   1,953 kB   (writes)
ns='tools:3780b296-…'  files           n=2   1,953 kB   (writes)
ns=''                  files           n=1     195 kB   (writes)   ← the capped crossing
ns=''                  messages        n=4       2 kB   (blobs)
```

~15.7 MB of raw blob bytes sits under one `tools:<uuid>` namespace **on the caller's own
`thread_id`**. **The two measures differ and both are given on purpose**: the group-by sums
`octet_length(blob)` and so attributes 83% of 18.9 MB, while *removing* that namespace takes the
table totals from 18,944 kB to 424 kB — 97.8%. The gap is row, index and TOAST overhead the
group-by does not count, so the attribution is the conservative number and the removal is the
measured one. The
the single largest item is the helper's `messages` channel re-serialised per version — the channel
`_EXCLUDED_STATE_KEYS` keeps out of the caller's *state*, which was never the same claim as keeping
it out of the caller's *checkpoints*. So `D-2026-09-12`'s attribution was right; only its "this cap
cannot reach them" was, and it remains, correct — a different lever was needed, and it existed.

## What is given up

Resuming a turn *inside* a helper, and time-travel over a helper's thread. Nothing here can reach
either: `interrupt()` has no caller in `src/`, the two tools that could ask a question
(`ask_clarifying_question`, `request_external_input`) are subtracted from every helper's surface, and
a new user message starts a turn rather than resuming one mid-graph. A crash mid-helper re-runs the
helper from its start, which is what the caller's own checkpoint would have produced anyway. The cost
is stated rather than discovered: a deployment that later gives a helper an interrupting tool must
revisit this line, and the test below is where it will find out.

## Consequences

- `agent/checkpointer.py`'s `PARTITION BY checkpoint_ns` **stays**. It is generic over namespaces,
  it costs nothing, and the leak it prevents is silent — but no shipped path now writes a second
  namespace, so `tests/test_checkpointer_prune.py` writes one through the saver instead of getting
  one from a real `task` call.
- `agent_subagent_files_max_chars`' own comment no longer quotes 10.4x. The amplification it was
  justified by was ~98% a cost this setting never touched.

## A second defect, found beside it and fixed in the same commit

`_bounded_file` floored the *budget* at 1 and then divided: `share = max(budget - held, 1) //
sharing`. With the channel already at the budget and two files crossing, `1 // 2 == 0` — and 0 is how
`agent_subagent_files_max_chars` is switched **off** (`bounded_content` returns uncut at
`limit <= 0`). Measured: two 500,000-character files against an exhausted budget stored **1,000,000
characters uncut**, with nothing logged and `chemclaw_subagent_file_truncations_total` unmoved. The
cap failed open exactly where the channel was fullest. The floor now applies after the division, as
the sibling `bounded_for_batch` already did and says why.

## What keeps it true

- `tests/test_subagents.py::test_a_helper_writes_no_checkpoint_of_its_own` — drives a real spawn
  against a real `AsyncPostgresSaver` and reads the namespaces off `checkpoints`. Mutated by
  restoring the omitted keyword: `{'': 15, 'tools:3d8b490d-…': 17}`. It asserts the caller's own
  rows are non-empty and the helper ran, so it cannot pass by measuring nothing.
- `tests/test_subagents.py::test_an_exhausted_budget_still_cuts_when_more_than_one_file_crosses` —
  mutated by flooring the budget instead of the share, stores 1,000,000 characters.
- `tests/test_checkpointer_prune.py::test_every_namespace_of_a_thread_is_bounded_and_not_only_the_root`
  — unchanged in what it guards, driven on a namespace it writes itself.
