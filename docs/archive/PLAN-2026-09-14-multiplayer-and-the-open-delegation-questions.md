# 2026-09-14 — Scoping shared-session multiplayer, and what would settle the three delegation questions

A dated record, not a living plan. It scopes one new wave and states what each still-open
multi-agent question would take to close. What it *asks for* lives in `docs/planning/BACKLOG.md`;
this is the reasoning behind those rows.

Written because a reader asked whether ChemClaw would support four patterns after the planned waves
— orchestrator/worker, debate/critique, role-specialised routing, and several humans sharing one
agent session. Three of the four are things this system **had and removed**, which makes "will it"
the wrong question: the right one is what evidence would justify adding them back.

---

## Wave 8 — several humans in one session

**Not a setting, and not the owner gate.** The instinct is that multiplayer is single-player with
the ownership check relaxed. Four measurements say otherwise, and the third and fourth are the
expensive ones.

### What is measured today

1. **A transcript row cannot say who wrote it.** `infra/sql/008_sessions.sql` declares
   `session_messages (id, session_id, message, created_at)` — no actor column, no index on one. A
   shared thread whose messages are anonymous is not a shared thread; it is one voice with several
   authors. This is a schema change *and* a backfill question, because every existing row predates
   the concept.
2. **Ownership is checked in 46 places** across `src/chemclaw/api/`. Each becomes a membership
   question rather than an equality, and membership has a shape ownership does not: who may add a
   participant, who may remove one, and whether leaving a session revokes access to what was said
   while you were in it.
3. **The turn stream has exactly one reader, by construction.** `api/detach.py` holds one
   `asyncio.Queue` and one `_attached` flag; `_attached_or_discard` drops events when nobody is
   reading. A second reader would *steal* items from the first rather than see a copy of them. So
   "everyone in the session watches the turn" is a fan-out this class does not have — and it cannot
   be bolted on without deciding what a slow reader costs the others, which is the bound
   `service_sse_send_timeout_seconds` exists for in the single-reader case.
4. **Two writers on one thread fork the DAG silently.** Measured in Wave 2: no error, last writer
   wins, 26 checkpoint rows with duplicate step numbers under different parents, and one pod's
   answer returned to its caller and absent from the session. `SessionTurnClaims` is what prevents
   this today, and it prevents it by *refusing* — a 409.

### The shape the work takes

**The serialisation is already right and its response is wrong.** One turn at a time per session is
the correct semantic for a shared thread: B's message should wait for A's turn rather than
interleave with it. What changes is the answer — today a second concurrent turn is refused, and in
a shared session it should **queue**. That is a smaller change than it sounds and a much better
one than relaxing the claim, which is the thing the fork measurement forbids.

Five pieces, in dependency order:

1. **Participants.** A `session_participants` table, and the 46 gates read membership. The owner
   stays, as the participant who may add and remove.
2. **Attribution.** An actor on every `session_messages` row, and on the event contract, so a
   surface can render "Ana asked" and "Ben asked". Without this, 3 and 4 are not worth building.
3. **A queued turn.** The claim's 409 becomes a wait with a bound, and the waiting message gets a
   position. The lease already handles the holder dying.
4. **Fan-out.** `DetachableTurn` gains N readers with per-reader backpressure, so one stalled
   browser cannot hold the turn or starve another participant.
5. **Whose authority.** The open policy questions, which are *not* engineering: whose roles govern
   a tool call (the sender's, almost certainly), whether B may approve a plan A's message
   produced, and whose `/memories/` load — they are namespaced per actor digest today, so a shared
   session either loads none, loads the sender's, or needs a session-scoped tier that does not
   exist.

**Piece 5 is the one to settle before 1.** It is cheap to defer and expensive to retrofit: an
approval model chosen after the schema is a migration, and "B approved A's plan" is an
authorization claim, not a UI detail.

### What it is not

Not Slack. A connector that puts ChemClaw in a chat room is a different piece of work on top of
this one, and it would want pieces 1–4 finished first. Scoping them together is how the hard half
gets skipped.

---

## The three delegation questions, and what would close each

### Orchestrator/worker — open, and Wave 3 is the instrument

One subagent runs on every turn today, compiled through the whole chain. Wave 3 adds an N-name
roster and depth 2, **gated** on an outcome measurement. The gate is the lesson: the specialist team
that `D-2026-08-15` deleted was added to be ready, shipped off, and stayed off in every
configuration.

What would close it: `evals/delegation.py`'s three-arm comparison, run against a real gateway on the
reading-heavy corpus Wave 0 wrote for it. **A negative result closes the question as legitimately as
a positive one** — that is stated so nobody re-opens it on the grounds that the answer was
disappointing.

What would *not* close it: another delegation-rate measurement. `D-2026-08-12` measured 2 of 15 and
`D-2026-08-13` measured 14/15 against 14/15 with the old arm already at ceiling. Rate is a mediator;
two of those fifteen probes span two specialists, so the accuracy figure had an unpassable floor
before any model was involved.

### Debate/critique — declined, and the restart condition is narrow

`D-2026-08-16-a-second-judge-is-a-second-answer-about-the-same-answer` declined `RubricMiddleware`
on two grounds: it cannot reuse `score_answer`, and a failed grading returns the *ungraded* answer —
so the control is absent exactly when it fails. Neither has changed.

What this system uses instead is **deterministic** verification, and in a domain whose ground truth
is checkable that is the stronger choice: `protocols/checks.py` runs atom balance, charge
consistency, limiting reagent, control presence, hazard screen and plate fit server-side; the report
harness verifies claims against retrieved evidence, discards ungrounded ones and marks the gaps; the
knowledge graph carries contradiction as a relation.

The restart condition, stated so it is testable rather than a mood: **a critic that refuses rather
than grades**, or one that *proposes a deterministic check the system then runs*. Both keep the
property a grader loses — that a failure of the critic is visible rather than silent. Wave 3's
advisor is deliberately neither: it answers a question the agent asked and the agent stays the
author.

### Role-specialised routing — the profiles exist, the router does not

Seven profiles ship and they genuinely narrow: `evidence` reaches **zero** side-effecting tools,
`safety` reaches one, `default` reaches all 49. What was deleted is automatic routing between them.

What would close it: a measurement that compares *answers* on a corpus where the right profile is
not inferable from the question's surface form — which is the case routing is for, and the case the
retired corpus did not contain. Until such a corpus exists, a router is a guess with a metric
attached.

---

## What this record does not do

It does not schedule Wave 8 against the existing waves — multiplayer competes with capability, and
that is a product call rather than an engineering one. It does not reopen the challenge panel. And
it does not promise that Wave 3's measurement will come out in favour of a roster; the plan is
written so that it need not.
