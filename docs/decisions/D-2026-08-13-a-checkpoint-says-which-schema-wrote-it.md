# D-2026-08-13-a-checkpoint-says-which-schema-wrote-it — Every checkpoint carries the state schema it was written under, and a foreign one is refused by name

**Status:** accepted · **Date:** 2026-08-13 · Extends `D-2026-08-10-langgraph-rebuild-of-the-conversation-layer` §3 (turn state is the checkpointer's).

## Context

Layer 1's turn state lives in a Postgres checkpointer (`agent/checkpointer.py`), and `thread_id` is
the session id — so a checkpoint outlives the *build* that wrote it by design. That is the whole
point of it being durable, and it is also the failure mode nobody had looked at.

LangGraph restores a checkpoint's `channel_values` into channels built from the **current** graph's
state schema, and it ships no migration system. A channel the checkpoint never held simply stays
empty, so the first node that reads it raises a bare `KeyError` naming a field. Measured on this
tree: a thread checkpointed under a state declaring `plan`, resumed under one declaring `todos`,
fails with exactly

```
KeyError: 'todos'
```

from inside the node, with nothing in it naming the thread, the schema change, or a remedy. A
redeploy that moves a state field therefore strands **every session that already has turn state**,
and the only evidence a chemist or an operator gets is a key name.

This is not hypothetical for this repository. `ChemclawState` has moved twice in the last four
weeks: the LangGraph rebuild replaced the hand-built plan field with `TodoListMiddleware`'s todo
list (D-2026-08-10), and `6dd1f90` re-declared `model_calls`/`loop_capped` as `UntrackedValue`
channels. Both are exactly the shape that produces the `KeyError` above, and both shipped green,
because **nothing in a unit test has a checkpoint from the previous build.**

## Decision

**1. `SchemaStampedSaver` — a checkpoint records the state schema it was written under.**
`AsyncPostgresSaver`'s `metadata` is a plain `jsonb` column the saver round-trips untouched, so the
stamp needs no migration and no table of its own, and it travels *with* the checkpoint — which is
the only thing that makes the check possible on a thread whose writer was a different build. Two
overrides, on `aput` and `aget_tuple`, because those are the only two points where the schema is
knowable and where it matters. `alist` is deliberately unguarded: history reads render checkpoints,
they do not restore them into a running graph, and a schema change is not a reason to stop showing
what a session did.

**2. The version is *derived*, not declared.** `STATE_SCHEMA_VERSION` is twelve hex characters of
sha256 over the sorted channel **names** `get_type_hints(ChemclawState, include_extras=True)`
reports, inherited fields included. A hand-maintained constant would be correct exactly as often as
somebody remembered to bump it, and the deploy where it was forgotten is the deploy that needs it —
the change looks like an ordinary field rename and every test passes. Deriving it also covers the
case no constant ever would have: a dependency bump that reshapes the upstream `AgentState` /
`PlanningState` this state extends moves the fingerprint with it.

**3. Names only, and the residual is stated rather than papered over.** A name is what a node
indexes state by, so a name appearing, disappearing or moving is precisely what becomes a
`KeyError`. A same-name *type* change is not caught, because a type repr is not stable enough to
hang a session's resumability on. Middleware that contributes its own channels is outside the
fingerprint as well — `create_agent` merges those in, and this module cannot see them without
importing the agent builder that imports it. So the stamp is a strong signal, not a proof of
compatibility: it catches the schema this repository declares, and it never fires falsely on one it
does not.

**4. Refuse, do not silently start over.** A mismatch raises `CheckpointSchemaMismatch` — its own
type, so a caller can tell "this session predates a schema change" from "the database is down",
which a `KeyError` on a field name does not support — carrying the thread, both versions, and the
remedy. This is the same call `agent/plan_state.py` already makes for an unreadable plan and for the
same reason: a turn that resumes with the conversation silently dropped answers *normally* —
confidently, out of context, with no sign anything is missing — and a confidently wrong answer about
a process is worse here than no answer.

**5. An *unstamped* checkpoint is accepted.** Every checkpoint written before this guard has no
stamp, and refusing those would brick every live session at the deploy that introduces the guard —
the exact outcome the guard exists to prevent, caused by the guard. They resume as they always did,
and the first write of each thread stamps it from then on.

## What was rejected

- **A hand-bumped version field in `core/config`.** The failure is invisible at the moment it is
  introduced (see decision 3 above); a knob that must be remembered is a knob that will not be.
- **Starting the thread fresh on a mismatch.** Cheapest to implement and the most dangerous
  behaviour available: the two outcomes are indistinguishable to a chemist and not at all
  indistinguishable in what they authorize.
- **Encoding the version in `checkpoint_ns`.** It would partition threads by schema without any
  error at all, so a redeploy would silently orphan every session's history — a rename of the
  failure, not a fix, and it makes `durable/retention.py`'s thread-keyed prune (which follows
  `parent_checkpoint_id` chains) reason about a namespace it does not model.
- **Migrating old checkpoints forward.** It requires knowing, per schema pair, what the new channel
  should hold — which is the migration system LangGraph deliberately does not have, and which this
  repository would then own for every future state change.

## Consequences

- **Nothing is destroyed by a refusal.** The checkpoint rows stay until `durable/retention.py`
  prunes them, and the transcript (`session_messages`) and the audit chain are separate stores the
  checkpointer never held (D-2026-08-10 §3). What the chemist gets is `api/runner.py`'s ordinary
  turn-failure event — classified `internal` and **non-retryable**, which is exactly right, because
  retrying cannot move a checkpoint to a different schema — while the log carries this module's own
  ERROR naming the session and both versions.
- **The durable saver only.** `process_checkpointer`'s in-memory fallback is unstamped and needs no
  guard: it dies with the process that declared the schema, so no foreign checkpoint can reach it.
- **The operational contract on a state change becomes explicit.** Moving a channel name is now a
  deploy that ends in-flight turns by design, loudly. A deployment that wants to avoid that drains
  sessions first; before this, it had no way to know it should.
- `tests/test_checkpointer_schema.py` first *measures* the bare `KeyError` with no guard and no
  Postgres involved, so the thing being fixed is on the record rather than described; then proves
  the fingerprint moves on a renamed channel and on an added one and **does not** move on a
  reordered declaration (a fingerprint that never moves refuses nothing; one that moves on its own
  refuses everything), that a foreign stamp is refused by name, that a thread written under this
  schema still resumes, and that a checkpoint stripped of its stamp — a row a pre-guard build wrote
  — resumes rather than being refused.
