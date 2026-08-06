# D-2026-08-06-a-turn-that-does-not-finish-cleanly — A turn that does not finish cleanly

**Status:** accepted · **Date:** 2026-08-06

## Context

Seven backlog rows that turn out to share one shape: **the happy path was tested and the exit was
not.** A turn that answers, a job that completes, a search that finishes — all covered. A turn the
client abandons, a job that fails, a search a chemist cancels — none.

- `run_turn` abandons the agent's `ResponseStream` on every non-exhausting exit [L]
- the mid-turn resume drops `user_input_requests` [L]
- a failed durable job is dropped from the resume, and the docstring says it is not [L]
- `beating()` abandons the work it wraps when the activity is cancelled [L]
- `evals.live`'s `failed_loudly` is unconditionally true [M]
- sessions bricked by a stranded `tool_result` have no self-heal path
- VIBE-1(a): a domain refusal reaches the model as `WorkflowFailureError` after five retries

## Decision

### The agent's stream is closed, because upstream cannot be asked to

Measured on `ResponseStream` directly rather than reasoned about. It runs its cleanup hooks and
finalizer from `__anext__`, on exactly two paths — the source raising `StopAsyncIteration`, and the
source raising anything else. It exposes **no `aclose()` and no `__aexit__`**, and it is a plain
object rather than an async generator, so Python's own async-generator finalization does not apply
to it either:

```
async for x in ResponseStream(source(), cleanup_hooks=[...]):
    break
# source's `finally` has not run; the cleanup hooks never run at all —
# not on GC, not at loop shutdown.
```

Two corrections to the row. The cleanup hooks never run *at all*, which is stronger than "on this
exit". But the underlying async generator **is** eventually finalized by asyncio's GC hook — 250 ms
later in the probe, and only once nothing references it. That is the leg that matters today, because
it is the one holding the HTTP response to the model open, and deferring it to a garbage collection
is how a connection pool runs out under load while every request looks finished.

So `api/runner.py:_closing` wraps both `agent.run` sites: it closes the underlying iterator and runs
the hooks upstream would have run. Both are private attributes, read defensively — the honest cost of
the gap, and registered in `DEFERRED.md` so it ends with being deleted.

`run_turn` is now typed `AsyncGenerator` rather than `AsyncIterator`. That is a contract, not a
detail: a caller that stops early **must** close it, and closing is what releases the stream and
rolls the turn back. Under the weaker type the obligation was invisible to callers and to mypy.

### A refused or failed job says so, in the turn

`handle.result()` **raises** for a workflow that failed, was cancelled, timed out or was
terminated. `return_exceptions=True` captured the raise and the job vanished from the resume's map —
indistinguishable from one that had not finished, so the model resumed with no mention of the
failure. A chemist reads silence as success. That is exactly what the function's own `Returns:` said
must not happen.

The catch asks `job_status` rather than composing a status word, because the raise is the same object
for all four terminal states: composing here would report every one as "failed", and would give the
resume a second opinion about a run that `get_durable_job_status` already answers for.

The resume also emits `ApprovalRequestEvent` now. The first pass always did; the resume loop simply
had no such branch. The consequence is not a missing UI element — the turn continues as though the
chemist had been asked, so it is the plan gate silently not applying.

### Cancelling a wait cancels the work — as far as that is possible

`asyncio.wait` does not cancel what it waits on. Every exit from `beating()` that was not "done"
left the task running with nobody awaiting it, so a `cancel_job` returned promptly and the work
carried on; if the orphan later raised, the exception was never retrieved either.

**Necessary and, measurably, not sufficient.** A coroutine is cancelled properly. A `to_thread` —
which is what both `bo` propose activities are — cancels only the *future*, and the pooled thread
runs the BoFire fit to completion. calc's CREST and xtb subprocesses are bounded by `run_isolated`'s
own timeout and its process-group kill, so their burn ends at that timeout rather than never. The
remainder is a property of `to_thread`, not of this function, and is in `DEFERRED.md` with the
condition that would close it — not claimed as fixed here.

### A degraded capability is not a failure

`failed_loudly` was `tools_failed or error_code or degraded`. A `capability_degraded` event is the
system *working*: it announces, before the first token, that this turn's tool surface is short.
Counting it as a visible failure was tolerable while only an unreachable connector raised it. Then
the per-turn Temporal probe started raising it on any deployment without a broker — every offline
run and every local one — and `failed_loudly` became unconditionally true. The harness's headline
signal is `not answered and not failed_loudly`, so the silent death the whole module exists to find
could no longer be reported by it.

This is the "a metric that cannot report its own subject" family again, and the correction is the
rule the field's own docstring already stated: loud means a `tool_failed` or an `error`. Degradation
is a separate axis and is still recorded, separately, in `outcome.degraded`.

### The stranded-result heal ships with the alarm, not without it

D-145 built `unmatched_result_ids` as an assertion and deliberately did not wire it into the read
repair, on the argument that healing silently would mask a regression in `droppable_rows` — the one
primitive every deleter of conversation rows goes through — rather than surface it. The cost it left
standing is that any session split by the old age-based retention was unusable forever, recoverable
only by editing the database.

The argument is answered rather than overruled. The heal is counted
(`chemclaw_history_stranded_results_total`, whose HELP text says to alert on any non-zero rate) and
logged at WARNING naming the session. Every producer is now guarded, so that counter should sit at
zero forever; a non-zero rate is a louder signal than the silence it replaces.

One `strip_orphans` over both halves rather than a mirrored pair, because it is one rule: a
`function_call` and its `function_result` are an indivisible unit, and whichever half survives alone
is equally fatal to the thread. The argument against a windowed read gets *stronger*, not weaker:
healing both directions is safe precisely because the read sees the whole session, so "no partner
anywhere" is a fact rather than an artifact of where the window fell.

### `preconditions` is a list, because one job needs two rules

VIBE-1(a) asks for the atom/charge balance check to run before launch, so the actionable message —
"reaction is not atom-balanced (reactants minus products): C +2, H +4, O +2" — reaches the model
through `surface_domain_errors` in the same turn instead of staying in the worker log behind
`WorkflowFailureError: Workflow execution failed`, and so that Temporal stops retrying a refusal
five times.

The two equation-carrying jobs already declared `require_supported_solvents`. One slot forces a
combining function per *combination* — a cross-product of rules that each want to be stated once —
so `JobSpec.precondition: str | None` became `JobSpec.preconditions: list[str]`, run in declaration
order with the first refusal winning. The manifest is the natural place for the composition because
it is where the job is declared. The per-item pattern moved from `Field(pattern=...)` to a validator,
since a list field cannot carry one, and `_REFERENCE` is now one compiled pattern shared with
`params_model` so the two cannot come to accept different names.

`check_balance` **moved** to a leaf, `science/calc/balance.py`, rather than being copied:
`reaction.py` imports `xtb_engine` and therefore `tblite`, and a precondition is resolved by
importing it in the chat service's process (D-118). `reaction.py` imports it back, so the launch
check and the workflow enforce one definition. RDKit is core's own dependency, so the leaf may use
it; the SMILES parse is local for the same reason `science/safety/screen.py` keeps its own.

Wiring it immediately found an unbalanced equation in this repository's own `compare_solvents` test
fixture — `CC(=O)O → CC(=O)[O-]`, a proton short — which had been the test data for a solvent rule
nobody had reason to look past.

## Consequences

- A disconnected client, a cancelled turn and a raising consumer all release the agent's stream now,
  deterministically, instead of at the next garbage collection.
- A failed, cancelled, timed-out or terminated job appears in the resume with its own status word.
- An approval raised during a resume reaches the chemist.
- `cancel_job` cancels a coroutine-shaped wait; the `to_thread` remainder is registered, not hidden.
- The live harness can report a silent death on a deployment with no broker — which is every offline
  run, and was the state the signal was in when it was needed.
- A bricked session heals on the next read, and the heal is visible in the metrics.
- An unbalanced equation is refused in the turn that asked for it, and a job may declare as many
  independent launch rules as it has.
- Two upstream/runtime remainders added to `DEFERRED.md`, both with the trigger that closes them.

## Alternatives rejected

- **Reading `stream._iterator` without the defensive `getattr`.** The tests' fakes return bare async
  generators, and a hard attribute access would make the wrapper a second thing that can fail on the
  path that already failed.
- **Composing the terminal status word at the catch site in `job_results`.** The raise is identical
  for failed, cancelled, timed-out and terminated, so it would flatten all four and give the resume
  its own opinion about a run.
- **Cancelling `beating()`'s task unconditionally, without the `done()` guard.** Harmless for a
  finished future, and exactly the kind of "harmless" that stops being so the day the wrapped
  awaitable is a task someone else also holds.
- **Treating any unanswered turn as loud.** Would report a system that broke visibly when it did not,
  and the one signal worth having would be gone in the other direction.
- **Healing stranded results silently.** The thing D-145 argued against; the counter is what makes
  this a different proposal rather than a smaller version of the same one.
- **A combining precondition function per job.** A cross-product of rules that each want to exist
  once — which is how five near-identical `require_solvents_and_balance_and_…` functions get written.
- **A pydantic validator on `ReactionJobSpec` for balance.** It would run inside the Temporal
  workflow sandbox at deserialization, where RDKit is not importable, and re-run on every replay.
