# D-2026-09-18-a-bound-on-what-a-helper-adds-is-not-a-bound-on-what-its-caller-had — A bound on what a helper adds is not a bound on what its caller had

**Status:** accepted · **Date:** 2026-09-18 ·
**Corrects:** `D-2026-09-18-a-checkpointer-of-none-is-the-callers-checkpointer`, merged the same day
— one regression it introduced, one scope claim it got wrong, and three of its own numbers

## Context

Four fresh-context reviews were run over that commit, each on one dimension and each driving code
rather than reading it. The engineering held: the mechanism claim, the measurement table, both
ratios, the predicate, the upstream citations for `_EXCLUDED_STATE_KEYS`, and the unreachability of
a mid-helper resume were all independently reproduced, and every mutation the new tests exist for
goes red. What did not hold is below.

## 1. The cap it fixed was cutting files the helper never wrote

`_bounded_file`'s fail-open — `1 // 2 == 0`, and 0 is "no cap" — was real and the fix was right in
isolation. What the fix did **not** account for is what the `Command` it rewrites actually contains.
deepagents hands a subagent every non-excluded key of its caller's state and copies them all back
(`_EXCLUDED_STATE_KEYS` is `messages`, `todos`, `structured_response`), so `files` is the caller's
**whole** channel, not the helper's contribution to it. Every file in it was cut. Below the budget
that was a re-cut nobody had noticed; at an exhausted channel the new floor made it destruction:

```
caller's own /scratch/mine.md, 200,000 chars, helper returns
  before this commit:  200,000  (the fail-open preserved it)
  after  that commit:       45  ('[200,000 chars cut] [system …]')
```

— with the WARNING beside it reading *"cut 200000 character(s) from a file a helper wrote"*. The
helper had never touched it. **The fail-open that commit closed had been accidentally protecting a
chemist's documents.**

**Decision:** bound only the paths whose text differs from what the caller already holds, and count
`sharing` over that set. This cannot lose anything, which is what makes it a plain defect rather
than a trade: upstream's reducer is `result[key] = value`, so re-delivering an unchanged file is a
no-op on the channel and cutting it could only ever have cost bytes.

It also makes the bound **exact** where it was both destructive and under-used, because every
document the caller carried used to dilute the share:

| caller holds | helper's 800,000-char file, before | after |
| --- | --- | --- |
| 200,000 (exhausted) | caller's file destroyed; helper 45 | caller's preserved; helper 45 |
| 150,000 | caller's cut to 25,000; helper 25,000 | caller's preserved; helper 50,000 |
| 2,000 | caller's preserved; helper 99,000 | caller's preserved; helper 198,000 |

## 2. It fixed one instance of a class and declared the class closed

That commit's Consequences said "no shipped path now writes a second namespace", and
`agent/checkpointer.py` said the same in the present tense. Both were false when written.
`retrieval/fanout.py` compiled its fan-out with a bare `graph.compile()` — checkpointer `None`, the
exact spelling the commit had just condemned — and `sweep_sources` runs inside `gather_evidence`,
which is bound on the caller's surface **and** the helper's. Driven on a real saver, a subgraph
compiled that way and invoked inside a turn writes its own namespace:

```
compile()                     namespaces: [('', 3), ('call:214bbd77-…', 3)]
                              biggest blob: ('call:…', 'ranked', 195 kB)
compile(checkpointer=False)   namespaces: [('', 3)]
```

195 kB of *retrieved corpus* checkpointed on the chemist's own `thread_id`, under a channel no cap
reaches — `agent_subagent_files_max_chars` does not apply and the fan-in is pre-merge — for a graph
nothing resumes.

**Decision:** `retrieval/fanout.py` compiles with `checkpointer=False`, and the guard is on the
**class**: `test_every_compiled_graph_in_this_tree_names_its_checkpointer` fails any `.compile()` in
`src/` that does not say what it wants. Syntactic on purpose, and defensible here where an AST
reading of an absent keyword was not — the hazard *is* the default, so what the assertion asks is
that every compile site states a choice rather than inheriting one.

**A related scope claim stands corrected rather than fixed.** `checkpointer=False` stops a graph
adopting its parent's saver; it does not stop a *grandchild* compiled with `None`, because
`_algo.py` re-propagates whatever is in the incoming config. So "a helper writes no checkpoint of
its own" holds for a helper that calls no tool which invokes a compiled graph — which, with the
fan-out fixed, is now every tool it holds. The class guard is what keeps that true.

## 3. Its mechanism citation named a function never consulted on that path

The comment said `False` works because "`find_subgraph_pregel` skips subgraphs that disabled
checkpointing". Patched over a real spawn, that function is called 15 times and never once sees the
helper: it scans *node-bound* runnables, and a helper is invoked from inside a tool. The resolution
is `Pregel._defaults` — `if self.checkpointer is False` **before** the config lookup — and
`langgraph.types.Checkpointer` documents the three values outright. Now cited, and
`test_a_subgraph_compiled_without_a_checkpointer_inherits_its_parents` pins both, because a
docstring is the weakest kind of promise: if `None` ever meant "none", every `checkpointer=False`
here would become a no-op that reads as deliberate.

## 4. Three of its own numbers were wrong, in the commit about numbers being wrong

- **"~15.7 MB of raw blob bytes / 83%"** — the printed rows sum to **17,760 kB, 93.8%**. No
  principled subset gives 15.7 MB. Three of the five rows are `checkpoint_writes`, so "blob bytes"
  mislabels them too. Corrected in the four places that carried it.
- **"~98% of 20,712 kB"** — that figure is `D-2026-09-12`'s arm, and that arm's own cap reclaimed
  8.8% of it, so at most 91.2% can be the helper's thread. 97.8% is the reduction on *this*
  commit's arm (18,944 → 424). Carrying a percentage across two bases is the defect the paragraph
  was written to describe.
- **"stores 1,000,000 characters"** for a mutation — the test uses `budget * 4` per file, so the
  mutant stores **1,600,000**. 1,000,000 is the standalone probe's number, transcribed onto a
  different fixture.

The merged ADR is not edited, per the standing rule; this one is the record. Four surviving copies
of a *fourth* stale claim — the per-turn spend cap "ships at 0" when it ships at 300,000 — are
corrected here too, including `docs/guides/runbook.md`, which is the operator-facing copy an
on-call engineer reads, and a `docs/decisions/README.md` cell that asserted both values at once.

## Consequences

- A helper returning no longer touches its caller's documents. `chemclaw_subagent_file_truncations_total`
  now counts only what a helper actually added.
- One `gather_evidence` sweep stops writing ~195 kB of corpus per fan-out into the chemist's thread.
- `agent_subagent_files_max_chars` now spends its whole budget on the helper's contribution, so the
  effective allowance for a delegated read went *up* without the setting changing.

## What keeps it true

- `tests/test_subagents.py::test_a_chemists_own_file_survives_a_delegation_it_had_nothing_to_do_with`
  — drives the faithful `Command` shape through the shipped middleware. Mutated by dropping the
  skip: the caller's file comes back at 45 characters.
- `tests/test_upstream_surface.py::test_every_compiled_graph_in_this_tree_names_its_checkpointer` —
  mutated by restoring the bare compile, names `retrieval/fanout.py`.
- `tests/test_upstream_surface.py::test_a_subgraph_compiled_without_a_checkpointer_inherits_its_parents`.
- `tests/test_subagents.py::test_the_file_cap_set_to_zero_is_off_rather_than_absolute` — the
  `else: share = 0` branch that commit introduced had no test, and `share = 1` survived 80 of them.
- `tests/test_checkpointer_prune.py::test_every_namespace_of_a_thread_is_bounded_and_not_only_the_root`
  — its fixture now writes channel values and pending writes, so the `checkpoint_ns` predicates in
  `pruned_writes` and `pruned_blobs` are guarded. Dropping either survived this file in **both** the
  old and new versions; it now fails.
