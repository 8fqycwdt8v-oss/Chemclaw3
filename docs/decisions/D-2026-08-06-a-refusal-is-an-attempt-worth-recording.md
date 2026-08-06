# D-2026-08-06-a-refusal-is-an-attempt-worth-recording — A refusal is an attempt worth recording

**Status:** accepted · **Date:** 2026-08-06

## Context

**AUDIT-2**, from the 50-user load run: a tool call rejected for bad arguments is neither audited nor
authorization-checked. `agent_framework._tools._auto_invoke_function` composes the parse error and
returns it **before** the function-middleware pipeline, so *"the model asked for `find_notes` with
arguments it could not satisfy"* left no row in `audit_events` at all.

The row was found because the load run's own numbers were wrong for the same reason: 100 "tool calls"
that all failed argument validation, and therefore produced no audit rows to cross-check against
(LOAD-1). The gap is what made the miscount invisible.

Two things the row does not say, both established by reading the dispatcher rather than the row:

- **There is a second early return, and it is the more interesting one.** A tool *name* that is in no
  map returns even earlier than the argument check. "The agent tried to call something that does not
  exist" is a fact about the model's behaviour — and about a prompt injection that half-worked — and
  it was equally invisible.
- **Authorization not running there is correct, not a gap.** Nothing executed. There is no action to
  authorize, and adding a gate would only produce a second way to refuse an already-refused call. The
  row bundles the two; only the audit half is a defect.

## Decision

A wrapper on `_auto_invoke_function`, installed by `build_agent` beside the audit middleware it
completes, records one `rejected` event for any call that reached no middleware.

### The discriminator is "did anything audit this", not a message match

MAF's refusal messages are recognisable (`"Error: Argument parsing failed."`) and matching them was
the obvious implementation. It breaks silently the day upstream rewords one or adds a third early
return — and it cannot tell MAF's composed error from an exception a *tool body* raised, which the
middleware already audits correctly.

So the middleware sets a `ContextVar` at entry and the wrapper reads it after awaiting dispatch. That
asks the question that actually matters, so a new upstream refusal path is covered the day it appears
rather than the day someone notices. It also makes the wrapper self-deleting in the right way: if MAF
ever runs its middleware for refused calls, the marker says so and this becomes dead code instead of
a silent double-record.

Both halves of the `ContextVar` behaviour were measured before the design rested on them: a value set
inside an awaited coroutine **is** visible to its caller (the `await` runs in the caller's context),
and `asyncio.gather` copies the context per task, so two parallel tool calls cannot see each other's
marker. The second is asserted as a test rather than left as reasoning — without it, one successful
call would suppress the audit of a refused one running beside it.

### `rejected` is its own outcome

Not `error`. Nothing executed, so there is no failure to report; what happened is that the *attempt*
was malformed. A trail that cannot tell the two apart cannot answer "what did the agent try" without
also implying it ran. `latency_ms` is `0.0` for the same reason — the honest number, distinguishable
from a fast tool by the outcome beside it.

The **raw** arguments are recorded, because they are the only interesting thing about a rejected
call: the validated form does not exist, which is what "rejected" means. Reading them can itself
fail — that is the definition of this path — so the reader is defensive and falls back to a truncated
verbatim blob. An exception raised *inside* the audit of a refusal would replace a recorded attempt
with a crash, in the one place that must add no failures.

### The wrapper binds nothing at install time, and that is a correction

The first version bound sink, actor and correlation id at install, mirroring the middleware. The
patch is **process-global and permanent** while the middleware is per-agent — so the first
`build_agent` in the process would have owned every rejection recorded by every later agent. Benign
in production, where there is one sink; not benign as a design, and it was the tests running beside
another agent's build that showed it. All three are resolved per call instead: the turn's ambient
identity, and the one configured sink.

The patch is idempotent for the same reason. `build_agent` runs once per profile per process, and a
second patch over the first would wrap our own wrapper and record every refusal twice — invisibly,
because both rows would be correct.

### Rejected alternative: inspect the turn afterwards

The runner sees the turn's messages and could spot a `function_result` carrying an exception. It
cannot tell one MAF composed from one a tool raised, and the second is already audited. Double-
recording every failed tool call to catch the refused ones is a worse trail than the gap.

## Consequences

- A call naming an unknown tool, or carrying arguments its schema refuses, now leaves exactly one row
  naming the tool, the actor, and what was sent — and logs at WARNING, which is where an injection
  that half-works becomes visible.
- A successful call and a raising tool are still recorded once each, by the middleware, with their
  real latency.
- One more vendored-internal dependency (`agent_framework._tools._auto_invoke_function`), scoped to
  one function and reversible by the returned undo. It ends with being deleted, like every other
  upstream workaround in `DEFERRED.md`.
- `tests/test_rejected_call_audit.py` drives MAF's real dispatcher rather than a stand-in, because
  the finding *is* about MAF's control flow: a test over our own wrapper would prove the wrapper
  works and say nothing about where upstream returns.

**Adjacent, in the same package:** the `PostgresAuditSink.record` mutation survivor —
`statement_timeout_seconds=...` could be replaced with nothing and every test still passed. Closed
as a **sweep over the source** rather than a test for that one store, because the defect is not that
the audit sink forgot: it is that any store can forget, silently, and only a live hung query says
so. The AST guard in `tests/test_db.py` found 29 bounded call sites with the same shape and the same
absence of a test, plus four that must run unbounded (migrations and grants, where an index build
may legitimately run long; two operator-run measurement scripts). Those four are an allowlist with
its reason written beside each entry, and a third test fails when an entry stops being needed — an
allowlist that outlives its reason is how a guard rots into a rubber stamp. Postgres tests skip
offline, so the source is the one place this property is always checkable.

## Alternatives rejected

- **String-matching MAF's refusal messages.** Breaks on a reword, and conflates a refused call with a
  tool that raised.
- **Adding an authorization check on the refused path.** Nothing executed; there is no action to
  authorize.
- **Auditing from the runner, after the turn.** Cannot distinguish MAF's composed error from a tool's
  own, so it either misses refusals or doubles every failure.
- **Patching at import time.** Cheaper to write and dishonest about what it does: the one place a
  vendored patch belongs is beside the middleware whose blind spot it covers.
