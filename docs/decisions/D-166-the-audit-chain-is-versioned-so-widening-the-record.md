# D-166 — The audit chain is versioned, so widening the record does not invalidate the record

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-034 (durable trail), F10-G1 (hash chain)

## Context

A durable job can now be traced to the question that prompted it. D-157 gave `ConnectorJobWorkflow`
a `job_records` row carrying the run's `rationale`, its `session_id` and its `correlation_id`, so
"why was this campaign started?" is answerable for anything that goes through the connector seam.

An ordinary tool call cannot. `api/runner.py` mints a `correlation_id` per turn and stamps it on
`audit_events`, on a job's Temporal memo, and on the connector request header — and on nothing that
holds the user's words. `audit_events` had no `session_id`; `session_messages` had no
`correlation_id`; no table mapped one to the other. So for `gather_evidence`, `predict_pka`,
`suggest_next_experiment`, `propose_knowledge_note` — the great majority of the trail — the GxP
record could prove *that* a tool ran, with which arguments and by whom, and could never reach the
conversation that caused it.

Two columns close that. The interesting part is what they would have broken.

## The problem the columns create

`audit_store.chain_hash` computes `stable_hash({"prev": prev_hash, "event": event.model_dump()})`.
`model_dump()` is the *whole* model. So adding a field to `AuditEvent` changes the bytes hashed for
**every row already in the table**, whose stored `row_hash` was computed over the narrower shape.

The first deployment to run the migration would therefore fail `make audit-verify` across its
entire history — not because anything was altered, but because the definition of the hash moved
underneath it.

That is a worse failure than it first looks. The chain exists to answer one question: *was this
trail altered after the fact?* A chain that reports "broken" after a schema change makes that
question unanswerable, because "someone tampered with it" and "we added a column" produce the
identical symptom. An auditor cannot distinguish them, and neither can the operator. The control
would still be running and would have stopped meaning anything — the same failure mode as
`audit.py`'s own history, where the durable sink was constructed at one call site, the deployed
service passed nothing, and the compliance record was empty in production while every document
called it the trail.

## Decision

**Each row records which field set its `row_hash` covers.** `infra/sql/024_audit_provenance.sql`
adds `chain_version SMALLINT NOT NULL DEFAULT 1`; `chain_hash` takes `version` and the verifier
passes each row's stored value rather than the current one.

- **v1** is everything written before this migration: the eight original fields.
- **v2** adds `session_id` and `purpose`.

Reconstructing v1 by *selecting* those eight keys from `model_dump()` is exact rather than
approximate, and that is load-bearing: `stable_hash` canonicalizes with `sort_keys=True`, so the
subset serializes byte-identically to what the narrower model produced. A test asserts this against
an independent reimplementation of the old hash rather than assuming it.

`DEFAULT 1` is the right default for the same reason `prev_hash`/`row_hash` defaulted to `''` in
`011`: rows that predate the migration are exactly the rows the default describes, and no backfill
can be more truthful than that.

**`session_id` is stamped ambiently**, from `agent/session_context`, with the same precedence
`actor` and `correlation_id` already use — a tool has no request context, and an agent is cached per
profile for the process's life, so anything bound at build time would be shared by every user on the
pod. Empty off the request path, where there genuinely is no session.

**`session_messages.correlation_id`** is written per turn, so the two halves of "what happened in
this conversation" — the words and the tool calls — finally share a key.

## What is deliberately not done

**`purpose` lands as a column and nothing fills it.** The column is here because schema churn on a
hash-chained table is worth doing once and this is that once. Populating it honestly is a separate
decision: making the model author a reason per call means changing every tool signature, and
deriving one from the harness's active todo step is a *heuristic*. A provenance field that is
sometimes an inference is worse than an empty one — a reader cannot tell which rows are which, and
the field would quietly become unreliable exactly where it matters. D-157's `rationale` works
because a job launch is a discrete, deliberate act with an obvious author; an inline tool call is
not, and pretending otherwise would produce plausible text rather than provenance.

**Nothing is backfilled.** Rows written before the migration genuinely have no correlation id.
Inventing one would make an unanswerable question look answered, which is the failure this ADR is
about.

## Consequences

A trail spanning the migration verifies end to end, and that is pinned by tests that build v1 rows
from an independent reimplementation of the old hash — not through `chain_hash` itself.

That independence was learned rather than designed. The first version of those tests built v1 rows
*with* the function under test, so both sides moved together: deleting the version switch left the
tests green while the mechanism did nothing. Removing the switch now fails four tests instead of
one. This is the second time in this work that a test proved self-consistent rather than pinned —
the plan-approval test in D-164 had the same shape — and both were caught by deliberately breaking
the code rather than by the suite passing.

The remaining gap in this area is `purpose`, and the larger one is that the reasoning a
`correlation_id` now reaches is itself erodible: `session_store._compact` rewrites rows,
`durable/retention.py` prunes by age, and `rollback_to` deletes a turn's rows on disconnect. The
join is necessary and not sufficient, and `BACKLOG.md` says so.
