# D-2026-07-31-one-gate-over-one-side-effecting-set — One gate over one side-effecting set

**Status:** accepted · **Date:** 2026-07-31 · **Extends:** D-167 (an approval authorizes a
request), F10-C (per-tool authorization)

## Context

`dry_run: true` is documented as "show me what you would do without doing it" — a natural primitive
for a deployment whose shipped autonomy is `plan_only`, and one whose whole value is that the model
can neither set it nor clear it (it is a per-turn contextvar, not a tool argument).

It was checked in three places: `agent/durable_tools.py`'s report launcher, the generated job
launchers in `connectors/jobs.py`, and the template launcher in `templates/registry.py`. Each
checked for itself, returned a tailored `dry_run_notice`, and covered exactly its own tool.

Everything else ran. On a `dry_run: true` turn the agent still called `propose_knowledge_note`,
which pushes a branch to the knowledge repository; still called `record_confirmed_answer`, which
does the same; and still wrote to `user_preferences` and `subscriptions`. Three tools remembering
is not a control. It is three tools that happened to.

The backlog had already named the shape of the fix — "one gate over one `side_effecting` set rather
than three ad-hoc checks" — and by the time it was written the set existed: D-167 built
`plan_gate.gated_tools()` as `STATE_CHANGING_TOOLS ∪ every enabled connector's declared
state-changing tools and jobs ∪ every enabled template launcher`.

## Decision

**One middleware, over the set D-167 already assembled.** `refuse_writes_on_dry_run` attaches at
the tool-invocation boundary beside `enforce_plan_approval` and refuses anything in the
side-effecting set while the turn is a dry run. The three ad-hoc checks are deleted, and
`dry_run_notice` with them.

Three choices inside that.

**The set moves to `agent/authz.py` as `side_effecting_tools()`.** It read correctly as
`plan_gate.gated_tools` while the plan gate was its only caller, and stopped reading correctly the
moment dry-run needed the same answer: dry-run applies whether or not the harness is on, so the
definitive list of "tools that change things" cannot be owned by a harness module. It belongs where
the classification it extends already lives, beside `STATE_CHANGING_TOOLS` and the partition test
that holds that classification to the tool registry.

**It refuses by raising, not by returning.** `DryRunRefusal` subclasses `AuthorizationError` for
the reason `PlanNotApprovedError` does: the two behaviours already built around that class are
exactly the two wanted — the audit middleware records the refusal as an `error` outcome, and
`surface_authorization_denials` hands the model the message verbatim instead of MAF's opaque
"Function failed." A subclass rather than the base so a caller can still tell "you lack a role"
apart from "you asked me not to actually do this".

**The tailored wording is traded away deliberately.** The old notices named the job and echoed its
validated payload; the gate's message names the tool and says nothing ran. That is a real loss, and
it buys a guarantee: a write added next year is covered on the day it is classified, not on the day
someone remembers to add a fourth check. Echoing arguments from the middleware was considered and
rejected — the refusal would then carry an arbitrarily large validated payload into the model's
context on a turn that deliberately does nothing.

## Consequences

Every write is covered, including the two that push git branches. The gate is unconditional
(`is_dry_run()` is False off the request path, so it is a no-op on every ordinary turn), which
removes the "is it attached here?" question the three-check arrangement invited.

**Where dry-run still does not reach, and why that is not a hole.** A template *step* invokes tools
inside a Temporal activity rather than through the agent's middleware — but `is_dry_run()` is a
per-turn contextvar set by the front door, so it is False in a worker process by construction.
There is no turn to rehearse there. Similarly, `connectors/identity.py` keeps sending the
`X-Chemclaw-Dry-Run` header, which the connector correctly does not trust: it is advisory context
for the connector's own logs, not a control, and it stays that way.

**One test had to change shape, and the change is the point.** `test_dialogue.py` and
`test_connector_jobs.py` called the launchers *directly* and asserted a "DRY RUN" return value.
Those calls bypass the middleware, so under this decision they launch — which is correct: a direct
call is not a turn. The tests now drive the gate, and the connector-job case asserts the invariant
that actually matters, that **every** declared job's name is in the set, rather than that one
hand-built spec checked a flag.
