# Task: deep review of the agentic engine, the agent harness and deep-research mode

Branch: `claude/agentic-engine-refactor-b1la3v`. Decision:
`docs/decisions/D-2026-08-05-one-rule-in-three-places-is-three-rules.md`.

**The review had one lens: find a rule the code states more than once.** In `agent/`, `api/runner.py`
and the retrieval path that `gather_evidence` drives, every duplicated rule found was either already
producing a defect or one edit away from producing one — and the one performance finding was the
opposite shape, a derivation stated *nowhere* as reusable and therefore repeated on every call.

_(The previous occupant of this file was the live-test lane for Temporal + durable workflows + LLM +
Postgres, `docs/decisions/D-2026-08-04-a-lane-that-only-runs-where-docker-runs.md`; it is in
`git log`.)_

---

## Plan

- [x] **1. Read the engine end to end** — `agent/` (33 modules), `api/runner.py`, the harness wiring
      (`chemclaw_agent`, `harness_mode`, `harness_todo`, `plan_gate`, `loop_cap`), and the
      deep-research path (`research_tools`, `retrieval/retrievers`, `hybrid`, `verifier`,
      `runner_answer`, the `deep-research` skill).
- [x] **2. Measure the three suspected hot spots before changing any of them.** Two did not survive
      the measurement and were left alone (see Review).
- [x] **3. One emitter for the plan** — `_PlanEmitter` in `api/runner.py`, both sites through it;
      the post-resume site can no longer emit `[]`.
- [x] **4. One resolver for the harness dimensions** — `harness_enabled_for` / `autonomy_for` /
      `PLAN_ONLY` in `agent/harness_mode.py`; `build_agent`, `_build_harness_agent` and
      `plan_gate.gate_applies` all read through them. `_resolved_autonomy` deleted, `instructions`
      passed rather than re-derived, `gate_applies` typed.
- [x] **5. Cache the conflict index behind the notes fingerprint** — `kg.conflicts.conflict_index`,
      `kg.graph.cached_notes`/`NotesFingerprint` made public as the seam it keys on, and the whole
      scan moved off the event loop.
- [x] **6. Tests, ADR, backlog.** 7 new tests; the `PlanEvent` one fails against the previous code.
      One measured finding handed to `BACKLOG.md` as a decision rather than patched.

---

## Review — what was actually measured

**The plan emitted as an empty checklist.** Two emit sites, two predicates: the turn loop guarded on
truthiness, the post-resume site on `is not None`. A turn that empties its todo list during a
mid-turn resume — what MAF's own instructions tell the model to do when the chemist changes topic —
produced `plan events: [['step one'], []]`, and an empty `PlanEvent` renders as "the agent has no
plan", the rendering reserved for an agent that does not plan at all.
`test_an_emptied_plan_is_not_streamed_as_an_empty_checklist` fails against the previous code
(verified by stashing `src/` and running it).

**The harness posture, decided three times.** `X if profile.X is None else profile.X` appeared in
`build_agent`, in `_resolved_autonomy` and in `gate_applies`. That triplication has already cost one
live defect: `api/runner.py` read `settings` directly, so a `plan_only` profile under a global
`execute` got the gate attached and its approval never spent, and one human decision authorized
every later turn. `_build_harness_agent` also re-derived the profile's instructions while its
docstring said `build_agent` had pre-resolved them — a sentence describing code that did not exist.
Both are one call now.

**The conflict index, recomputed on every retrieval call — the only measured win.** It was the one
whole-corpus derivation not cached behind the stat fingerprint (the parsed notes and the assembled
graph both are), and it ran on the event loop. On a 2,000-note corpus over 7 substrates, a
three-source `gather_evidence` sweep:

| | before | after |
| --- | --- | --- |
| first sweep, cold corpus | 3,915 ms | 2,249 ms |
| second sweep, corpus unchanged | 2,458 ms | 22 ms |

(200 substrates: 1,549 → 1,415 ms cold, 98 → 19 ms warm.) The cold-sweep half comes from holding the
lock across the computation, so three concurrent sources miss once between them instead of three
times in parallel.

**Two suspected hot spots did not survive being measured, and that is the useful half of this
review.** `_current_plan` runs a full harness todo `load_state` per streamed update, which looked
like an obvious per-token cost: it is 14 µs, or 21 ms across a 1,500-update turn. `GraphRetriever`
builds 2,000 evidence chunks for the 40 `gather_evidence` keeps, which looked like obvious waste: the
excerpt work for all 2,000 is 5.8 ms against a 3 ms unavoidable scan, and bounding the per-source
list would have changed how `hybrid` mode fuses ranks for no gain. Both left exactly as they were.

**What was found and deliberately not fixed.** `conflicts._suspected` is O(k²) in the notes sharing
a `(type, compound_smiles)` and emitted 141,156 conflicts over that same corpus — ~141
`conflicts_with` ids per evidence chunk reaching the model. Caching bounds how often that is paid
and nothing about whether it is worth showing. Capping it changes what KM-8 tells a chemist, so it
went to `BACKLOG.md` with its numbers.

**Gate:** `make lint type test` green — 3,243 passed, 135 skipped (3,236 before; +7 new tests).
