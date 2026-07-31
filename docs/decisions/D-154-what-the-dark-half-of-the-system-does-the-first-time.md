# D-154 — What the dark half of the system does the first time it runs

**Status:** accepted · **Date:** 2026-07-31 · **Supersedes:** nothing

## Context

Four prior live passes each covered a slice: the nine-stage e2e (D-109), a 50-user load test against
a **stub** model (D-119), fifty expert questions (D-138), and an agentic-system review (D-136/D-137).
All four ran with roughly thirty settings at their shipped defaults — off, or pointed at an in-memory
backend. `harness_enabled`, `verifier_enabled`, `budget_enabled`, `mid_turn_resume_enabled`,
`retrieval_mode=graph`, `note_reindex_enabled`, `audit_verify_enabled`, `retention_enabled`,
`digest_enabled`, `calibration_enabled`, `eval_drift_enabled`, `artifact_evict_idle_days=0`,
`data_sources` without `vector`/`lexical`/`eln-ord`/`vendored`.

So the code behind them had been written, type-checked, unit-tested against mocks, and shipped —
and had **never executed**. That is the largest untested surface in the system, and it is invisible
to every gate: with the defaults, you exercise the narrowest slice while every surface returns 200.

This pass brought the whole stack up natively (Postgres 16 + pgvector 0.8.1, Temporal 1.4.1, six
connector bundles, four Temporal workers, the front door under `entra_required` with one signed
identity per probe, the real `xtb` 6.6.1 binary, a dedicated knowledge checkout for the PR-gate)
with **every one of those flags on**, and drove it with real Anthropic traffic. Three code reviews
ran in parallel over disjoint areas, each with a stated method rather than a survey.

## Decision

Fix the defects that make a shipped-but-never-run feature actively worse than an absent one — the
ones where the failure is *silent*, or where the broken state looks healthier than the working one —
and close each defect's *class* at the gate that should have caught it, not only its instance.

### What was wrong, and why nothing saw it

**Every step template was unreachable from a conversation.** `templates/registry.py` did
`cast(BaseModel, params).model_dump(...)`; `cast` is a static no-op and MAF hands the tool body the
decoded JSON object, so every run raised `AttributeError: 'dict' object has no attribute
'model_dump'`. This is D-138's defect exactly, in the sibling module: that fix was applied where the
bug was observed rather than everywhere the assumption lived, and the template seam — the fourth of
the four documented ways to add a capability — kept it. `tests/test_templates.py` checked the
generated tool's name, docstring and schema and never called it; `make template-validate` validates
declarations, not invocation.

**The reference connector's flagship job could not start.** `connectors/bo/connector.yaml` declared
`precondition: …:require_rounds_within_ceiling`, which takes an `int`, while `connectors/jobs.py`
calls `precondition(spec)` with the validated `CampaignSpec`. Every `start_optimization_campaign`
raised `TypeError: '>' not supported between instances of 'CampaignSpec' and 'int'` before any
durable work. `resolve_precondition` types it `Callable[[Any], None]`, so mypy is blind; the
validator built the tool without invoking it; the only tests call the rule directly with a bare int.

**The embedding cache evicted the batch it was about to return.** The FIFO trim ran *before* the
result was read back out, deleting oldest-first from the whole dict including keys the current call
had just inserted. `retrieval/vector_index.py` embeds one text per note in a single batch, so the
note-index rebuild — which hybrid retrieval depends on — raised a bare `KeyError` naming nothing for
any corpus above `embedding_cache_size` (2048). `tests/test_embedding_cache.py` only ever embeds one
text per call, the one shape that cannot hit it.

**A broken verifier reported maximum confidence.** With the judge enabled and its endpoint
unreachable, `verify_answer` degraded to the deterministic citation gate, which for an ordinary chat
answer has no `[[wikilinks]]` to check and returned `confidence=1.0`. `review_required` is
`confidence < 0.7`, so every answer in the deployment came back maximally confident and unflagged
while nothing had been verified. A permanently broken judge produced a *better-looking* signal than
a working one.

**Retention deleted undelivered push-back events.** The module docstring justifies pruning
`session_events` with "a **consumed** push-back mailbox row is spent"; the SQL was a bare age sweep
with no `consumed_at` predicate. A durable job outliving the window — a QM or HPC run, exactly what
this channel exists for — lost its completion, the session waited forever, and the harness "awaiting
job" todo never flipped. It also destroyed the `system-audit-integrity` and `system-eval-drift`
rows, which by construction are never consumed: retention deleted the tamper evidence.

**A detected audit-chain tampering completed successfully and showed a green schedule.**
`audit_verify.py` notifies `system-audit-integrity`; both consumers of the mailbox scope their claim
to `kinds=("job_completed",)`, so the row is not merely unread but never eligible. `eval_drift.py`
hit the same problem and mitigated it with an explicit `logger.warning`; `audit_verify.py` copied the
notify pattern and not the mitigation, and had no logger at all — producing precisely the "silently
un-delivered alert reads as an all-clear" outcome its own docstring says it must never produce.

**The digest advanced its watermark after a failed delivery.** `notify_session_best_effort` swallows
the failure and the acknowledgement ran unconditionally, so a swallowed send was indistinguishable
from a successful one and the matches it covered could never be re-reported — against the ordering
the workflow's docstring says exists so that "a crash between 'found matches' and 'delivered' must
cause a re-report, not a silent skip".

**Artifact eviction was registered, served, and started by nothing.** `ArtifactEvictionWorkflow` is
decorated, imported by the background worker and advertised on its queue, with no schedule, no route
and no caller. An operator who sets `artifact_store_max_bytes` and `artifact_evict_idle_days` — the
documented way to turn eviction on — got nothing. This is the failure `durable/registry.py` exists to
prevent, one level up.

### Closing the classes, not the instances

- `make connector-validate` now checks that a declared `precondition` can accept the params object
  the launcher hands it — binding the signature catches arity, comparing the annotation catches
  shape. Confirmed to fail on the old wiring and pass on the new.
- The generated template launcher is now exercised through `agent_framework.tool(...).invoke()`, the
  framework's own dispatcher, so the test cannot encode our idea of what MAF passes.
- `notify_session_best_effort` now returns whether it delivered. Most callers rightly ignore it; a
  caller that advances a watermark past what it just tried to send must not.
- Both eviction bounds now earn the schedule, so the two settings that document the feature turn it
  on rather than turning on nothing.

## Consequences

The offline gate is green at **2061 passed, 26 skipped, 87.2% coverage**. The 26 skips are honest and
named: 19 Temporal tests (the test server binary cannot be fetched from this sandbox) and 7 CREST
tests (the binary is not packaged for this distribution). Those are the only two subsystems whose
sole verification is CI.

A meta-finding, now visible three times over: **the test supplied the thing the system was supposed
to supply.** D-138 recorded it for the connector-job launcher; it recurred verbatim for the template
launcher, for the BO precondition (tests pass an int, the launcher passes a spec) and for the
embedding cache (tests embed one text, production embeds the corpus). The pattern is not carelessness
— it is what happens when a test constructs its own inputs instead of driving the real entry point.
The durable countermeasure is the one applied here: drive the framework's own dispatcher, and let the
validator compare the two declarations rather than trusting either.

One confirmed finding is **deliberately not fixed here**, because closing it means changing a
decision this repository made on purpose. See `docs/planning/BACKLOG.md` (DARK-1): the harness
plan-approval gate authorizes a *session*, not a *plan*. Reproduced live — approve a four-item plan,
then ask a completely different question in the same session, and it executes autonomously with
`approved=false` for the new plan, running a calculation and a knowledge-graph write. The fix
requires deciding what the plan hash identifies (today it includes completion state, so it changes on
the first ticked box, which is why execution can not be bound to it) and whether a deployment with no
approval store fails open or closed. That is an ADR with GxP consequences, not a patch.
