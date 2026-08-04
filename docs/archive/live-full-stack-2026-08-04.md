# Live full-stack pass — 2026-08-04

The first run of the whole system with **every layer live at once**: a real Temporal broker, real
connector workers, real Postgres/pgvector, the front door, and a real model. Every prior pass was
missing one of those — most consequentially the broker, which every probe file said so in its own
header and which `live-grounded-2026-08-03.md` recorded as "Temporal absent".

Four defects found, all four fixed and covered by regression tests. Two of them were in the
signal built to find them, which is recorded here rather than quietly corrected: a measurement
that has never been wrong has usually never been used.

## The stack

| piece | what ran | how it got there |
| --- | --- | --- |
| Postgres | 16.13 + pgvector **0.8.6**, 34 migrations, cluster at `/var/lib/postgresql/chemclaw-live` | `make live-infra` (native path; no Docker daemon) |
| Temporal | Server **1.31.2** via CLI 1.8.2 built from source, file-backed dev server on 7233 | same |
| Workers | `background-jobs`, `connector-calc`, `connector-bo`, `connector-qm`, probes on 9000-9003 | `make live-up` |
| Connectors | bo · calc · chem · molfp · rxnfp · safety, all healthy; qm unprobed | one uvicorn on 8810 |
| Front door | FastAPI + SSE on 127.0.0.1:8000, `/readyz` naming all six connectors | `make live-up` |
| Model | Anthropic, `claude-sonnet-5` (`agent_model` default) | `ANTHROPIC_API_KEY` |
| Config | `entra_required=false`, `session_store=postgres`, `connectors_required=true`; harness slice re-run with `harness_enabled=true, harness_autonomy=plan_only` | pinned by `infra/live/processes.sh` |

**Corpus caveat, stated first because it bounds every retrieval number below.** This repository
ships a **38-note** seed graph and two ELN exports. The archived passes ran against ~1,025 notes
pointed at the mock's ~10,000 HTE records. Retrieval-grounded verdicts here are therefore worse for
reasons that are about the corpus, not the code, and **the two sets of numbers must not be
compared**. What this pass measures is the machinery: does every layer work live, and does the
system tell the truth about what it did.

## What was populated, and how

| table | rows | filled by |
| --- | ---: | --- |
| `note_index` | 38 | `make reindex` |
| `molecule_fingerprints` | 7 | `ElnSyncWorkflow` on `background-jobs` |
| `reaction_fingerprints` | 2 | same |
| `calculation_results` | 109 | the durable calc jobs |
| `job_records` | 11 | `ConnectorJobWorkflow` |
| `audit_events` | 139 | the live turns |
| `session_messages` | 162 | `session_store=postgres` |
| `session_events` | 8 (3 `job_completed`, 5 `job_failed`) | job push-back |

Two things worth recording about getting there, because both are procedure a future operator needs:

- **The ELN sync refused both entries the first time**, correctly: `note_repo_dir` defaults to `"."`,
  and the PR-gate refuses the working checkout because every submission begins `git reset --hard` +
  `git clean -fd` (G4). The lane was starting workers whose *every* note submission could only be
  refused — which silently removes the entire knowledge-contribution half of a run, since the
  PR-gate is the one path job results, reports and playbooks all take. `bootstrap.sh` now clones a
  dedicated `knowledge-repo` and `processes.sh` points `CHEMCLAW_NOTE_REPO_DIR` at it.
- **That first run still advanced the cursor** past both entries, so the retry needed the documented
  re-ingest procedure (runbook §v: start the workflow with `since` before the entries' timestamps).
  That is by design — a rejection is deterministic bad data — and the procedure worked exactly as
  written. Both entries then ingested, creating real PR-gate branches
  (`note/reaction-eln-2026-001`, `-002`).

## Stage A — the durable smoke, 6/6

`make live-jobs`, on `calc-compute_reaction_energy-…` started by that run:

| check | observed |
| --- | --- |
| workflow reached COMPLETED | COMPLETED, started 16:52:19Z |
| calculation cached in Postgres | 76 `xtb*` rows |
| job recorded in Postgres | `calc/compute_reaction_energy`, with its rationale |
| duplicate launch rejoins | id matches; cache rows 109 → 109 (nothing recomputed) |
| wedged worker → pending job | id after 20 s, COMPLETED once resumed |
| audit chain verifies | intact, **over 139 audit events** |

That last parenthetical is a fix, not decoration — see F3.

## Stage B — the probe corpus, with the model

The `du-*` probes, run for the first time (`--no-judge`; mechanical signals only):

| signal | first run | after fixes |
| --- | ---: | ---: |
| answered at all | 3 / 4 | 3 / 4 |
| expected tool reached | 4 / 4 | 4 / 4 |
| failed silently | **1** | 1 (du-03; now emits `empty_answer`) |
| durable jobs started | 1 (`RUNNING`) | 1 |
| finished inside the turn (never announced) | *not measured* | 1 |
| probes needing a durable job that ran none | du-01, du-03 (**du-01 false**) | du-03 |
| turns that surfaced a failure | 0 | **1** |
| median turn | 128.5 s | 142.1 s |

Harness slice (`harness_enabled=true`, `plan_only`), du-01 + du-04: **2/2 answered, 2/2 expected
tool reached, 0 silent failures.** No LIVE-1/LIVE-8-shaped failure in the configuration the Helm
chart ships — the first time that configuration has met a live model since D-152.

## Findings

### F1 [High] A durable job that failed after its turn told nobody, ever

`compare_solvents` was launched for a three-solvent screen, the turn told the chemist it was
running, and the child failed ~30 s later on an unknown ALPB solvent name. **No event of any kind
was emitted.** `ConnectorJobWorkflow` awaited `execute_child_workflow` with no failure path, so
`notify_session_best_effort` was never reached; `job_completed` had no counterpart, and the failure
was recoverable only by polling `get_durable_job_status` with an id nobody had kept.

The chemist's input was not even wrong in the ordinary sense: they said "2-MeTHF", one of the most
common process solvents there is, and the model passed it faithfully. It is the ALPB database that
does not know that name.

**Fixed.** A `job_failed` event now exists end to end — the workflow pushes it before the failure
propagates, `JobFailedEvent` carries the reason, the SSE claim covers both outcomes, and the browser
renders it in the warning lane rather than the grey trace. A failed job also *releases* the harness
todo waiting on it, which would otherwise wait on an outcome that had already happened. Verified
live: `session_events` holds five `job_failed` rows carrying their reasons.

### F2 [High] A job that failed *inside* the turn reached the model as "Error: Function failed."

The same failure on the inline path. `_await_briefly` correctly lets a genuine failure raise rather
than degrading to a job id — but `WorkflowFailureError` is neither a `ChemclawError` nor a
`SubsystemUnavailableError`, so `surface_domain_errors` passed it through and MAF rendered its
generic string.

**This is the third appearance of one defect.** The first made a model fabricate an entire
development report when the broker was unreachable (2026-08-03); the second was the unconfirmable
launch, framed in `connectors/jobs.py`. The pattern does not vary: a failure that reaches a model
wordless is not read as "this failed", it is read as "proceed".

**Fixed.** The model now receives:

> the 'compare_solvents' job ran and failed: ValueError: unknown ALPB solvent
> '2-methyltetrahydrofuran'; common valid names are water, methanol, ethanol, acetonitrile,
> acetone, thf, dmso, toluene, chcl3, ch2cl2, hexane, ether, ethylacetate

— which it can act on, by retrying with `thf` or telling the chemist. Confirmed in the audit log on
the real agent path.

**And the fix was wrong once, measurably.** `failure_reason` first walked to the *innermost* cause
and returned `TBLiteRuntimeError: String value for epsilon was not found among database of
solvents` — true, and useless to whoever typed "2-MeTHF". The measured chain was
`WorkflowFailureError → ChildWorkflowError → ActivityError → "unknown ALPB solvent …" → the tblite
internals`: the sentence the product had deliberately written sat one frame above the bottom.
Depth is not specificity; the deepest frame belongs to whoever is furthest from the user.

### F3 [Med] A turn that wrote nothing said nothing about it

du-03 made 29 tool calls over 197 s (`find_past_jobs` ×8, `load_skill` ×6, `find_notes` ×5, …),
never reached `start_optimization_campaign`, and ended with an empty `AnswerEvent`. No error, no
tokens. A guard existed for the *harness* loop cap; this run had the harness off, so nothing
covered it — and `evals.live` scores exactly this shape as `failed_loudly=False` because it is the
worst outcome a turn can have: a user cannot retry what never said it went wrong.

**Fixed.** An empty turn now yields `ErrorEvent(code="empty_answer", retryable=True)` naming the
tool-call count, plus a declared counter so it is a trend rather than an anecdote.

Related, and *not* fixed: the smoke's `audit chain verifies` check had been passing over **zero**
rows, because the audit sink records agent tool calls and a job launched from a script makes none.
"Verifies" and "verifies over something" are different claims. The check now reports the count.

### F4 [Med] The durable signal called a working path a miss

`probes needing a job that started none` flagged **du-01**, which had in fact run
`compute_reaction_energy` end to end — workflow `calc-compute_reaction_energy-4cf212292f8f8e4e`,
COMPLETED, confirmed against the broker. A job answering inside `inline_wait_seconds` is
deliberately never announced (an already-finished run would never emit the matching
`job_completed`, so a surface would draw a row that stays "running" forever), so `jobs_started` is
legitimately empty for a job that worked.

A signal that flags a working path is worse than no signal: it spends the reader's attention on the
one thing that was fine. **Fixed** — reach is now asked of the *tool calls* against the declared job
names, with inline completions reported separately, and both directions are pinned by tests.

### F5 [Low] The lane's readiness budget was shorter than a cold start

On a reclaimed container the connectors process sat in uninterruptible disk sleep paging in ~1 GB
of libraries and took **2 m 29 s** to bind; the budget was 90 s, so the lane declared a healthy
process dead. Raised to 300 s, and `wait_for` now fails *immediately* when the pid is gone, so a
genuinely crashed process is still reported in a second rather than waited out.

## What this pass did not cover

- **Entra enforced.** Everything ran with `entra_required=false`. The documented approach (a local
  RSA keypair, self-served JWKS, minted tokens — `live-gates-2026-07.md`) was out of scope here.
- **The full 230-probe corpus.** Only the durable probes and the harness slice were run; the wide
  behavioural sweep against a 38-note graph would measure the corpus, not the system.
- **`connector-qm`.** Its job needs a cluster (`DEFERRED.md`), so the worker ran but was unprobed.
