# D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed — a failure that says nothing is read as "proceed"

**Status:** accepted · **Date:** 2026-08-04

## Context

The first live pass with every layer up at once — real broker, real workers, real Postgres, real
front door, real model (`docs/archive/live-full-stack-2026-08-04.md`) — found the same defect three
times in three different places, and the repository had already met it twice before under other
names:

| when | where | what the caller saw | what it did |
| --- | --- | --- | --- |
| 2026-08-02 | unreachable broker → `request_development_report` | `Error: Function failed.` | model wrote the whole report by hand and presented it as PR-gated |
| 2026-08-02 | task queue with no worker | `Error: Function failed.` | retry storm |
| 2026-08-04 | job failed *after* its turn | **nothing at all** | "job started" stood forever |
| 2026-08-04 | job failed *inside* its turn | `Error: Function failed.` | model had no idea the screen had failed |
| 2026-08-04 | turn produced no prose | **nothing at all** | empty answer, no error |

The concrete case: a chemist asks for a three-solvent screen and says "2-MeTHF" — one of the most
common process solvents there is. The model passes it faithfully. The ALPB database does not know
that name, the durable job fails ~30 s later, and the turn has already said *the job is running*.
`ConnectorJobWorkflow` awaited its child with no failure path, so `notify_session_best_effort` was
never reached and `job_completed` had no counterpart. The reason existed only in Temporal's history,
under an id the chemist would have had to keep.

Each instance was previously treated as its own bug. Reading them together, they are one:

> **An outcome that says nothing is not a neutral outcome. A model reads silence as permission to
> continue, and a person reads it as success.**

This is why the count is five and not one. Fixing the instance leaves the class.

## Decision

**Every path by which work can fail must deliver a sentence, and the sentence must be the one
written for the reader.** Concretely, in the three places the live pass found:

1. **A durable job that fails after its turn** pushes a `job_failed` session event before the
   failure propagates — carrying the reason — and the front door claims it, types it
   (`JobFailedEvent`), and renders it in the warning lane rather than the grey trace. Propagating
   stays correct; propagating *silently* does not. A failure also releases the harness todo waiting
   on that job, which would otherwise block a plan on an outcome that has already happened.

2. **A durable job that fails inside its turn's inline wait** is re-raised as a `ConnectorJobError`
   — a `ChemclawError`, therefore surfaced verbatim by `surface_domain_errors` — instead of a bare
   `WorkflowFailureError` that MAF renders generically.

3. **A turn that produces no prose at all** yields `ErrorEvent(code="empty_answer", retryable=True)`
   naming the tool-call count. The existing guard covered only the harness loop cap; the live case
   had the harness off.

### The sentence is the *application's*, not the deepest one

`failure_reason` skips Temporal's structural frames and takes the **first application-level**
message. This is stated as a rule because the obvious implementation is wrong and was shipped
wrong for an hour: walking to the innermost cause returned

> `TBLiteRuntimeError: String value for epsilon was not found among database of solvents`

while the frame directly above it was

> `unknown ALPB solvent '2-methyltetrahydrofuran'; common valid names are water, methanol, …`

Both are true. Only one names the value the chemist typed and the ones that would work. **Depth is
not specificity — the deepest frame belongs to whoever is furthest from the user.**

### And the corollary: a check must say what it checked

Two measurements in this pass were reassuring and vacuous, and both were in code written to *find*
this class of problem:

- The smoke's `audit chain verifies` was passing over **zero** rows, because the audit sink records
  agent tool calls and a job launched from a script makes none. It now reports the count.
- `probes needing a job that started none` flagged a probe whose job had run end to end, because a
  job answering inside `inline_wait_seconds` is deliberately never announced. A signal that flags a
  working path is worse than none: it spends the reader's attention on the thing that was fine.

"Verifies" and "verifies over something" are different claims. This is the same correction
D-2026-08-03 made to the fabrication metric, arriving from the opposite direction — there a signal
saw too little and cried wrong; here signals saw too little and said fine.

## Consequences

- `job_failed` is a new event kind on an existing channel. Additive: consumers that do not know it
  ignore it, and the SSE claim now covers both outcomes rather than destroying one.
- `empty_answer` is a new `ErrorCode` and `chemclaw_turn_empty_answers_total` a new declared
  counter, so the shape is a trend rather than an anecdote.
- Regression tests exist for all three paths, including one that runs against a real Temporal
  server (`tests/test_connector_job_workflow.py`) and therefore skips where the test-server binary
  cannot be fetched — verified live here instead, against the broker on 7233.
- **Not fixed, deliberately:** `compare_solvents` accepts a solvent name that only fails deep inside
  the durable job. `JobSpec.precondition` exists for exactly this and validating the name at the
  tool boundary would turn a 30-second durable failure into an immediate correction. Filed rather
  than done, because it is a chemistry-surface change and this ADR is about the reporting seam.
