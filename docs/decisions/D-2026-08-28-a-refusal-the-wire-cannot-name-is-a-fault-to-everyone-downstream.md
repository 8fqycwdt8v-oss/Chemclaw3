# D-2026-08-28-a-refusal-the-wire-cannot-name-is-a-fault-to-everyone-downstream — all five gates reach the surface, classified from the exception

## Status

Accepted. Completes what `ToolFailedEvent.reason` was added for (D-167's live-eval finding, carried
by `agent/plan_gate`) and supersedes the string-prefix classifier that shipped with it.

## Context

`agent/audit.refusal_reason` is the one table that separates "the system declined, on purpose, and
said so" from "something broke". Its own docstring states the reason it is one table: *"a new gate
cannot forget to count itself as long as it raises one of these."* It classifies **five**:

```
DryRunRefusal          -> dry_run
UndeclaredWriteRefusal -> undeclared_write
PlanNotApprovedError   -> plan_gate
RepeatedCallRefusal    -> repeat
AuthorizationError     -> authz        (a plain role denial; SkillsReadOnlyRefusal lands here too)
```

`ToolFailedEvent.reason` — the field a surface reads to decide whether a row is the control working
or a fault — was `Literal["plan_gate"] | None`.

**So four of the five gates left this process indistinguishable from an unreachable pod.** Measured
against `Chemclaw3_ui`, which is the consumer this field exists for: its trace renders
`reason === 'plan_gate'` in warn amber with a shield and the words "needs plan approval", and
*everything else* in danger red with "failed". A chemist who switched **Dry run** on — a control
they operated themselves, one line above the composer — read their own choice back as a broken
tool. A role denial looked like an outage, so the reader's next move was to retry or report a bug
rather than to ask an administrator for the role.

That is the exact failure the field was created to prevent, and the ADR that created it said so:
folding a refusal in with a database outage "reports a correctly-gated turn as a broken one".

**The classifier was also the wrong shape, and would have made widening worse.** `graph_stream`
recovered the one reason it had by testing

```python
detail.startswith(f"{PlanNotApprovedError.__name__}:")
```

against `signal.message` — a string `failure_detail` builds as `"<class>: <message>"` and truncates
to 300 characters. `plan_gate_failure_reason`'s docstring defended reading the detail line rather
than adding a field: *"a new field on `ToolFailureSignal` is a third repository's contract for a
fact this side can already derive, so it is not worth the coordination."* That held at one reason.
At five it buys five copies of a class name, living in a module that cannot see the classes,
checked against a truncated string — a second opinion about a question the audit trail had already
answered on the same call, with nothing keeping the two answers equal.

## Decision

**Classify where the exception is, carry the verdict, and name the vocabulary once.**

- `agent/tool_authz.announce_tool_failures` already catches the exception, so it calls
  `refusal_reason(exc)` there and passes the result to `record_tool_failure`. Only the *raising*
  path classifies: an MCP tool that returns its failure has no exception, and every gate here
  refuses by raising, so there is no gate to name.
- `ToolFailureSignal.reason` carries it, defaulted to `None` — additive, exactly as `call_id` is,
  because a signal shape is a contract two other repositories read.
- `RefusalReason` is defined **once**, in `core/turn_signals`. `agent/audit` types its table with
  it, `api/events` types the wire with it. `core` is the one layer both the agent and the API may
  import; the alternative was a `cast` at the boundary, which is a static-typing device standing in
  for the agreement this makes structural.
- `plan_gate_failure_reason` is deleted. `PLAN_GATE_REASON` stays, typed as a `RefusalReason`,
  because `evals/live.py` classifies on it and should read it from the gate it names.

## Consequences

- **The wire contract gains four members.** `tests/fixtures/turn_events_contract.json` is
  regenerated in this change — one line — and that is the tripwire working as designed:
  `Chemclaw3_ui` and `Chemclaw3_mock` mirror this field, and a member added here and not there is
  dropped in transit by the mirror's field-by-field normalizer. The UI half lands with this.
- **Widening is the safe direction for a consumer that ignores the field**, and unsafe for one that
  switched on it exhaustively — there were none: the only consumer compared against `plan_gate`.
- **A gate whose reason the wire cannot express now fails loudly**, in the change that adds it:
  `ToolFailureSignal.reason` is typed as the closed set rather than as `str`, so the failure is a
  type error at the gate rather than a silent `null` at a surface.
  `tests/test_api_observability.py::test_every_gate_the_audit_trail_classifies_can_be_said_on_the_wire`
  asserts `_refusal_types()` and `RefusalReason` are the same set in both directions, derived rather
  than transcribed.
- **The classification no longer depends on prose.** A test pins that a refusal whose message names
  no class at all still classifies, and that a `ConnectionError` whose message *does* say
  `"PlanNotApprovedError:"` does not — the two halves the string match got wrong in principle and
  could have got wrong in fact the day a message was reworded past 300 characters.

## What was rejected

- **Widening the `Literal` and leaving the string match.** It would need a prefix per class, in a
  module that cannot import them, against a truncated string — five chances to drift where the
  audit row already holds the answer.
- **Deriving the reason in the surface from the message.** The same defect one repository further
  out, and the thing `reason` exists to stop.
- **A `str` field on the signal.** It keeps the mirror additive at the cost of the property worth
  having: a gate whose reason nothing downstream can render should fail here, not report itself as
  a database outage.
