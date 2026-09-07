# D-2026-09-07-a-residue-nobody-can-name-is-a-residue-nobody-can-finish — an interrupted erasure is finished by session id, on the application's own privileges

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
D-2026-09-06-an-erasure-that-races-a-live-turn-is-not-an-erasure (which opened this and ends "It
stays open"),
D-2026-08-08-the-conversation-is-erasable-the-record-is-not (the two tiers),
D-2026-08-28-an-erasure-that-cannot-name-what-it-missed ·
**Corrects** `ErasureReport.residue_total`'s and the CLI's claim that an operator with **owner
rights** has to remove the residue.

## Context

`erase_actor` takes the durable turn claim on every session it is about to erase and counts what it
could not hold. A non-zero `residue` means a turn committed rows after the sweep, under a session
whose `session_owners` row is already gone — and every actor-scoped route in this system reaches a
session *through* that row. The previous ADR left the remedy as a sentence: "an operator with owner
rights has to remove those rows by session id."

That sentence was never run.

## What was measured

A session is seeded, erased with `--apply`, and a turn then commits two messages under the same id:

```
first erase:        {session_messages: 1, session_turns: 1, session_owners: 1, ...}
residue:            {session_messages: 2} under ['sess-…']
second erase_actor: every table 0        <-- reaches nothing
```

Two things fall out of that run.

**The report names no session.** `residue` is `{table: count}`, and by the time it is computed
`session_owners` no longer holds a query that could recover the ids. So the remedy the report
printed — "remove these rows by session id" — named something the operator had no way to obtain.
That is the defect this ADR's title is about: an unnameable residue is an unfinishable one, whatever
privileges anyone holds.

**And the rows were never beyond the application's reach.**
`session_store._session_delete_statements()` — the set `SessionOwnerStore.delete_session` already
executes as the runtime role — is keyed on `session_id` and **reads `session_owners` in none of its
statements**. What the actor route cannot do is *find* the sessions. So "owner rights" was a
misdiagnosis: the missing piece was the identifiers and an entry point, not a privilege.

## Decision

**`ErasureReport.residue_sessions` carries the ids, and `leaver.finish_erasure(sessions, apply=)`
deletes by id.** The CLI grows `--finish <session-id> ...`, and the actor run that reports a residue
prints that exact command with the ids filled in — the remedy is one line below the failure that
names it, rather than in a document an operator has to be told about separately.

**It refuses any session that still has an ownership row, by name.** That is the whole safety
property: this route can only finish what is *already* orphaned. Without it, `--finish <any id>`
would be an unscoped conversation delete that skips the ownership check every other path in this
system makes — a larger hole than the one being closed.

**It takes the same durable turn claims and refuses the same way**, because the residue exists
precisely because a turn was writing; finishing under a live turn would produce a second residue
under the same ids. It runs the deletes in **one transaction** across every named session, for
`erase_actor`'s reason and one more: a finish interrupted half way is a second partial erasure under
ids whose owner rows are already gone, and there is no third place to go. The **dry run is real** —
the DELETEs run and roll back.

**What it deliberately leaves behind is printed on every run.** `tool_result_links` is denied DELETE
to the runtime role on purpose (`app_privileges.sql`: a link may only disappear behind the
content-addressed blob it points at), so it is named with its reason and excluded from `remaining` —
a table this route does not touch has to be visible in the report, and one reported as *remaining*
would read as unfinished for ever. `durable/retention.py`'s age sweep collects it with its blob.

**Auditable means the report proves the state, not that a row is written to `audit_events`.** The
trail is a record of *tool invocations* (`AuditEvent`'s own definition), and stamping a CLI sweep
into it under a fabricated correlation id and actor would be the overload D-040 exists to prevent.
What makes this auditable is what the run can show: it re-counts every session it touched after it
commits, `finished` is that count being empty, the exit code is `2` when it is not, and the retained
tier — `audit_events`, `turn_costs`, `plan_approvals` — is untouched by a route derived from the
*conversational* delete set, so what the person did stays attributable after their residue is gone.

**The one-turn divergence is handled by not depending on it.** A turn cancelled between the graph run
and the transcript write leaves `checkpoints: 8, session_messages: 0` (the previous ADR's
measurement), so a residue is not reliably both records of a conversation. `finish_erasure` runs
every statement in the derived set regardless of which half is present, and
`test_a_residue_that_is_only_graph_state_is_finished_too` is the graph-only arm — the case where a
finish that needed a transcript row would report "nothing to do" while the conversation stayed
recoverable from the checkpointer.

## Consequences

- `agent/leaver.py`: `residue_sessions`, `ResidueReport`, `finish_erasure`,
  `_delete_orphaned_sessions`, `finish_leaves`. `_residue_for` now returns the sessions alongside
  the counts — grouped by the session column, because the ids are half the answer.
- `cli/erase_actor.py`: `--finish`, its report, and the rule that exactly one of `actor` and
  `--finish` is given. Exit `2` still means "it wrote and did not finish", and now has a command
  behind it.
- `docs/guides/runbook.md`'s offboarding section says what a residue is and how to finish it,
  naming no table (`tests/test_leaver.py` enforces that).
- Four tests, the first watched failing with `_delete_orphaned_sessions` filtering its targets
  through `session_owners` — which is what every session-scoped route in this system did before this
  one: `removed_total == 0` and the residue still standing.
- **What is still open**, and it is the other half of the previous ADR's last paragraph:
  `chemclaw_transcript_thread_divergence_total` names one branch of the divergence and not the class
  — the gateway-failure path and a cancellation mid-tool are uncounted, so a flat series is not
  proof of agreement. That belongs to `api/runner.py` and is untouched here.
