# D-2026-08-29-a-review-with-fresh-context-is-a-different-instrument — auditing #280

**Status:** accepted · **Date:** 2026-08-29 · Seven independent reviewers over the merged
eight-findings change (#280), and the ~30 defects they found.

## Context

`#280` shipped eight infrastructure findings and was merged green: `make lint type test` passed
with Postgres and Temporal running, every declaration gate held, and the change carried its own
list of six defects that *building* had found. It was then read by seven reviewers, each with fresh
context, each scoped to one failure domain — authorization, the durable layer, SQL, units, the
operational read model, the outbound seams, and the tool-schema cache.

They found roughly thirty more. Every finding below was reproduced here before it was acted on;
several were reproduced twice, because two reviewers reached the same defect from different
directions and one of them ran it against a live Postgres.

## The finding that matters more than any individual defect

**Almost every one of them is a docstring that states the correct control beside code that does not
implement it.** Not a missing control — a *described* one. The list is uncomfortably consistent:

- `api/mcp_face.py` documented at length what its read-only surface exports. It exported **nothing**:
  the capability-tool registry is filled by import side effect and the face never imported the
  modules that fill it, so the pod logged "serving 0 read-only tool(s)" and answered `tools/list`
  with an empty array. Six tests passed because the test file imports the seeder itself.
- `api/routes/pending.py` says "'I asked the QA lead to approve this' must not mean 'and I may
  approve it myself'". The only producer of approval waits raised them with `asked_of` unset, which
  takes the "anyone authenticated" branch — so the requester could approve their own irreversible
  external change.
- `agent/evidence_tools.py` says "the route to another person's session is `CurrentSession`'s
  ownership check, not this tool". `CurrentSession` is a FastAPI dependency on `/sessions/{id}`
  routes and was not on that path at all, while `session_id` was a plain argument. The ids were
  discoverable from another advertised tool.
- `durable/digest.py` says the outbound delivery "runs *after* the acknowledging condition is
  already satisfied". It ran before it, and could fail non-retryably, so one misspelled channel name
  wedged the watermark.
- `deliver/message.py` says "the same filter runs here" about credential redaction. It did not;
  connector tokens reach `redact_secrets` only through an argument nothing passed.
- `deliver/registry.py` swallows a per-channel failure with "Logged by the caller with its own
  context". There was no such caller, no logger and no metric in the whole package.
- `operations/window.py` justified its 730-day clamp because those tables "are pruned by
  `durable/retention.py`". All five are in `_NOT_PRUNED`, explicitly *refused*.
- `core/units.py` says case separates molarity from length. It does, for `M`/`m` and `mM`/`mm` —
  the two pairs its test covers — and `nM` resolved to *nanometre* while `µm` resolved to
  *micromolar*.
- `effects.external_ref` is called "the only handle an operator can undo this by hand" in the ledger
  it belongs to and read by the evidence pack, and `SettleEffectInput` had no such field, so every row it would ever hold was empty.

This repository already has a name for the last shape:
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`. What #280 shows is that the
shape is not rare and is not caught by the thing that usually catches it. **A test written by the
author of the code inherits the author's belief about what the code does.** `tests/test_mcp_face.py`
imports the registry seeder, so it could not see an empty registry. `tests/test_operations.py` wrote
its injection marker into four columns the reading never selects and gave the one column that *is*
caller-influenceable a safe literal. `tests/test_units.py` checked the two prefixes the registry got
right. In each case the test proves the mechanism and not the instance, and the author cannot tell
the difference because both look like the thing they meant.

Fresh context is what separates them, and it is cheap: seven reviewers, each reading a diff they did
not write, against the repository's own rules.

## Decision

Fix all of them in one change, and record the two prose corrections rather than quietly editing
them away.

**Nothing here supersedes `D-2026-08-29-a-trail-nobody-can-read-answers-no-question` or any other
merged ADR** — those decisions stand and their code stands. What is retracted is two *claims*:

1. **"Five tables recorded this system's own work and none had a reader."** False for four of the
   five: `cli/explain.py`, `publish/backfill.py`, `durable/job_record_store.py`,
   `kg/proposal_store.py` and `agent/plan_approval_store.py` each read one. Only `turn_costs` had no
   reader, which is exactly what its own docstring said. The true and narrower claim — the one the
   package is actually for — is that every existing reader is a **point lookup** and none of them can
   read *across* the record. The module docstrings, the package README and this ADR carry the
   correction; the merged ADR is left alone, per the rule on merged ADRs.
2. **`MAX_WINDOW_DAYS`'s stated reason.** The clamp stays at 730 for the reason that is true — these
   tables only grow and every read is an unindexed-range aggregate under a statement timeout — rather
   than for a retention that does not happen.

## What changed, by severity

**Controls that were claimed and absent.** The registry seeding is one module both consumers import,
with a fresh-interpreter test (the only kind that can fail). Irreversible-effect approvals are routed
at the launch site, refused outright when unrouted, and `SECOND_PERSON_KINDS` refuses the requester
before routing is consulted. `assemble_evidence_pack` resolves ownership through `owner_permits`, the
one definition the HTTP routes now share. The face's deny-list is reframed on the predicate that is
actually load-bearing — *about this deployment's people, or about its chemistry* — which withheld
five more read-only tools. The face gained a Service, an optional Route and an ingress NetworkPolicy;
a pod no ingress policy selects is unrestricted for ingress.

**Corruption and wedges.** `effects._SETTLE` gained the guard `_BEGIN` already had, so a failure
after the change landed cannot rewrite an applied row to `failed`; `external_ref` is coalesced and
has a producer. `pending_requests` gained `run_id`, separating a retry of the opening activity from a
re-ask after a lapsed deadline — the case `ALLOW_DUPLICATE` exists for and the projection dropped.
Both unit ladders are complete and in step, `µM`/`µm` are exact, and a percent that states its basis
carries it and refuses across bases. Three config reads moved out of workflow bodies, where they
decided how many commands were emitted.

**Everything else** is in the diff and in `tasks/todo.md`: the evidence pack's silent truncation and
its missing job outcome, `authorship`'s dropped `superseded`, `job_activity` counting failures as
runs, the model's raw tool name reaching a reader, `Message.kind` as a path component,
`correlation_id` on the wire, the commitment cursor colliding with the ELN sync's, a CWD-relative
export path, `answered_at` on an expiry, and `report_measurement` stamping a unit nobody stated.

## What was found sound

The tool-schema cache added late in #280 under CI pressure — the one change most likely to be
wrong, since it shares objects across turns in a system that compiles per turn precisely to avoid
sharing. Verified against the installed `langchain_core`: the only instance mutation is a memo that
is a pure function of the schema, 16 concurrent invocations under different identities crossed
nothing, profile narrowing is unaffected, and the cache is bounded by the registry. Its stale
performance figures are corrected here and its documented-but-unchecked bound is now a test.

## Consequences

- Two migrations (`078`, `079`), a new setting (`effect_approval_role`), and one behaviour change a
  deployment must act on: **a job declaring an irreversible effect refuses to run until
  `CHEMCLAW_EFFECT_APPROVAL_ROLE` names an approver.** Nothing in this repository declares one, so
  nothing breaks today; the refusal is the point.
- `report_measurement` now requires a unit for a calibrated property. Its previous default silently
  asserted the ledger's unit, which is worse than the empty string it replaced.
- The lesson is in `tasks/lessons.md`: **a test written by the author of the code inherits the
  author's belief about what the code does**, so a review that shares that belief cannot find these.
