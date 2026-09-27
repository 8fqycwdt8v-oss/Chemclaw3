# D-2026-09-27-an-author-is-a-person-and-an-agent — one authorship model for notes, the audit trail and transcripts

**Status:** accepted · **Date:** 2026-09-27 · **Decided by** the owner (2026-09-27, final) ·
**Closes** the `BACKLOG.md` row *"Three subsystems want one missing column: who wrote this"*
(issue #474) · **Builds on** `D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor`
(invariant 3) and `D-2026-09-06-the-one-agent-that-exists-is-named-in-the-trail` · **Is the
prerequisite for** the multi-human-session row, and the sender-governs decision the owner took on
2026-09-26 for it builds on this model.

## Context

Three subsystems record something somebody wrote, and each had noticed in its own corner that it
could not fully say who:

- `kg/note.py`'s `Note.created_by` is `Literal["human", "agent"]` — **a note names no person**.
- `audit_events` carries `actor` (the person) and `agent` (the helper or peer that made the call,
  `''` for the agent the chemist talks to). It had the pair already.
- `session_messages` had **no actor column at all**, so a transcript could not say whose words a
  message was — harmless with one owner per session, and the first thing a shared session needs.

Answered three times, in three corners, the question gets three subtly different answers — a
nullable here, a sentinel word there, a person column that means "the owner" in one table and "the
sender" in the next.

## Decision

**An author is a pair: the person it was written for, and the agent that wrote it.** One value
object, `core/authorship.Authorship(actor, agent)`, and every subsystem stores it in the same two
names:

| | `actor` | `agent` |
|---|---|---|
| meaning | the human principal on whose behalf it was written (the Entra `oid` of the turn or job) | the agent that wrote it |
| absent | `None` — **not recorded**, never "nobody", never guessed | `None` — **a human wrote it directly** |
| other values | — | a profile name (`<caller>-helper`, a peer's name); `""` = an agent wrote it, unnamed |

**`""` is the unnamed agent because it is what the audit trail already meant by it.** Adopting
`audit_events`' encoding rather than inventing one is what lets that table join with no migration
and no backfill; a sentinel word such as `"agent"` would have been a second spelling of the same
statement, and a profile could one day be called that.

Per subsystem:

- **Audit trail — no schema change.** `actor`/`agent` are the pair; every row is a tool call, so
  `agent` is never "no agent", which is why it stays `NOT NULL DEFAULT ''` there. `AuditEvent.agent`
  now defaults to `UNNAMED_AGENT`, and a test pins the two to one value.
- **Session messages — migration 109.** `actor TEXT` and `agent TEXT`, both nullable (a default is
  an assertion about every existing row — `tasks/lessons.md` rule 89). `save_messages` writes the
  turn's actor on every message and `agent = NULL` for a `HumanMessage`, `""` for everything else;
  the durable and in-memory providers stamp the pair on read; `GET /sessions/{id}/messages` returns
  it as `TranscriptMessage.author` — the column's reader; a fork copies it rather than naming the
  forker.
- **Notes — an optional `actor:` frontmatter field.** `record_note`, the one write path, stamps the
  ambient actor (`get_current_actor`, bound by every driver) on the subject note and refuses one
  naming a *different* person, for the reason it refuses `created_by: human`. `created_by` stays as
  the agent half, and `Note.authorship` reads the two together. A note carries no agent *name*:
  the graph that wrote it is a build-time argument of the audit middleware and reaches no tool body,
  and `D-2026-08-26` forbids rebuilding an ambient carrier for it — so an agent note's agent is
  `UNNAMED_AGENT`, stated rather than invented.

### Backfill

- **Notes: read-time, no rewrite.** An old `created_by: agent` note reads as
  `Authorship(actor=None, agent="")` — the agent marker, person unrecorded — and a `created_by: human`
  note as `(None, None)`. `actor` is omitted from the rendered frontmatter while `None`, so every
  existing note re-renders to the same bytes and the writer's "nothing staged, nothing to commit"
  rule holds. The knowledge repository is not this system's to rewrite.
- **Session messages: in the migration, from facts the database holds.** `actor` = the session's
  owner (every stored session has had exactly one person in it, so every message was written on that
  person's behalf); an orphaned or unattributed session stays `NULL`. `agent` from the row's own
  speaker label in either stored shape: human/user → `NULL`, anything else → `""` — the unreadable
  label included, because the transcript already renders such a row as the agent's.
- **Audit: none needed.**

Measured, on this repository's Postgres image in a laptop Docker VM, one million rows: the backfill
statement took **49.4 s**, the same rewrite as `UPDATE … FROM` **42.3 s**, and `SET actor = NULL,
agent = NULL` — which computes nothing — **41.0 s**. The row rewrite is the cost, so a large
transcript table needs `migrateJob.activeDeadlineSeconds` raised for this release (said in
`values.yaml` and in 109's header).

### Erasure

- `session_messages` is erased **by session and by author** (`… OR actor = ANY(%(actors)s)`). The
  second arm finds nothing new today — measured at 0.88 s of sequential scan over a million rows —
  and is what reaches a leaver's words in a session somebody else owns once sessions can be shared.
- A note's `actor:` is in the knowledge repository, not the database: it is named in the erasure
  report's out-of-reach tier (`leaver._BEYOND_REACH`) with how to find it, and retained on the audit
  trail's line — the note is the record, and removing a name from it is a history rewrite that is
  the repository owner's decision.
- `audit_events.actor` stays in the retained tier, unchanged.

## What builds on this

The multi-human-session row and the **sender-governs** decision (owner, 2026-09-26): a message's
`actor` is its sender, so whose roles govern a tool call, whose memories load and who may approve a
plan all have a column to read. Two things this deliberately does not do, because they are that
row's: the owner gate is unchanged, and the erasure's second arm is not covered by the turn claims
the sweep takes, which only span the leaver's own sessions.

## Alternatives weighed

- **`author_actor`/`author_agent` columns.** Rejected: the audit trail already spells the pair
  `actor`/`agent`, and `tests/test_leaver.py`'s person-column vocabulary matches `actor` exactly —
  a new spelling would have been invisible to the completeness check that exists to catch it.
- **A nested `author: {actor, agent}` frontmatter object replacing `created_by`.** Rejected: every
  existing note would re-render differently (a spurious commit per note), and the prompt and every
  evidence reader already read `created_by`. The flat `actor:` beside it changes no existing byte.
- **Deriving session-message authorship at read time instead of backfilling.** Rejected: `NULL/NULL`
  would then mean both "pre-109" and "a human with no identity bound", and the erasure predicate
  could not find a pre-109 row by author.

## What was measured rather than assumed

- The backfill and erase-statement timings above (`EXPLAIN (ANALYZE, BUFFERS)` on the erase).
- `tests/test_authorship.py` drives every entry point: `record_note` under a bound identity, with
  none and with a mismatched note; `save_messages` → Postgres → `get_messages` → `_transcript`; the
  in-memory provider; migration 109's own statements over pre-109 rows in both shapes, an orphan and
  an unattributed session; `fork_session`; and `erase_actor` over a two-person session.
