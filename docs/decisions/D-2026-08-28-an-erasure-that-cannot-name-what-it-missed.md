# D-2026-08-28-an-erasure-that-cannot-name-what-it-missed — three false greens closed, and the two registers that describe one argument are joined

**Status:** accepted · **Date:** 2026-08-28 · Extends `agent/leaver.py`'s two-tier rule and
`durable/retention.py`'s disposal register; reverses nothing.

## Context

Two registers bound the durable stores and they were written apart.

- `agent/leaver.py` answers *"someone left, now what?"* — `_ERASE` (the conversation, deleted) and
  `_RETAINED` (the record, counted and kept, because an attributable record that can be deleted on
  request is not an attributable record).
- `durable/retention.py` answers *"what bounds this table's growth?"* — `_PRUNABLE` (an age window)
  and `_NOT_PRUNED` (every other table, with its reason).

Both are exhaustive over the schema and each is checked. Neither was checked **against the other**,
and the erasure register's completeness test derives its work from *column names*. Four audits,
each verified against a live database with the real `erase_actor` run, found what those two facts
allow.

## What was found

**1. A departing person's unread digests survived their erasure, and the report said complete.** A
digest lands in the synthetic mailbox `digest-<oid>`, which by design has no `session_owners` row
(`durable/digest.digest_channel`). Every `session_events` row is reached through that table, so the
join could not match it. Their standing queries went in the same run, leaving copies of those
queries in a mailbox nothing would ever open, under `session_events: 0`.

**2. Erasing one person deleted another person's stored tool result.** `_ERASE` deleted every blob
any of the leaver's sessions linked, with no "unless another session links it" arm —
`session_store._SESSION_DELETE` has had exactly that arm since the single-session delete was
written. Measured: two sessions link one blob, erasing the first owner removed the blob, and
`ON DELETE CASCADE` took the *second* session's link row with it. A chemist who erased nobody found
their own transcript pointing at a result the surface could no longer fetch.

**3. A person's id and their own prose sat in a table in neither tier.**
`result_publications.document` is a `jsonb` column carrying `publications[].actor`, `.session_id`,
`.correlation_id` and a free-text `.rationale`. The completeness test derives from column names and
this column is called `document`, so the check that exists to catch precisely this could not see it
— the `tool_result_links` failure the module already records, one indirection further out.

**4. The completeness test's vocabulary is itself a hand-written list.**
`audit_anchors.reseal_by` names "who accepted the gap and why" and was not among its six spellings,
so a live person-column sat in neither tier with the test green.

**5. Three tables said "no decision is on record" while the decision was on record next door.**
`note_proposals`, `plan_approvals` and `turn_costs` are all in `_RETAINED`, kept through an erasure
request for exactly the reason the four "refused" entries in `_NOT_PRUNED` give. One argument,
applied to seven tables in one register and four in the other.

**6. `session_owners` cannot be disposed of without a window nothing points at.** An ownership row
goes only once no session-scoped table holds a row for that session, and `tool_result_links` empties
only behind its blob, on `CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS` — which defaults to 0 like every
other window. So a deployment stating a conversation policy and nothing else forgets **no session
that ever called a tool**, while the sweep logs a clean pass every night.

## Decision

**The erasure register gains two tiers, because two positions existed that it had no way to state.**

- `_RETAINED_IN_PAYLOAD` — the retained tier for a table whose person is inside a payload. One
  entry, `result_publications`, with a `jsonb` predicate. Retained rather than erased by the same
  line as everything in `_RETAINED`: a publication says who asked for a result and why. Split from
  `_RETAINED` rather than folded in, because the difference is exactly what hid it — that register
  holds column names, and a schema-derived test reads them as such.
- `_BEYOND_REACH` — tables this command can neither clear nor count, with the reason. One entry,
  `audit_anchors`: the runtime role holds no privilege on it (deliberately — its writer went with
  the audit hash chain) so a `SELECT count(*)` would fail the whole erasure, and the schema is
  forward-only, so an older build's rows may still name that operator. The CLI prints this tier
  unconditionally. **Naming what was not answered is the point**: withholding it is what turns a
  partial erasure into one that looks complete, which is the module's own founding argument.

**The two registers are joined by a test rather than merged into one.**
`test_a_table_the_erasure_keeps_is_not_disposed_of_on_a_clock` derives the rule *a retention clock
may not dispose of what a person asking to be forgotten does not* from `_RETAINED`, over every
table it governs instead of the four names that were typed out. Merging them further was considered
and rejected: the two keys are not the same shape — retention keys on a timestamp expression
evaluated in SQL, erasure on an actor identity in two spellings reached three different ways
(directly, through a join, and through a digest SQL cannot derive) — so one register expressing
both degenerates into per-table SQL, which is what already exists.

The payload tier is deliberately outside that rule, and the distinction is real: `result_publications`
is retained against an erasure *request* and still disposable on a *policy* once delivered, because
by then the record it receipts lives in a store this system does not own. "Not deletable on request"
and "not disposable on a clock" are different claims.

**Fixes 1, 2, 4 and 6 land as code.** The mailbox is matched by exact equality against the channel
`digest_channel` mints, per actor spelling. The blob deletion gains the arm its twin already had.
The vocabulary learns `reseal_by`. The ownership prune logs, once per pass and before the query,
which unset windows can only ever hold it back — because "nothing was disposable" and "nothing is
left" are the same zero.

## Consequences

- Three regression tests were shown red against the pre-fix statements and green after, rather than
  asserted to cover something.
- An orphaned `tool_result_links` row — what `delete_session` leaves when a blob is shared, and what
  the new arm now also leaves — is beyond erasure permanently, and that is accepted rather than
  fixed. It names a session id no ownership row resolves, so what it keeps alive is unattributable
  rather than somebody's, and the age sweep collects the link with its blob.
- `infra/sql/README.md`'s Disposal column no longer disagrees with the code on four rows, and its
  legend no longer delegates to a `BACKLOG.md` row that was closed and deleted.
- `STORE_TABLES` gains the derived proof `CHECKPOINT_TABLES` has had since it was written: a
  LangGraph minor adding a store table now turns a test red instead of landing a table that escapes
  both the erasure sweep and the disposal register with every test green.
