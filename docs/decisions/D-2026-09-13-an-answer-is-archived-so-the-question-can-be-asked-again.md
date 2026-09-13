# D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again — the attribution moves, the reopen is allowed, and the refusal is deleted

Migration 079 closed a real defect: `pending_store._OPEN`'s reopen NULLs `answered_at`,
`answered_by` and `answer`, so a re-ask of an already-answered question destroyed somebody's
attribution and their payload — in a table `retention._NOT_PRUNED` keeps precisely because "the row
is the only record there is". It fixed it by scoping the reopen to the terminal states in which
nobody answered, and `durable/awaiting.py` raised a **non-retryable** `ApplicationError` when the
upsert wrote nothing.

Refusing was the right direction. It was not a right outcome.

## What it cost

`request_id_for` keys on `(kind, subject, asked_of)` alone, and `request_external_input` sets
`WorkflowIDReusePolicy.ALLOW_DUPLICATE`, both on purpose: re-asking a *standing* question is an
ordinary act in this system. The monthly stability pull. The next campaign round's measurement of the
same arm. A re-launched approval of the same irreversible effect. Every one of those mints the same
`request_id`, meets an `answered` row, and — since 079 — fails the activity non-retryably, which
fails the workflow that asked (`ConnectorJobWorkflow._approve_effect` turns a failed approval into a
refused job).

So the question could not be asked again for as long as the old answer stood, and for this table
that is for ever: nothing prunes it, by a decision with its own reason.

The refusal's own error message told the asker to "vary the subject to ask a new question" — which
is advice to make the *same* question look different in order to get past a guard, in a field
(`subject`) that a person reads in their inbox.

## The decision

**The answer moves somewhere nothing can overwrite it, and the reopen is allowed.**

`pending_request_answers` (migration 096) is keyed `(request_id, run_id)`, because a *run* is what a
cycle is: the run that answered owns the archived row, and the run that re-asks owns the live one.
`_ARCHIVE_ANSWER` runs before `_OPEN`, in the same transaction, so the attribution is either moved
aside or the reopen does not happen — there is no ordering in which an answer is blanked. `'answered'`
joins the reopen's terminal states.

Three properties make it the right shape:

- **A retry by the owning run archives nothing and reopens nothing.** `run_id <> %s` is what
  separates an at-least-once redelivery of the opening activity from a new cycle; without it the
  archive becomes a log of redeliveries rather than of cycles, and the row the workflow has already
  settled is disturbed. Driven as its own arm.
- **Insert-only, by grant.** `infra/sql/grants/app_privileges.sql` gives the application INSERT and
  neither UPDATE nor DELETE on the archive. It holds an answer that was moved aside precisely so a
  reopen could not blank it; a credential able to edit one would undo the move.
- **Refused by retention and retained through erasure, inheriting `pending_requests`' argument rather
  than getting a new one.** These rows *are* that table's attribution, one hop later. The growth bound
  is the same and tighter — one row per *answered* cycle that was later re-asked, human-paced twice
  over. `agent/leaver.py` scrubs `requested_by` and `answered_by` exactly as it does there, and
  `asked_of` stays for the same reason it stays there (advisory routing, possibly an entitlement
  rather than a person).

## What was deleted, and why that is the consistent move

With every terminal state reopenable by a different run, `open_request`'s verdict has **no reachable
input that is False**. Driven over all five shapes the upsert admits — a first ask, a retry by the
owning run against an answered row, a re-ask by a different run, a re-ask after `expired`, and a
caller with no `run_id` — it was `True` in every one.

So `_CLAIMED_BY`, `open_request`'s return value, and `open_pending_request_activity`'s non-retryable
raise are **gone**. A guard whose condition is provably false reads as a control and is not one —
which is the `reject_widening` shape this repository deleted rather than keep alive with a test that
calls it directly. The invariant is not lost, because an invariant is not a function:
`tests/test_pending_store.py` drives all five shapes and asserts what each does to the row *and* to
the archive, so a future narrowing of that `WHERE` clause — rewritten three times now, in 076, 079
and 096 — goes red on the behaviour rather than on a verdict nobody reads.

## What was not done

**No `(request_id, run_id)` primary key on `pending_requests` itself.** That is the better model — an
append-only log of asks, with no second table and the history in one place — and it is a migration of
a live table plus every reader keyed on `request_id` (`_CLAIMED_BY`, `_SETTLE`, the inbox listing,
`_may_answer`, the answer route), each of which would have to mean "the row for the newest run". The
archive is additive and reversible; that is not.

**The audit trail was considered as the attribution record and declined.**
`default_audit_sink` returns `NullAuditSink` unless `session_store == "postgres"`, so relying on it
would make an answer's attribution conditional on a deployment's session-store setting — a control
conditional on how somebody started the process, which is the thing `SECURITY.md`'s own rule about
`assert` objects to one level down. `pending_requests` is durable unconditionally.

**The pre-existing conflation in `open_request`'s `run_id=""` default is untouched and recorded
here.** Two different no-run callers are indistinguishable to the upsert, so a second one meeting a
terminal row neither reopens it nor learns that it did not. That was true before this change and is
true after it; the only caller that passes a real `run_id` is the activity, and it is the only one
whose behaviour this decision is about.

## What keeps it true

| property | test |
| --- | --- |
| all five shapes of the reopen: first ask, retry by the owning run (row untouched, archive empty), re-ask after `answered` (row `waiting`, answer archived whole), re-ask after `expired` (nothing archived), and a no-run caller | `tests/test_pending_store.py::test_a_re_ask_of_an_answered_question_opens_and_the_answer_is_archived` |
| a re-ask through the **activity** returns the deadline it asked for instead of raising, and the previous cycle's answer is in the archive | `tests/test_awaiting.py::test_a_re_ask_of_an_answered_question_opens_through_the_activity` |
| a retry of the opening activity still does not disturb a settled row | `tests/test_pending_store.py::test_a_retry_of_the_opening_activity_does_not_disturb_a_settled_row` |
| asking again after a lapsed deadline still reopens | `tests/test_pending_store.py::test_asking_again_after_a_deadline_lapsed_reopens_the_row` |
| every table in the schema has a disposal decision, and no table is both pruned and refused | `tests/test_retention.py::test_every_table_in_the_schema_has_a_disposal_decision`, `::test_no_table_is_both_pruned_and_refused` |
| the erasure sweep names every attributable retained table | `tests/test_leaver.py` |

The archive is read in those tests by a direct query rather than through a store function, because
there is no reader for it in `src/` and deliberately none: nothing in the system consults an archived
answer, it exists so the record is not destroyed. A helper written only for the test would be the
reader, and the test would then be asserting its own code.

Three mutations, each restored from a `.bak`:

| mutation | result |
| --- | --- |
| `'answered'` comes back out of the reopen's terminal states (the pre-fix behaviour) | red, both tests |
| `_ARCHIVE_ANSWER` is not executed | red, both tests |
| the archive stops excluding the owning run (`run_id <> (%s::text \|\| '-never')`), so a retry archives its own answer | red on the named arm — *"a retry archived its own answer, which makes the archive a log of redeliveries rather than of cycles"* |

Two earlier attempts at that third mutation were invalid SQL — an unterminated quote and an untyped
placeholder — and both reddened thirteen tests for the wrong reason. Recorded because a mutation that
breaks the fixture rather than the subject proves nothing, and the thirteen red lines look exactly
like success.
