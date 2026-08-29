# Anthropic Agent SDK features worth having here — implementation

Four items came out of an audit of the Claude Agent SDK against this repository's LangGraph
stack. Three are implemented here; the fourth is a design pass, because it touches the
middleware chain and the ADR has to come before the edit.

## 1. A turn's *spend* cap, beside its *iteration* cap — DONE

**The gap, corrected against the code rather than the prose.** The proposal said "Chemclaw only
caps by call count". That was half wrong: `api/budget.py` already meters tokens. What it does
with them is the gap — `check()` runs *before* a turn against usage already booked, and
`record()` books the turn *after* it finished. Nothing bounds spend **inside** a turn, and
`api/budget.py`'s own docstring states the belief that leaves the hole: "A single agent turn is
already iteration-capped, so one turn cannot loop forever." That caps *iterations*, not tokens.
A turn inside its 25-call ceiling can bill unboundedly — a wide fan-out of large tool results
against a 200k context is ~25 calls and millions of tokens — and the session budget finds out
one turn too late.

- [x] `agent/spend_cap.py`: meter in `wrap_model_call`, enforce in `before_model`
- [x] `billed_tokens` (`TurnTotal`) and `spend_capped` (`TurnFlag`) channels on `ChemclawState`
- [x] `agent_max_turn_billed_tokens` setting (0 = off, matching `_over`'s convention)
- [x] wire into `_middleware()` beside the loop cap
- [x] `spend_cap_reached` error code + `chemclaw_turn_spend_caps_total` + runner event
- [x] tests on a **compiled graph**, not on the hook

**Two design points, both measured rather than assumed.**

*Why `before_model` enforces.* `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`
— an `after_model` counter is short-circuited by any middleware that jumps from `after_model`.
`before_model` cannot be skipped. This is the same slot `loop_cap` occupies, for the same reason.

*Why the count is a state channel and not the ambient.* The turn's spend has to cross the
subagent boundary or a fan-out gets one budget each — regression 3 in `agent/loop_cap.py`'s
list. `TurnTotal` already folds concurrent writes additively. Probed on a compiled graph before
committing to it: `wrap_model_call` returning `ExtendedModelResponse(command=Command(update=…))`
reaches the channel and `before_model` reads it back — `[0, 100, 200]`, final `300`. The first
probe wrote a channel `ChemclawState` did not declare and LangGraph dropped it in silence,
which is the failure `tests/test_state_channels.py` exists to catch and is why the probe came
before the design.

## 2. Session fork — DONE

Branch a thread at its current checkpoint without mutating the original.

- [x] `agent/session_fork.py` — the copy, as SQL
- [x] `POST /sessions/{session_id}/fork`, authorized by the existing `resolve_session`
- [x] tests against a real Postgres schema

**Three things the research turned up that a naive fork gets wrong:**
- Every checkpoint PK leads with `thread_id`, so the fork is an `INSERT … SELECT` with the id
  swapped — no LangGraph API needed, and none exists (`adelete_thread` is the only thread-level verb).
- `checkpoint_blobs` is keyed `(thread_id, ns, channel, version)` and is **shared across a
  thread's checkpoints**, so copying only the tip loses channel values written at an earlier
  version. The whole thread is copied.
- A fork with no `session_messages` rows is **invisible** to `GET /sessions`: the owner listing
  `LATERAL`-joins `max(created_at)` and drops sessions with none. The transcript is copied too.
- The fork inherits the parent's **profile**, because a profile is attenuation-only and
  restoring the default would silently widen the tool surface.

## 3. Per-profile effort — DONE

- [x] `effort` on `AgentProfile` and `llm_effort` in settings
- [x] per-provider translation, gated the way `prompt_caching_middleware` is
- [x] tests asserting the constructed client, and asserting absence when unset

**Why this is not one shared kwarg.** The shipped chart runs `openai_compatible` against
`gpt-oss`, where `reasoning_effort` is a real parameter; `ChatAnthropic` has no such parameter
and spells the same idea `thinking={"type": "enabled", "budget_tokens": N}`, which additionally
must be under `max_tokens` and refuses a set `temperature`. So it cannot join
`_generation_options()`, whose contract is "caps both providers accept". An unset effort stays
**absent** from the request, which is that module's existing rule and matters more here than
elsewhere: a 400 from a rejected parameter is deliberately *not* failed over
(`_failover_exceptions`), so a bad value fails every turn rather than degrading.

## 4. Deferred connector tool schemas — DESIGN ONLY, NOT IMPLEMENTED

The most valuable of the four and the only one that needs an ADR before an edit, which is what
this item delivers. `docs/decisions/D-2026-08-29-a-tool-schema-nobody-calls-is-still-paid-for.md`
states the measurement, the design, the three rejected alternatives and the restart condition.
No code in `agent/` is touched.

## Review

**What shipped.** Three guards (`agent/spend_cap.py`, `agent/session_fork.py` +
`POST /sessions/{id}/fork`, `AgentProfile.effort`), two ADRs, 21 new tests, and one design document
for the fourth item. `make lint` and `make type` green.

**Four things found while building that were not the task.** Each is the same shape — a claim in
prose that the code did not support — which is why they are listed rather than quietly fixed:

1. **My own first finding was wrong in the reassuring direction.** "Chemclaw only caps by call
   count" — `api/budget.py` meters tokens and has done all along. The real gap was narrower and
   more interesting: both its halves sit *outside* the turn. Writing the proposal from the
   architecture docs rather than the code produced a finding that was true in outline and wrong in
   the part that decides the design.
2. **`tests/pg.py::create_checkpoint_tables` ran `MIGRATIONS[1:4]`** — three `CREATE TABLE`s, none
   of the `ALTER`s — while its docstring claimed "the shape under test is the shape production
   has". Invisible to every test that only `INSERT`s named columns; immediate for the first one
   driving a real saver. Fixed here, since a fork test cannot exist without it.
3. **`tests/test_context_floor.py` undercounts by 7,799 tokens (~24%)**, in a file whose docstring
   argues its number "is the payload rather than an approximation of it". `@tool` is identity, so it
   measures raw callables while `create_agent` binds larger wrapped objects (all 49 differ), and it
   never sees the 7 `FilesystemMiddleware`/`SubAgentMiddleware` tools. **Not fixed here** — the
   corrected floor (~39,983) exceeds the ceiling it would have to be measured against, and this
   repository's rule is that raising a ceiling is its own deliberate commit. `BACKLOG.md` row added.
4. **`events.py` said `loop_cap_reached` was the *only* error sharing its turn with an answer.**
   True until this change, false after it; corrected in the same commit rather than left to rot.

**One thing deliberately not done.** Item 2 is designed and unbuilt. It changes what the model can
see, inside the chain that authorizes tool calls, and its failure mode is a wrong answer that never
names the capability it needed — not a slow turn. The ADR carries the measurement, three rejected
alternatives, and an explicit **stop**: if the eval corpus cannot separate the deferred arm from the
bound arm on *tool selection*, the schemas stay bound. That is the D-2026-08-12/13 precedent applied
before the work rather than after it.

**A false alarm I raised and then disproved, kept because the reasoning error is the lesson.**
Five `tests/test_api_sessions.py` tests failed with `psycopg.errors.UndefinedObject: operator class
"bit_jaccard_ops" does not exist`, and I called them pre-existing and environmental on the strength
of reproducing them on a stashed clean tree. That check was **confounded**: the full suite was
running in the background at the time, so both arms ran two pytest sessions against one Postgres.
With the suite finished the same five pass. The cause was concurrency, not the database.
"Reproduces without my change" is not the same claim as "reproduces in isolation", and only the
second one was worth making.

**One genuinely pre-existing failure, established the second way.**
`tests/test_message_migration.py::test_erasure_still_works_where_the_checkpointer_has_never_run`
fails with the same error *in isolation, on a stashed clean tree, with nothing else running* —
which is the test the paragraph above should have been. Root cause is the sandbox database rather
than the code: the `chemclaw` database has only `plpgsql` installed (`pg_extension` lists no
`vector`, and `pg_am` has no `hnsw`), so the migration that builds a bit-vector index has no
operator class to name. Untouched by this change and left alone.

**What the full suite caught that nothing smaller did — six declaration registries.** Every one is
a place this repository makes you say out loud what you just added: a new turn outcome must be
reachable (`test_api_observability`), a new setting must be in `.env.example` (`test_config`), a new
`degraded()` subsystem must be declared (`test_degraded`), a new error code is mirrored by the UI
and mock repos (`test_event_contract`), a new metric needs a dashboard panel (`test_deploy_chart`),
a new `ChemclawError` subclass must be classified retryable or not (`test_publish`), and a new
session-scoped route must be in the ownership inventory (`test_service`). None of these is
reachable by running the tests for the thing you changed, which is the argument for running the
whole suite before believing any of it.
