# D-2026-08-05-one-rule-in-three-places-is-three-rules — One rule written in three places is three rules

**Status:** accepted · **Date:** 2026-08-05 · **Extends:** D-167 (the plan approval applies to the
act, not the session), D-137 (only a human moves the harness into execute), D-151/KM-14 (derive
once, cache behind the corpus fingerprint), KM-8 (a conflict is a flag on the evidence)

## Context

A review of the agentic engine, the harness wiring and the deep-research retrieval path looked for
one thing: **a rule the code states more than once**. Every finding below is an instance of it, and
two of them had already produced observable defects.

**1. The plan, emitted by two sites with two predicates.** `api/runner.py` streams the harness's
todo list as a `PlanEvent` from the turn loop and again after a mid-turn resume. The first guarded
on `if plan and plan != last_plan`; the second on `if current_plan is not None and current_plan !=
last_plan`, which admits `[]`. So a turn whose plan was cleared during the resume — what MAF's own
todo instructions tell the model to do when the chemist changes topic — emitted the empty checklist
`_current_plan`'s docstring says must never be produced. Measured: `plan events: [['step one'],
[]]`. A surface renders that as "the agent has no plan", the rendering reserved for an agent that
does not plan at all. Carried into this pass from the 2026-08-05 review's backlog.

**2. The harness dimensions, resolved from `profile-or-settings` in three places.** The rule
`X if profile.X is None else profile.X` was written out in `build_agent` (wire the harness at all),
in `_build_harness_agent` via `_resolved_autonomy` (starting mode, loop predicate) and in
`plan_gate.gate_applies` (attach the tool gate, spend the approval). That triplication has cost a
live defect once already: `api/runner.py` read `settings` directly instead, so a profile narrowed to
`plan_only` under a global `execute` got the gate attached and its approval never spent, and one
human decision authorized every later turn — `gate_applies`'s own docstring records it as DARK-1
recurring. The same file also resolved a profile's `instructions` twice, once in `build_agent` (dead
on the harness branch) and once inside `_build_harness_agent`, while the latter's docstring claimed
they were "pre-resolved by `build_agent`" — a sentence describing the code that should have existed.

**3. The conflict index, recomputed on every retrieval call.** `retrieval/retrievers.py` rebuilt the
whole-corpus conflict map inside *every* `SourceRetriever.retrieve`: once per `gather_evidence`
sweep under the default single-source config, three times with `vector` and `lexical` also enabled,
once per section of a development report, and again on the next sweep over an unchanged corpus. It
was the one artifact derived from the whole note tree that was not cached behind the stat
fingerprint — the parsed notes and the assembled graph both are (KM-14) — and it was the expensive
one. It also ran **on the event loop**: `load_notes` was offloaded to a thread and the scan that
follows it was not, so seconds of CPU sat between every other concurrent turn on that worker and its
next token.

Measured on a synthetic 2,000-note corpus, three sources enabled, one `gather_evidence` sweep:

| corpus shape | sweep | before | after |
| --- | --- | --- | --- |
| 7 substrates (programme-shaped) | first, cold | 3,915 ms | 2,249 ms |
| 7 substrates | second, corpus unchanged | 2,458 ms | 22 ms |
| 200 substrates | first, cold | 1,549 ms | 1,415 ms |
| 200 substrates | second, corpus unchanged | 98 ms | 19 ms |

Reproduce with the retrievers driven directly over a generated corpus; the numbers above are
`GraphRetriever` + `VectorRetriever` + `LexicalRetriever` under `asyncio.gather`, which is what
`gather_evidence` does.

## Decision

**1. One emitter for the plan.** `api/runner._PlanEmitter` holds the last-emitted plan *and* the
predicate, and both sites call `changed(session)`. Falsy covers both cases a plan must not be sent
for — no harness (`None`) and an empty checklist (`[]`) — so one predicate serves both. This is the
move `ToolCallTrace` already makes for streamed tool calls: state the loop carries across iterations
belongs with the decision that reads it. The two sites are now identical by construction rather than
by two people remembering the same rule.

**2. One resolver for the harness dimensions.** `agent/harness_mode.harness_enabled_for(profile)`
and `autonomy_for(profile)`, beside the `PLAN_MODE`/`EXECUTE_MODE` constants they belong with, plus
`PLAN_ONLY` for the autonomy value three decisions compare against. `build_agent`, the harness
builder and `gate_applies` all read through them; `_resolved_autonomy` is gone, and `instructions`
is passed into `_build_harness_agent` rather than re-derived — the docstring's claim is now true.
`gate_applies` takes an `AgentProfile` instead of `Any` while the seam is being tightened.

**3. The conflict index is cached like everything else derived from the corpus.**
`kg.conflicts.conflict_index(notes_dir, as_of)` owns the computation and the cache, keyed by the
notes' stat fingerprint **and** the date it was computed for — `find_conflicts(as_of=…)` scans only
the notes current on that day, so yesterday's map is a different answer rather than a stale one.
`kg.graph.cached_notes` and the `NotesFingerprint` alias become public, documented as the seam every
derived-from-notes cache keys on: a second derivation computing its own fingerprint would pay the
stat scan twice and could disagree with the first about whether the corpus had changed.

The lock is held across the *computation*, not merely around the dict access, which is the one place
this differs from the graph caches. The three sources of a sweep run concurrently, so a lock guarding
only the lookup would let all three miss together and compute the same answer three times in
parallel — the 3,915 ms first sweep above. A caller that waits waits exactly as long as the work it
would otherwise have redone.

`retrievers._conflict_index` becomes one `asyncio.to_thread` call, which is also what moves the scan
off the event loop.

## Consequences

- A plan that is cleared mid-turn now streams nothing rather than an empty checklist;
  `tests/test_runner.py` drives a resume that empties the todo list and fails against the previous
  code.
- A profile's harness posture is decided in one place, so "does the harness run" and "does the gate
  apply" cannot diverge. `tests/test_profiles.py` pins the agreement in the direction that matters:
  a profile asking for the approval-first posture under a deployment default of `execute` gets the
  harness *and* the middleware that gates it.
- Deep research pays the conflict scan once per corpus state instead of once per retriever call.
  Freshness is unchanged: any note added, edited or deleted moves the fingerprint and busts the
  entry, and `tests/test_conflicts.py` pins both halves — reuse on an unchanged corpus, recompute on
  a changed one — because a conflict nobody is shown because the flag went stale is exactly the
  failure KM-8 exists to prevent.
- **What this does not fix, and deliberately.** The cost that remains is `_suspected`, which is
  O(k²) in the number of notes sharing a `(type, compound_smiles)` — the shape a real programme
  produces, since an optimization campaign is many runs on one substrate. Over the 2,000-note /
  7-substrate corpus it emits **141,156** conflicts, so a chunk would carry ~141 `conflicts_with`
  ids into the model's context. That is not a caching problem; it is the heuristic's own
  productivity, and capping or re-scoping it changes what a chemist is shown. Recorded in
  `docs/planning/BACKLOG.md` with its measurement rather than guessed at here.

## Measurement notes

Two of the three hot spots this review suspected did not survive being measured, which is why only
one performance change is in this ADR:

- `api/runner._current_plan` runs a full harness todo `load_state` per streamed update, which
  looked like a per-token cost worth removing. It is **14 µs**; a 1,500-update turn spends 21 ms
  polling the plan. Left alone.
- `GraphRetriever.retrieve` builds an `EvidenceChunk` per matched note, unbounded, and
  `gather_evidence` then keeps 40 — 2,000 chunks built for 40 kept. The excerpt work for all 2,000
  is **5.8 ms**, against a 3 ms scan that is unavoidable. Bounding the list before the chunk build
  would have been a real change to how `hybrid` mode fuses ranks for no measurable gain. Left alone.
