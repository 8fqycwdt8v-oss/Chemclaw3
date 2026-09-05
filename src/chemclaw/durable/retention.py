"""Bounded growth for the durable stores (gap SCH-1).

Nothing in this system ever deleted anything: no `DELETE`, no TTL, no retention window anywhere in
the tree. `session_messages`, `session_events`, `audit_events`, `calculation_results`, `note_index`
and both fingerprint tables grew for the life of the deployment. That is not only a disk-cost
problem: a records story with no disposal story is incomplete, and "keep for N years, then dispose"
is a policy a deployment has to be able to state and act on.

**What this prunes, and what it deliberately refuses to.**

The bullets below are the *argued* cases — the ones where the decision was close, or where getting
it wrong destroys something. They are not the whole schema, and this docstring read as though they
were. Measured: it named **three** refusals against the **thirty-three** tables this sweep does not
prune, so thirty of them had no disposal decision recorded anywhere a reader or a test could reach
one — including `bo_campaigns` and `bo_suggestions`, which are append-only and grow on every
campaign ask. `_NOT_PRUNED` below closes that. Every table in the schema is in exactly one of it
and `_PRUNABLE`, and `tests/test_retention.py` fails the next migration that adds a table without
saying what bounds it — because a list whose whole discipline is being exhaustive has to be checked
to be exhaustive, not asserted to be.

- `session_events` — a consumed push-back mailbox row is spent; it exists to wake one stream once.
- `session_messages` — conversation history. Bounded by age, per the deployment's policy, **but an
  age cutoff alone cannot dispose of a conversation row** (D-145). A `tool_use` and the
  `tool_result` answering it are one indivisible unit: delete either half and the API rejects the
  whole thread on every subsequent turn. Rows of one turn are written together and so share a
  `created_at`, but a cutoff is an instant with no knowledge of turns, and a pair *can* straddle it
  — a call retried across a window boundary, a mid-turn-resume interleaving, a clock that moved.
  Worse, nothing repairs the damage afterwards. A read-time repair used to strip an orphaned
  *call*, which made half of this failure self-healing; it went with the MAF thread that needed it
  (D-2026-08-10 §2), so both directions are now permanent. So this table is pruned per session
  through `droppable_rows`, which refuses any row whose partner is not also expiring — the sweep
  has to be right the first time.

- `tool_result_blobs` — the full text of what a tool returned, kept so a surface can fetch it
  (`api/tool_results.py`, migration 042). This is the table that shows what the three refusals
  below actually turn on, because it is the one that holds no *record*: the answers are in
  `calculation_results` and `job_records`, and a trace blob is a view of a turn that already
  happened, so a swept one costs a chemist a rendering they can ask for again and never a
  recomputation. That is what makes a plain `created_at` cutoff sufficient here — no LRU, no cost
  ordering, because ordering evictions by value only pays when what is being ordered is expensive
  to regenerate, and nothing here is. `tool_result_links.content_hash` is `ON DELETE CASCADE`, so
  the link rows go with the blob and this sweep needs no orphan pass.

  Its window still defaults to 0 like the others, and that is a deliberate uniformity rather than
  a considered policy for this table: `retention_enabled` is off by default, so a number here would
  differ from 0 only for a deployment that switched retention on without stating this window —
  which is exactly the case `test_retention_is_off_until_a_policy_is_stated` refuses. The cost is
  that the highest-volume table in this set is unbounded until an operator says otherwise, and
  `infra/sql/README.md` says so rather than implying a bound that does not exist.

- `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` — the LangGraph turn state (D-2026-08-10
  §3). They belong on this list for the same reason everything above does and were missing for a
  reason worth stating: they are created by `AsyncPostgresSaver.setup()` rather than by a migration
  in `infra/sql`, so they appear in no schema review and in no inventory. Erasure already reached
  them per actor (`agent/leaver.py`); disposal did not, so a deployment that erased nobody kept
  every turn's state for its whole life.

  Pruned by **thread**, not by row. A checkpoint chains to the one before it through
  `parent_checkpoint_id`, so deleting the old rows inside a live thread would leave the survivors
  pointing at nothing; a thread expires whole, when its newest checkpoint does. All three tables go
  in one transaction, against the per-table rule below, because they are one thread's state split
  across three keys with no foreign key to enforce it — `_prune_checkpoints` says what committing
  them separately would cost.

  **No migration can add an index to them, and no migration can `ANALYZE` them either.**
  `infra/sql` is applied by a `pre-install` hook Job that completes before any app container starts,
  so on a fresh install these tables do not exist when it runs; a migration is recorded in
  `schema_migrations` on that first run and never re-executed, so a `CREATE INDEX` guarded on the
  table's existence would be a permanent no-op that reads like a control — the `map_to_hpc_identity`
  shape D-2026-08-15 deleted. Measured, the index nobody can add is worth nothing anyway, and for a
  sharper reason than "it did not help much": the index the query would need **cannot be built at
  all**. `CREATE INDEX ... (thread_id, ((checkpoint->>'ts')::timestamptz))` is rejected with
  *functions in index expression must be marked IMMUTABLE*, because casting text to `timestamptz`
  depends on the session's `TimeZone`. The only buildable form stores the **text**, which
  `max((checkpoint->>'ts')::timestamptz)` never reads — measured on 200 000 threads / 600 000 rows,
  adding `(thread_id, (checkpoint->>'ts'))` moved the thread query from 600 ms to 641 ms, i.e.
  slightly the wrong way, for a 2.7 s build and permanent write amplification on the checkpoint
  path.

  What the missing migration *does* cost is **planner statistics**, and that — not the statement —
  is the whole of the problem this sweep ever had on a large table. `_EXPIRED_THREADS` and
  `_ANALYZE_THREADS` carry the measurements.

- `session_owners` — one row per session id a client has ever created, and the one row that makes a
  session reopenable at all: `api/deps.py::_rehydrate_session` 404s an id this table does not hold.
  It is therefore pruned **behind everything it keys, never in front of it** — a row goes only when
  the session is past the window, nothing session-scoped holds a row for it any more, and no live
  turn lease names it (`_prune_session_owners`). The plain cutoff the pair in `_PRUNABLE` describes
  would be wrong twice over: it would strand rows (every session-scoped sweep in this system starts
  from this table, so an owner row deleted ahead of a checkpoint or a stored tool result puts that
  row beyond both `session_store.delete_session` and `leaver.erase_actor`), and it would delete a
  session a chemist is mid-conversation in, since a transcript is written after the answer exists.

  Measured, the growth is real and small per unit: **124 bytes** per session, index included
  (200 000 rows, 24 MB total relation size) — but it is per *session id created*, and the companion
  UI creates one on the first keystroke, before any message is sent. So an abandoned draft costs a
  permanent row, `_OWNER_LIST` already hides it from the session list (its lateral join drops a
  session with no messages), and nothing ever deleted it: the only `DELETE` against this table was
  `agent/leaver.py`'s actor-scoped erasure, which a deployment that no one leaves never runs.

- `session_turns` is **not** in `_PRUNABLE` and is not refused either: it is a turn *lease*, deleted
  on every clean release, so it does not accumulate under normal operation. What survives is the
  lease a SIGKILLed worker never released — overwritten in place the next time that session claims
  a turn, so it is one row per crashed session and not growth. That row is swept with its session's
  ownership row, in the same transaction, because a lease naming a session nothing can find is
  exactly the orphan the ordering above exists to prevent. A **live** lease is never touched: it is
  what says a turn is running right now.

- `audit_events` is **refused**, by design, not by omission. The trail is the record of who ran
  what, and for a tool call that changed nothing durable it is the *only* record — so disposing of
  it is not a cache decision, it is deciding to stop being able to answer a question about the past.
  Which rows, how old, and exported where first are questions for whoever owns that record; a
  cleanup job on a clock is the wrong place to answer them. The refusal used to be argued from the
  row hash chain that once sat over this table; the chain is gone and the refusal is not, because
  the chain was never the reason. The job says so out loud rather than silently skipping the table.

- `job_records` is **refused**, and it is the newest reason to be careful here (D-157). The table
  exists precisely because a durable run's result used to expire — with Temporal's own history —
  and take a campaign's entire evaluation record with it. Ageing those rows out on a clock would
  restore the failure this system just removed, one retention window later. Its disposal story is
  the same *archive-then-record* design `audit_events` needs, and it belongs in the same ADR.

- `calculation_results` is **refused** for a different reason: D-011 ("never compute twice") is a
  correctness *and* cost guarantee, and evicting a cached result silently converts a cache hit into
  a recomputation — potentially an hours-long search. A cache is bounded by cost policy, not by a
  retention
  clock, so it needs its own eviction design (LRU by access, or by compute cost) rather than an age
  cutoff. Deliberately not lumped in here.

- `bo_campaigns` and `bo_suggestions` are **refused**, and this is the pair the list was silent
  about. A campaign row is the decision space a chemist and an agent jointly framed, and a
  suggestion row snapshots the candidates, the observations they were drawn from and the space they
  were drawn in — migration 031's own words, "the sequence *is* the campaign's history". Both are
  append-only by design, which is what made the silence worth closing rather than what argues for
  pruning them.

  **Erasure already answered this question in the harder direction.** `agent/leaver.py`'s
  `_RETAINED` tier keeps both tables through a *data-subject erasure request* — counting the rows
  and naming why they stay ("who framed an optimization campaign's decision space") — beside
  `audit_events` and `job_records`, the two tables refused above. A retention clock may not dispose
  of what a person asking to be forgotten does not, so the answer here was fixed by a merged
  decision before the question was put.

  The abandoned-campaign case is real and does not overturn it. `campaign_id` is a hash of the
  problem, so a campaign nobody resumes costs one row plus its suggestions and the identity is
  *stable*: deleting it would leave `resume_campaign` reconstructing the same id against an empty
  history, which reads to the next chemist as "nobody has asked this before" rather than as an
  error — the one outcome worse than keeping the row. Growth is not the pressure it would have to
  be either: a row is written per **human ask**, not per model call or per tool call, against
  `tool_result_blobs`'s row per tool call and `checkpoints`' several per turn. `bo_suggestions`
  cascades from `bo_campaigns`, so a policy on the parent would be the whole policy — and
  `cli/rekey_campaigns.py` exists to carry old campaigns *forward* across a problem-hash change,
  which is not something a deployment builds for rows it means to age out.

Every prune is age-based against a per-table window, runs on `background-jobs`, and reports what it
removed so the deletion is itself auditable in the job's own result.
"""

import logging
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from psycopg import AsyncConnection
    from psycopg.rows import TupleRow

    from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
    from chemclaw.agent.message_pairing import droppable_rows, stored_call_ids, unreadable_rows
    from chemclaw.agent.session_store import SELECT_SESSION_ROWS
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection, existing_tables
    from chemclaw.core.logging import log_event
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)

# Tables this job is allowed to prune, with the timestamp column that dates a row and the extra
# predicate that decides whether a row of that table is disposable at all. Explicit and closed: a
# new table is a deliberate addition here, never something a wildcard sweeps up.
#
# `session_events` carries `consumed_at IS NOT NULL` because the module docstring's justification
# for pruning it is that "a **consumed** push-back mailbox row is spent". Age alone was the whole
# predicate, so an undelivered `job_completed` older than the window was destroyed: a durable job
# that outran the retention window — a long conformer search, exactly what this channel exists
# for — lost its
# completion, the session waited on it forever, and the harness "awaiting job" todo never flipped.
# It also destroyed the `system-audit-integrity` and `system-eval-drift` alerts, which by
# construction are never consumed, so retention silently deleted the evidence. (The first channel
# retired with the audit chain's verifier; the argument stands on the second.)
#
# `tool_result_blobs` carries the bare `TRUE` because there is nothing to qualify: every row is a
# trace blob and every trace blob past its window may go. Its link rows are not listed separately
# and must not be — they cascade from the blob (042), so listing them would be a second, racing
# definition of the same disposal.
#
# `checkpoints` is in the register and, like `session_messages`, is not pruned by the plain cutoff
# the pair describes — `_prune_checkpoints` handles it and the pair records only that the table is
# in scope and what dates a row. It has no timestamp column at all: the checkpoint payload carries
# its own `ts`, which is what the expression names.
_PRUNABLE: dict[str, tuple[str, str]] = {
    "session_events": ("created_at", "consumed_at IS NOT NULL"),
    "session_messages": ("created_at", "TRUE"),
    "tool_result_blobs": ("created_at", "TRUE"),
    # **Delivered rows only**, and the predicate is the whole point rather than an optimization. A
    # delivered publication is a receipt for something that now lives in two places, so keeping
    # every one forever would be a third copy of every result this deployment has computed. A
    # `pending` or `failed` row is the only record that something has *not* been published, and
    # sweeping it on a clock would turn a results-store outage into a silent gap — which is the
    # exact failure the outbox exists to prevent. Dated by `delivered_at`, not `enqueued_at`: a row
    # that waited three weeks for a destination to come back should be kept for its full window
    # after it finally arrived, not deleted on arrival.
    "result_publications": ("delivered_at", "state = 'delivered'"),
    "checkpoints": ("(checkpoint->>'ts')::timestamptz", "TRUE"),
    # **Last on purpose.** Like `session_messages` and `checkpoints` this is not pruned by the
    # plain cutoff the pair describes — `_prune_session_owners` handles it, and the pair records
    # only that the table is in scope and what dates a row. The position is the load-bearing part:
    # the sweep may dispose of an ownership row only once the tables that hold the session's actual
    # rows have had their turn in the same pass, because this row is the only way back to any of
    # them. Iteration order is insertion order, so moving this entry up would silently strand rows.
    "session_owners": ("created_at", "TRUE"),
}

# Every other table in this schema, mapped to what bounds its growth instead — or to the fact that
# nothing does and no decision is on record. Together with `_PRUNABLE` this names **every** table,
# and `tests/test_retention.py::test_every_table_in_the_schema_has_a_disposal_decision` asserts
# exactly that, in both directions, against the migrations on disk.
#
# **Why this exists as data rather than as prose.** The module docstring above enumerates what this
# sweep prunes and what it refuses, and reads as though that were the whole schema. It was not:
# three refusals against thirty-three tables. `bo_campaigns` and `bo_suggestions` are how that was
# found — append-only, written on every campaign ask, in neither list — but they were one of thirty
# in the same silence, so adding two entries and leaving the register open would have fixed the
# example and not the defect. The shape is `agent/leaver.py`'s `_RETAINED` (table, why it stays),
# for the same reason: a disposal decision nobody can enumerate is a decision nobody made.
#
# **The keys are checked; the reasons are not.** That is the split `infra/sql/README.md` already
# draws over its own Disposal column — a test for the judgement would be a second copy of the
# answer, while a test for the *set* catches the migration that adds a table and says nothing. This
# register is what that column's "the BACKLOG row ... is the record of which those are" delegates
# to; that row covers `session_owners` and `session_turns` and nothing else, so for the rest of the
# blanks there was no record to delegate to.
#
# **"nothing bounds it" is a finding, not a policy.** Where the decision is genuinely open the entry
# says so rather than inventing an answer, because making the silence visible is this register's
# job and resolving eight unrelated tables in a retention change is not.
#
# **Three of those findings were not findings, and that is worth naming rather than quietly
# fixing.** `note_proposals`, `plan_approvals` and `turn_costs` each read *nothing bounds it* —
# two of them adding *no decision is on record*, which a first telling of this paragraph attributed
# to all three — while `agent/leaver.py`'s `_RETAINED` tier had been keeping all three through a
# data-subject erasure for exactly the reason the four "refused" entries above give — they name who
# did what to the science. One argument, applied to seven tables in one register and to four of
# them in the other, which is what two registers describing one set of tables does when nothing
# joins them. `tests/test_retention.py`'s
# `test_a_table_the_erasure_keeps_is_not_disposed_of_on_a_clock` is that join: it derives the
# rule from `_RETAINED` rather than from four names typed out here, so the next table added to
# the retained tier arrives with a disposal decision already made.
#
# Upstream's own version ledgers are out of scope here, and are named so the omission is deliberate
# rather than another blank: `checkpoint_migrations` and the memory store's
# `store_migrations`/`vector_migrations` are the `schema_migrations` of libraries this repo does not
# own. None is created by a first-party migration or named by a first-party constant, so the test
# below cannot derive them and this register does not claim them. (No count is written into that
# sentence on purpose — the counts in this tree's prose are the thing that goes stale.)
_NOT_PRUNED: dict[str, str] = {
    # Records. Deleting a row does not reclaim a cache, it ends the ability to answer a question
    # about the past — so disposal belongs to whoever owns the record, never to a clock.
    "audit_events": "refused: the record of who ran what — see the docstring above",
    # The local record of a *tool* composite, which has no other one: a primitive is a
    # `calculation_results` row and a job composite a `job_records` row, and both of those are
    # refused here for the same reason. Deleting one does not reclaim a cache — it ends this
    # deployment's ability to republish a computed value it can no longer derive, because a
    # composite's key names its own output and nothing can regenerate it but re-running the
    # science. Bounded by how much composite chemistry a deployment computes, exactly as
    # `calculation_results` is.
    "result_composites": (
        "refused: the only local record of a tool composite, and the source "
        "`publish/backfill.backfill_composites` republishes from"
    ),
    # The record of what this system changed in a system it does *not* own, and who approved
    # it when it could not be undone. Deleting one does not reclaim a cache; it ends the
    # ability to answer "did we file that, and on whose authority" about a change that is
    # still standing on the far side — which may outlive this deployment entirely. Bounded by
    # how often this system acts outside itself, which no job in this repository does at all.
    "effects": (
        "refused: what this system changed outside itself and who approved it. The change on "
        "the far side outlives any window this could be pruned on"
    ),
    # Not a record and not a cache: a *mirror*, upserted on `(source, external_id)` so it
    # converges on the source's snapshot instead of accumulating. What bounds it is the
    # portfolio it reflects — a programme has as many milestones as it has, and a row for a
    # finished one is what makes "what did we deliver last quarter" answerable. A clock
    # cutoff here would delete the delivered half of the very question this table was added
    # for. A source that stops exporting leaves stale rows, and `observed_at` is how a
    # reading says so rather than how a sweep decides.
    "commitments": (
        "refused: a mirror that converges rather than accumulating, bounded by the size of "
        "the portfolio it reflects. Staleness is reported by `observed_at`, not pruned"
    ),
    # Considered for pruning and refused, because the two registers disagreed and one of them
    # had to be wrong. A settled request names who asked somebody to run, review or deliver
    # something and who answered — the attribution for an answer that released a durable
    # workflow, which is the standing `plan_approvals` has and for the same reason. A table
    # retained through a data-subject erasure cannot also be swept on a clock, and
    # `tests/test_retention.py` is what caught the draft that claimed both.
    #
    # Growth is bounded by how often a person is *asked* something, which is human-paced and
    # orders below the session tables: this is not `session_events`, where one turn writes
    # many rows.
    "pending_requests": (
        "refused: the attribution for an answer that released a durable workflow, retained "
        "for the reason `plan_approvals` is. Bounded by how often a person is asked "
        "something, which is human-paced"
    ),
    "job_records": "refused: a durable run's evaluation record, which used to expire with "
    "Temporal's history and take a campaign's results with it (D-157)",
    "calculation_results": "refused: evicting a cached result converts a hit into a "
    "recomputation (D-011); bounded by cost policy, not by a clock",
    "bo_campaigns": "refused: the decision space a chemist framed and the history of what was "
    "proposed against it; kept through erasure, so not disposable on a clock",
    "structures": "refused: a `structure_id` is a handle handed to chemists and taken as an "
    "argument by the next calculation (D-2026-08-21) — pruning breaks it and reclaims nothing",
    "reaction_records": "refused: a row is the only readable form of an ELN run "
    "(D-2026-08-25), so pruning one deletes a result",
    # The prescriptive pair, placed beside `reaction_records` because they are its mirror: that
    # table holds what a chemist did, these hold what somebody is being asked to do. A design is the
    # record of an experiment somebody may still run or may already have run, and its revisions are
    # the corrections that make the whole tier worth keeping — an agent's draft and the expert's
    # edit of it are the two halves of one signal, so a clock that took the second would leave the
    # suggestion standing with the correction gone.
    #
    # **Both refusals are enforced by grant rather than merely intended, which is why they are
    # stated here rather than left implied.** `infra/sql/grants/app_privileges.sql` gives the app
    # INSERT/UPDATE on `experiment_protocols` and INSERT **only** on
    # `experiment_protocol_revisions`, so this sweep holds no DELETE on either and could not prune
    # them even if somebody added a window. The revision rows also cascade from the header row, and
    # that cascade is unreachable for the same reason — the parent is refused too — which is why
    # this entry states its own refusal rather than deferring to the parent the way `bo_suggestions`
    # does.
    "experiment_protocols": "refused: the design of an experiment somebody may still run, kept "
    "through erasure (`leaver._RETAINED`); no DELETE on it is granted, so the refusal is enforced",
    "experiment_protocol_revisions": "refused: the append-only history of a design, whose human "
    "revisions are an expert's corrections of a generated protocol — INSERT-only by grant, so "
    "neither a clock nor an UPDATE can reach one",
    "experiment_protocol_status_events": "refused: who approved, ran or abandoned which revision "
    "of a design, and why — the only record of a sign-off, because a later revision moves the "
    "header's status off it. INSERT-only by grant, like the revisions it points at",
    # Disposed by a mechanism that is not an age cutoff, and deliberately not duplicated here.
    "checkpoint_blobs": "swept by `_prune_checkpoints` with the thread it belongs to, not by a "
    "cutoff of its own — `checkpoints` in `_PRUNABLE` is the key that finds it",
    "checkpoint_writes": "swept by `_prune_checkpoints` with its thread, as `checkpoint_blobs` is",
    "artifact_blobs": "`durable/artifact_eviction.py`, by idle window and size budget",
    "document_files": "`ingest/documents/sync.py`, mark-and-sweep — rows a *complete* crawl did "
    "not see are removed, so a file deleted from the share leaves the index",
    "subscriptions": "deleted on unsubscribe, which is an event rather than an age",
    "observations": "stale rows are retired by status, not deleted",
    "store": "the scratchpad memory store (`agent/scratchpad.py`); erasure reaches it per actor",
    # Cascades. A `ON DELETE CASCADE` parent is the whole policy, and listing the child separately
    # would be a second, racing definition of one disposal.
    "bo_suggestions": "cascades from `bo_campaigns`",
    "calculation_artifacts": "cascades from `artifact_blobs`",
    "tool_result_links": "cascades from `tool_result_blobs` (042)",
    "document_chunks": "cascades in effect from `document_files` — the same sweep removes any "
    "cutting no remaining file row claims",
    # Derived and rebuildable: the source is elsewhere, so a row is regenerable rather than lost.
    "note_index": "derived and rebuildable (`make reindex`); rows for deleted notes are not "
    "removed",
    "reaction_labels": "derived and rebuildable by re-running the corpus drain and the backfill",
    "reaction_species": "derived and rebuildable; a species the source amended away is deleted "
    "with its reaction's record phase",
    "corpus_molecules": "derived and rebuildable by re-draining the corpus",
    "corpus_reactions": "derived and rebuildable by re-draining the corpus",
    # Bounded by construction — the row count cannot run away, so there is nothing to bound.
    "schema_migrations": "never: the ledger is the record of its own work, and the runtime role "
    "cannot write it at all",
    "sync_cursors": "one row per ingest source, so bounded by the source count",
    "corpus_cursors": "one row per append-only corpus source, so bounded by the source count; "
    "deleting a row is the supported way to force a full re-walk",
    "ingest_rejections": "bounded by its own writer (`ingest/rejections.py`): at most "
    "`_MAX_ROWS_PER_SOURCE` rows per source, the least recently refused evicted in the same "
    "transaction as a write (D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask). A "
    "clock is the wrong bound here — the runaway case is a source refusing every record it holds, "
    "which fills the table between two sweeps",
    "session_turns": "a lease, released at turn end; the lease a crashed worker never released is "
    "swept with its session's ownership row by `_prune_session_owners`, never on a clock of its "
    "own — a live lease is what says a turn is running",
    "audit_anchors": "retired with the audit hash chain; nothing writes it and the table is empty",
    "store_vectors": "not created in this deployment — the memory store is built without an "
    "`index_config`, so `AsyncPostgresStore.setup()` never makes it",
    # Nothing bounds these, and no decision is on record. Each is a real open question, not a
    # shorthand for "unimportant"; naming them is what this register is for.
    "molecule_fingerprints": "**nothing bounds it**, and no decision is on record",
    "reaction_fingerprints": "**nothing bounds it**, and no decision is on record",
    "user_preferences": "**nothing bounds it** — one row per person per key, and a preference has "
    "no age at which it stops being current",
    "predictions": "**nothing bounds it** — the calibration ledger's evidence, where pruning a row "
    "changes a calibration rather than reclaiming space; no decision is on record",
    "measurements": "**nothing bounds it** — the calibration ledger's other half, same question",
    "note_proposals": "refused: the PR-gate's record of what was proposed and who decided it — "
    "`leaver._RETAINED` keeps it through an erasure request, so a clock may not take it either",
    "plan_approvals": "refused: who authorized a plan to spend anything, kept through erasure "
    "(`leaver._RETAINED`); consumed rows are marked, never removed",
    "turn_costs": "refused: what a person's turns cost, the record an operator bills against — "
    "kept through erasure (`leaver._RETAINED`), so not disposable on a clock",
}

# The expired threads. The rule is the only correct one and has never changed: **a thread is expired
# exactly when its newest checkpoint is older than the cutoff.** The unit of disposal is a thread —
# `parent_checkpoint_id` chains a thread's checkpoints, so removing the old ones from a thread still
# in use would leave the survivors pointing at rows that are gone.
#
# **This statement was once replaced by a `WITH RECURSIVE` loose index scan and the replacement was
# reverted, because the premise it rested on was measured false.** That premise was: "an aggregate
# has to build every group before the `LIMIT` above it can discard one, so this plans
# `Seq Scan -> HashAggregate -> Sort -> Limit` and its cost tracks the table rather than the cap."
# It is true only of a table with **no statistics**. With statistics, `GROUP BY thread_id ORDER BY
# thread_id` matches `checkpoints_pkey`'s leading column, so the planner streams the index and the
# `LIMIT` stops it — no sort, no hash, no whole-table aggregate:
#
#     Limit
#       -> GroupAggregate  (Group Key: thread_id, Filter: max(...) < cutoff)
#            -> Index Scan using checkpoints_pkey on checkpoints
#
# Measured on 200 000 threads x 3 checkpoints, all expired, cap 501: this reads **1 504 rows of
# 600 000** and runs in **2.5 ms**, against **21.3 ms** for the walk. On 1 000 000 threads x 3 it is
# **2.5 ms** against the walk's **23.2 ms** — the "first pass against a deployment that never
# pruned" case the walk was written for, where the walk is 9x slower.
#
# **The steady state is what decides it.** Retention runs daily, so every pass after the first faces
# a backlog that is *sparse*: nearly every thread is live and the few expired ones may be anywhere
# in `thread_id` order. No statement can be bounded by the cap there — finding the expired minority
# means visiting every thread, and the only question is what one visit costs. This statement pays
# **one streaming index pass**: on 200 000 live threads / 600 000 rows it reads every row exactly
# once in **593 ms**. The walk pays a random index probe *plus* a correlated `max()` per thread:
# **8 147 ms** for the same answer, 13.7x worse, and it read 2.6x the table (26 003 scan rows on a
# 10 000-row table). Under a 2 s `statement_timeout` the walk is **cancelled** where this completes
# in 618 ms — the walk reaches "cancelled, retried, deletes nothing, forever" *sooner* than the
# statement it replaced, which is the failure it was written to prevent.
#
# Bounding the walk itself (`thread.visited < n` in the recursive term) was measured too and is
# dominated: at 200 000 live threads a visit cap of 501 costs 24.6 ms but looks at 0.25% of the
# table and returns nothing — a livelock, since the next pass starts from the same first 501 live
# threads. Raising it to 20 000 costs 847 ms, already *slower* than this statement's full pass while
# still covering 10%. The bounded walk is faster than this only in proportion to how much of the
# table it refuses to look at; at equal coverage it loses by an order of magnitude, and buying
# coverage back needs a durable resume watermark this job has nowhere to keep.
#
# So the fix for the no-statistics case is statistics, not a different statement — see
# `_ANALYZE_THREADS`. `ORDER BY thread_id` is load-bearing rather than cosmetic: it is what makes
# the primary key usable and the plan streamable, and it also makes a capped pass deterministic.
#
# One over the cap is asked for, for the reason `_EXPIRED_SESSIONS` does: to learn whether a tail
# exists at all. It is a probe, not a count — `RetentionOutcome` says so.
_EXPIRED_THREADS = (
    "SELECT thread_id FROM checkpoints "
    "GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s) "
    "ORDER BY thread_id LIMIT %s"
)

# What makes `_EXPIRED_THREADS` plan as the streaming index scan above rather than as a whole-table
# `Seq Scan -> HashAggregate -> Sort`.
#
# `checkpoints` is created by `AsyncPostgresSaver.setup()`, outside `infra/sql`, so no migration
# analyzes it — and until autovacuum first does, the planner has no idea `thread_id` holds hundreds
# of thousands of distinct values and reaches for a parallel hash aggregate. Measured on 200 000
# threads x 3 checkpoints with no statistics, cap 501: `Parallel Seq Scan -> Partial HashAggregate
# -> Sort (external merge, 5.8 MB to disk) -> Finalize GroupAggregate`, **1 526 ms** — against
# **2.5 ms** for the identical statement once analyzed. That window is real and it is exactly the
# first pass on a fresh deployment; it closes at the first autovacuum analyze.
#
# So the sweep analyzes the table itself, every pass, immediately before asking the question. It is
# cheap because `ANALYZE` samples rather than scans: **242 ms** on 600 000 rows, **424 ms** on
# 3 000 000 — a fixed sub-second cost on a job that runs once a day, and it also refreshes the
# statistics this sweep's own deletions invalidate. Measured, the new statistics take effect for
# the planner **inside the sweep's own uncommitted transaction**, which is why this can sit one
# statement ahead of the query it fixes rather than needing a connection of its own.
#
# Unconditional rather than "only when the table has never been analyzed": the conditional needs
# the `reltuples = -1` sentinel (a Postgres internal, version-dependent) to distinguish "never
# analyzed" from "analyzed and empty", and it would still miss the stale-statistics case. A quarter
# of a second a day does not buy that complexity. A role that does not own the table makes
# `ANALYZE` a warning and a no-op rather than an error, so no privilege guard is needed either.
_ANALYZE_THREADS = "ANALYZE checkpoints"

# The three statements the per-session conversation prune needs. Only sessions that actually have an
# expired row are visited, so a deployment whose sessions are all recent pays one indexed scan.
#
# `LIMIT` because one activity must not attempt unbounded work. The first pass against a deployment
# that has never pruned faces every session it has ever had, under a 30 s `statement_timeout` per
# statement — and a pass that times out is retried by Temporal, times out again, and exhausts
# `activity_max_attempts` having deleted nothing. A bounded batch makes progress on every pass and
# the schedule drains the tail; that a tail exists at all is reported rather than dropped.
_EXPIRED_SESSIONS = (
    "SELECT DISTINCT session_id FROM session_messages "
    "WHERE created_at < now() - make_interval(days => %s) "
    "ORDER BY session_id LIMIT %s"
)
_EXPIRED_IDS = (
    "SELECT id FROM session_messages "
    "WHERE session_id = %s AND created_at < now() - make_interval(days => %s)"
)
_DELETE_IDS = "DELETE FROM session_messages WHERE session_id = %s AND id = ANY(%s)"


# What still refers to a session, and the column that names it.
#
# **This is the reachability set, not a convenience list.** Every session-scoped sweep in this
# system starts from `session_owners`: `agent/leaver.py`'s erasure selects session ids out of it,
# and `session_store.delete_session` deletes one session by that row. So an ownership row removed
# while any of these still holds a row for the session does not merely orphan that row — it puts it
# beyond *erasure*, the one sweep that must never be able to miss something. Hence the rule this map
# encodes: the ownership row goes last, and only when nothing here is left.
#
# It is the session-scoped half of `leaver._ERASE`, and `tests/test_retention.py` asserts that
# against `session_store._session_delete_statements()` rather than trusting this comment — the next
# table added to the erasure sweep fails a test here instead of being silently outlived by the row
# that finds it. `tool_result_links` rather than `tool_result_blobs` because the link is the
# session-scoped row (a blob is content-addressed and may be shared with another session, which is
# why `delete_session` deletes it only when no other session links it).
#
# The three checkpoint tables are the ones that can genuinely be absent — they are created by
# `AsyncPostgresSaver.setup()`, not by a migration — and dropping an absent table's arm is exactly
# right rather than a loosening: a table that does not exist holds no rows for the session. All six
# are asked about in one `existing_tables` call rather than three, because the migration-created
# ones answer `present` anyway and a uniform question needs no special case to stay correct.
_SESSION_SCOPED_ROWS: dict[str, str] = {
    "session_messages": "session_id",
    "session_events": "session_id",
    "tool_result_links": "session_id",
    **dict.fromkeys(CHECKPOINT_TABLES, "thread_id"),
}


# Which *other* window has to be set before an ownership row can ever become disposable, per
# session-scoped table, as the ENV name an operator would set.
#
# **The dependency is invisible at the point where it bites, which is why it is written down.**
# `_untouched_arms` refuses an ownership row while anything in `_SESSION_SCOPED_ROWS` still holds a
# row for that session, and each of those tables empties on a window of its own. `tool_result_links`
# is the sharp case: it has no window and no DELETE grant, so a link row disappears only behind its
# blob, on `CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS` — which defaults to 0 like every other window. So
# a deployment that states a conversation policy and nothing else disposes of **no session that ever
# called a tool**, for as long as it runs, and the sweep reports a clean pass every night while
# doing it. That is a silence rather than a failure, and this map is what turns it into a line an
# operator can read.
_OWNERSHIP_DEPENDENCIES: dict[str, tuple[str, str] | None] = {
    # Its own window governs it, but the sweep only reaches `_prune_session_owners` when this
    # window is already set (`_window_days("session_owners")` *is* this setting), so this entry can
    # never be the advice. Recorded rather than omitted, so the map stays the same set as
    # `_SESSION_SCOPED_ROWS` and the test below can say so.
    "session_messages": ("session_messages", "CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS"),
    # **`None` because no window empties the rows that actually block here.** `_PRUNABLE` prunes
    # `session_events` only `WHERE consumed_at IS NOT NULL`, and the population that accumulates is
    # precisely the *unconsumed* one — a CLI launch, a template run, the `digest-<oid>` mailbox
    # nobody opened. Naming `CHEMCLAW_RETENTION_SESSION_EVENTS_DAYS` here said an operator could
    # unblock this by setting a number, which is false: an unconsumed row blocks its ownership row
    # at every window. `docs/planning/DEFERRED.md` carries that as an open row with its own
    # trigger; what belongs here is the absence of a knob, not a knob that does not work.
    "session_events": None,
    "tool_result_links": ("tool_result_blobs", "CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS"),
    **dict.fromkeys(CHECKPOINT_TABLES, ("checkpoints", "CHEMCLAW_RETENTION_CHECKPOINTS_DAYS")),
}


def unwindowed_ownership_dependencies(present: set[str]) -> list[str]:
    """The settings that must also be set before any session holding such a row can be forgotten.

    Public because it answers a deployment question — "why is `session_owners` not shrinking?" —
    and the answer is a list of ENV names rather than a stack trace.

    **It reads `_OWNERSHIP_DEPENDENCIES`, which is a second hand-written map**, and this docstring
    claimed to be "derived from `_SESSION_SCOPED_ROWS` so it cannot drift" — the exact defect the
    ADR beside it says it is repairing, two screens down from the paragraph naming it. Nothing
    derives one from the other, because the *reason* a table blocks is not derivable from the fact
    that it blocks. What closes it instead is
    `test_every_session_scoped_blocker_says_what_would_unblock_it`, which asserts the two maps name
    the same set — so a new blocker fails a test rather than dropping silently out of the advice.

    A `None` entry means "no window empties this", which is a different answer from "the window is
    unset" and is why the value is optional rather than a sentinel string.

    Args:
        present: Which session-scoped tables exist on this connection's search path; an absent one
            holds nothing and so blocks nothing.

    Returns:
        The ENV names, sorted and deduplicated, whose window is 0 while their table exists.
    """
    unset: set[str] = set()
    for table in present:
        dependency = _OWNERSHIP_DEPENDENCIES.get(table)
        if dependency is None:
            # Either the table is not a blocker, or nothing unblocks it — neither is advice.
            continue
        windowed_table, env = dependency
        if _window_days(windowed_table) == 0:
            unset.add(env)
    return sorted(unset)


def _untouched_arms(present: set[str]) -> str:
    """The `NOT EXISTS` chain that makes a session's ownership row disposable.

    One builder for both statements below, because the candidate query and the `DELETE` must ask
    the *same* question — a `DELETE` re-checking a weaker predicate than the query that chose its
    rows would delete rows nobody selected.

    The live-lease arm is part of it and is the one arm about *now* rather than about leftovers: a
    turn writes its transcript only after the answer exists (`api/runner._record_transcript`), so a
    session resumed from an old, empty ownership row genuinely has no rows anywhere while its turn
    is running. The lease is what says so. An *expired* lease is not a live turn — it is the crash
    artifact every other reader already treats as dead (`session_store._TURN_CLAIM` takes an expired
    row over unconditionally) — so it does not protect the ownership row, and it is swept with it.

    Args:
        present: Which of `_SESSION_SCOPED_ROWS` exist on this connection's search path.

    Returns:
        A SQL fragment of `AND NOT EXISTS (...)` arms, against the `o` alias for `session_owners`.
    """
    arms = "".join(
        f" AND NOT EXISTS (SELECT 1 FROM {table} r WHERE r.{column} = o.session_id)"
        for table, column in _SESSION_SCOPED_ROWS.items()
        if table in present
    )
    return arms + (
        " AND NOT EXISTS (SELECT 1 FROM session_turns t"
        " WHERE t.session_id = o.session_id AND t.expires_at > now())"
    )


# The candidate ownership rows. Capped and ordered by the primary key for the reason
# `_EXPIRED_THREADS` is: `ORDER BY o.session_id` matches `session_owners_pkey`, so the planner
# streams the index and the `LIMIT` stops it rather than sorting a whole table's anti-joins.
#
# **Measured, and the measurement is why no migration accompanies this.** On 200 000 ownership rows
# (20 000 abandoned drafts, 180 000 with history), cap 501: `Index Scan using session_owners_pkey`
# under four merge anti-joins, **1.9 ms**, 110 buffers. Adding `session_owners (created_at)` — the
# obvious index for the cutoff — produced the identical plan at **1.8 ms**, because the ordering is
# what drives the scan and the cutoff is not selective (in the case that matters nearly every row is
# older than the window, and the anti-joins are the filter). On a *drained* backlog (180 000 live
# sessions, nothing disposable) the plan becomes one parallel hash anti-join at **147 ms** — with
# that index present and unused, so it would not have helped there either. A migration that changes
# no plan is write amplification on the session-creation path in exchange for nothing.
_DISPOSABLE_SESSIONS = (
    "SELECT o.session_id FROM session_owners o "
    "WHERE o.created_at < now() - make_interval(days => %s){arms} "
    "ORDER BY o.session_id LIMIT %s"
)
# The disposal itself: the ownership row and, behind it, the lease nobody released.
#
# **The predicate is repeated here rather than trusted from the candidate query, and that is what
# closes the race.** Under `READ COMMITTED` every statement takes its own snapshot, so re-asking
# means a session that claimed a turn lease — or wrote a message, or a checkpoint — between the two
# statements is no longer disposable *at the moment of deletion*, and a turn always claims its lease
# before it runs. Without the re-check the sweep could delete the ownership row of a conversation
# that had just resumed, leaving a transcript nothing can find.
#
# One statement rather than two, so a lease and the ownership row it belongs to cannot be committed
# apart: the second `DELETE` reads the first's `RETURNING`, which is what makes "a lease goes only
# if its ownership row went" true rather than intended. Both counts come back in the same row.
_DELETE_SESSIONS = (
    "WITH disposed AS ("
    "  DELETE FROM session_owners o"
    "   WHERE o.session_id = ANY(%s)"
    "     AND o.created_at < now() - make_interval(days => %s){arms}"
    "  RETURNING o.session_id"
    "), leases AS ("
    "  DELETE FROM session_turns t"
    "   WHERE t.session_id IN (SELECT session_id FROM disposed)"
    "  RETURNING t.session_id"
    ") "
    "SELECT (SELECT count(*) FROM disposed), (SELECT count(*) FROM leases)"
)


class RetentionOutcome(BaseModel):
    """What one retention pass removed, per table — the job's own audit record.

    **Both `*_deferred` fields are probes, not counts, and read as "is there a tail" rather than
    "how long is it".** Each is `0` or `1`, because the statement behind it asks for exactly one row
    over the cap and never more: `1` means the backlog outran this pass, `0` means it drained. That
    is deliberate and it is the honest reading — a true remainder needs a second whole-table
    aggregate, measured at 3 444 ms on 3 000 000 rows against the capped query's own 2.5 ms, which
    would make the *report* cost three orders of magnitude more than the work it describes. What the
    fields exist to prevent is the opposite misreading: a cap that is not reported at all makes a
    still-growing table look bounded in every result this job returns.

    `sessions_deferred`, `threads_deferred` and `owners_deferred` are separate fields rather than
    one flag: the three caps bound different units (a conversation, a checkpoint thread, a session's
    ownership row) and an operator deciding whether to raise `retention_max_sessions_per_pass` needs
    to know which one is hitting it.
    """

    deleted: dict[str, int] = {}
    skipped: list[str] = []
    sessions_deferred: int = 0
    threads_deferred: int = 0
    owners_deferred: int = 0


def _window_days(table: str) -> int:
    """The configured retention window for `table`, in days. 0 disables pruning for that table."""
    return {
        "session_events": settings.retention_session_events_days,
        "session_messages": settings.retention_session_messages_days,
        "tool_result_blobs": settings.retention_tool_results_days,
        "result_publications": settings.retention_result_publications_days,
        "checkpoints": settings.retention_checkpoints_days,
        # **The conversation's window, deliberately, rather than a knob of its own.** An ownership
        # row is disposable only once nothing session-scoped is left (`_prune_session_owners`), so
        # a window here is a *floor* — "how long after it was created may an empty session be
        # forgotten" — and not the thing that decides disposal. The one number a deployment already
        # states about how long a conversation is kept is the honest floor for it: a session may not
        # be forgotten sooner than the conversation in it would have been. A second setting could
        # only be set equal to this one (no effect), longer (a delay before an already-empty shell
        # goes) or shorter (no effect either, because the guards, not the clock, are what hold the
        # row) — three values, one outcome, and a fourth thing for an operator to keep in step.
        "session_owners": settings.retention_session_messages_days,
    }[table]


@durable_activity("background")
@activity.defn
async def prune_expired_rows() -> RetentionOutcome:
    """Run one retention sweep, heartbeating so a dead worker is noticed in a minute not ten.

    A thin wrapper rather than a heartbeat inside the sweep, because the sweep has no boundary
    worth reporting at: it walks a closed table map and the slow part is one `DELETE` inside one of
    them, so "which table are we on" is neither stable nor interesting. That is exactly the "opaque
    single call" case `durable/heartbeat.py` was extracted for, and its `finally` guarantees the
    work is not left running detached if the beat itself fails.

    Without this the only thing that would notice a worker dying mid-sweep is
    `retention_timeout_seconds` — ten minutes, on a job whose whole point is to run unattended on a
    schedule.
    """
    return await beating(
        _prune_expired_rows(),
        "retention sweep",
        settings.background_activity_heartbeat_timeout_seconds,
    )


async def _prune_expired_rows() -> RetentionOutcome:
    """Delete rows past their table's retention window; return the per-table counts.

    Each table is pruned **and committed** in its own statement, so one failure cannot roll back
    the others — with one deliberate exception, the three checkpoint tables, which are one thread's
    state and go together (`_prune_checkpoints` says why). That was the docstring's claim before it
    was true: there was a single `commit()`
    after the loop, so a timeout on the second table discarded the first table's deletions and the
    run reported them as done — a sweep that says it removed rows it then rolled back is worse than
    one that fails outright, because the growth it was meant to bound continues while the log says
    otherwise. Committing per table also bounds each transaction to one table's locks.

    The same argument then applied one level down and was not made there: `session_messages` is
    pruned per *session*, and every session's deletions sat in one transaction that committed after
    the loop. A failure on the four thousandth session discarded the first three thousand nine
    hundred and ninety-nine, and the transaction held its row locks across the whole sweep on the
    single-replica background worker. So the fix is the same fix: commit each session
    (D-2026-08-05-a-sweep-that-commits-once).

    The cutoff is computed in SQL (`now() - interval`) so the app clock and the database clock
    cannot disagree about what "expired" means.

    **One table's failure does not stop the sweep from reaching the others.** The tables in
    `_PRUNABLE` are independent — nothing here reads from more than one of them — so a
    `statement_timeout` or a bad row confined to `session_messages` used to end the whole pass
    before `tool_result_blobs` or `checkpoints` were even attempted: the loop had no `try/except`,
    so an exception from one table's block propagated straight out of this function. Against a
    deployment where that one table has a persistent problem (an oversized session, a malformed
    row), every *other* table would never be pruned again until the first was fixed, and nothing in
    the job's own result said so — only Temporal's activity-failure log, which is not where an
    operator reading a retention report looks. Each table's block is now caught, logged and rolled
    back on its own, so its neighbours still get their turn in the same pass; the first exception is
    re-raised once every table has been attempted, so the activity still fails and Temporal still
    retries — the same outcome as before for the table that actually failed, with the isolation as
    the only change. The rollback matters beyond tidiness: an uncaught error leaves the connection
    in Postgres's aborted-transaction state, where every later statement on it fails too, so without
    it the "still attempt the rest" half of this fix would not work at all.
    """
    outcome = RetentionOutcome(deleted={}, skipped=[])
    first_error: BaseException | None = None
    async with connection(settings.postgres_dsn) as conn:
        for table, (column, disposable) in _PRUNABLE.items():
            days = _window_days(table)
            if days <= 0:
                outcome.skipped.append(f"{table} (retention disabled)")
                continue
            try:
                if table == "session_messages":
                    # Not a single sweeping DELETE: a conversation row's disposability depends on
                    # rows that may not be expiring (see the module docstring). Per session, through
                    # the pairing closure — and committing per session, which is why no `commit()`
                    # follows this call.
                    deleted, deferred = await _prune_session_messages(conn, days)
                    outcome.deleted[table] = deleted
                    outcome.sessions_deferred = deferred
                    continue
                if table == "session_owners":
                    # The ownership row is the only way back to a session's rows, so it is disposed
                    # of behind them — last in `_PRUNABLE`, and only when nothing holds a row for
                    # the session (`_prune_session_owners`). It commits itself, as the two branches
                    # below do, which is why no `commit()` follows this call.
                    owners, deferred = await _prune_session_owners(conn, days)
                    outcome.deleted.update(owners)
                    outcome.owners_deferred = deferred
                    continue
                if table == "checkpoints":
                    # Three tables, one thread, one transaction — see `_prune_checkpoints`. It
                    # reports each table separately because that is what an operator can go and look
                    # at, and it commits itself, which is why no `commit()` follows this call
                    # either.
                    counts, skipped, deferred = await _prune_checkpoints(conn, days)
                    outcome.deleted.update(counts)
                    outcome.skipped.extend(skipped)
                    outcome.threads_deferred = deferred
                    continue
                async with conn.cursor() as cur:
                    # Table and column come from the closed `_PRUNABLE` map above, never from a
                    # caller, so the interpolation cannot carry untrusted input; the *value* is
                    # bound.
                    await cur.execute(
                        f"DELETE FROM {table} "
                        f"WHERE {disposable} AND {column} < now() - make_interval(days => %s)",
                        (days,),
                    )
                    outcome.deleted[table] = cur.rowcount
                await conn.commit()
            except Exception as exc:  # isolated per table; re-raised once every table is tried
                await conn.rollback()
                logger.exception(
                    "retention sweep failed for table %s; the other tables are still attempted",
                    table,
                )
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error
    return outcome


async def _prune_session_messages(conn: AsyncConnection[TupleRow], days: int) -> tuple[int, int]:
    """Delete expired conversation rows, never splitting a tool-call pairing.

    Returns `(rows deleted, 1 if expired sessions remain beyond this pass's cap else 0)`.

    Three statements per session rather than one across the table, because the decision is not
    expressible in SQL: whether an expired row may go depends on whether the rows *paired with it*
    are also going, and those may be newer than the cutoff.

    Reads the session's **whole** history, not just its expired rows. That is the point — a
    candidate's partner being non-expired is exactly the case worth catching, and a partial view
    would report the split component as safe. Sessions are handled one at a time so the memory cost
    is one conversation, not the whole expired backlog.

    **One transaction per session.** Each session's deletion is committed before the next is read,
    so a failure part way through keeps everything already removed rather than discarding the whole
    pass — the identical argument `prune_expired_rows` makes for committing per table, which had
    not been made here. It also bounds how long this holds row locks: one session's worth, not the
    entire backlog's, which matters because the sweep shares the single-replica background worker
    with every other scheduled activity.

    The batch is capped and the existence of a tail returned. A first pass against a deployment
    that has never pruned would otherwise take an unbounded number of round trips inside one
    activity, and
    exceeding `retention_timeout_seconds` costs an attempt having committed only what it reached —
    with the cap it commits a bounded amount and says whether anything is left.
    """
    deleted = 0
    cap = settings.retention_max_sessions_per_pass
    async with conn.cursor() as cur:
        await cur.execute(_EXPIRED_SESSIONS, (days, cap + 1))
        session_ids = [row[0] for row in await cur.fetchall()]
    # One over the cap was requested purely to learn whether there is a tail; it is not worked.
    deferred = max(len(session_ids) - cap, 0)
    for session_id in session_ids[:cap]:
        async with conn.cursor() as cur:
            await cur.execute(SELECT_SESSION_ROWS, (session_id,))
            # Call ids, not deserialised messages. The rows of one session may be in *either*
            # stored shape — the M6 conversion pass is resumable — and the previous version read
            # them all with MAF's `Message.from_dict`, which raises `TypeError` on a LangChain
            # payload. So the sweep crashed on any session that had taken a turn since the
            # conversion, Temporal retried it to exhaustion, and retention silently stopped for
            # exactly the sessions still in use.
            rows = [(int(row[0]), stored_call_ids(row[1], row[2])) for row in await cur.fetchall()]
            if unreadable := unreadable_rows(rows):
                # Refuse the whole session rather than the row: an unreadable row links to nothing,
                # so pruning around it could strand a pairing it would have protected.
                logger.warning(
                    "skipping retention for session %s: %d row(s) in an unrecognised stored "
                    "shape (ids: %s)",
                    session_id,
                    len(unreadable),
                    ", ".join(str(row_id) for row_id in unreadable[:10]),
                )
                continue
            await cur.execute(_EXPIRED_IDS, (session_id, days))
            expired = {int(row[0]) for row in await cur.fetchall()}
            disposable = droppable_rows(rows, expired)
            if not disposable:
                continue
            await cur.execute(_DELETE_IDS, (session_id, sorted(disposable)))
            deleted += max(cur.rowcount, 0)
        await conn.commit()
    return deleted, deferred


async def _prune_session_owners(
    conn: AsyncConnection[TupleRow], days: int
) -> tuple[dict[str, int], int]:
    """Forget the sessions nothing can reopen: the ownership row, and the lease nobody released.

    Returns `({table: rows deleted}, 1 if disposable sessions remain beyond this pass's cap
    else 0)`.

    **A row here is what makes a session reopenable, so the question is not its age.**
    `api/deps.py::_rehydrate_session` answers 404 for a session id this table does not hold — that
    is the whole function of the row — and `_OWNER_LIST` already hides a session with no messages
    from the listing, so a client can only reach one of these by an id it still remembers. What
    decides disposal is therefore whether anything is left to reopen *into*: a row goes when the
    session is past the window, no table in `_SESSION_SCOPED_ROWS` holds a row for it, and no live
    turn lease names it.

    **That is also the ordering rule, and getting it backwards is the expensive failure.** Every
    session-scoped sweep in this system starts from this table, so an ownership row deleted ahead of
    a checkpoint, a stored tool result or an unconsumed push-back event does not just orphan that
    row — it puts it beyond `session_store.delete_session` *and* beyond `leaver.erase_actor`. So
    this runs last in `_PRUNABLE`, after the tables it keys have had their turn in the same pass,
    and it deletes nothing whose rows those tables did not manage to remove first. A session whose
    conversation was pruned earlier in this very pass is disposable in this one, and that is
    correct rather than hasty: what is left of it at that point is an empty shell the session list
    does not show and a resumed transcript would render blank.

    The lease goes with it and only with it (`_DELETE_SESSIONS`). An abandoned lease is a crash
    artifact — a worker SIGKILLed before `_TURN_RELEASE` — and it is not growth, because
    `session_turns` is keyed by `session_id` and the next claim on that session overwrites it in
    place. What it must never be is *collected on a clock of its own*: a lease that has not expired
    is a turn running right now, and deleting it hands the running turn's next refresh a false
    takeover (`api/state.py::_hold_turn_claim` counts one and stops beating).

    Capped and reported like the two passes above, for the same reason: the first pass against a
    deployment that has never pruned faces every abandoned draft the deployment has ever created,
    and a cap that is not reported reads as a drained backlog.
    """
    cap = settings.retention_max_sessions_per_pass
    async with conn.cursor() as cur:
        present = await existing_tables(cur, set(_SESSION_SCOPED_ROWS))
        # Said once per pass, before the query rather than after a disappointing count: a zero here
        # means "nothing was disposable", and an operator cannot tell that from "nothing is left".
        blocked_by = unwindowed_ownership_dependencies(present)
        if blocked_by:
            log_event(
                logger,
                "retention.ownership_blocked",
                "session_owners can only forget sessions that never wrote to the tables these "
                "unset windows govern: %s",
                ", ".join(blocked_by),
                level=logging.WARNING,
                unset_windows=", ".join(blocked_by),
            )
        arms = _untouched_arms(present)
        await cur.execute(_DISPOSABLE_SESSIONS.format(arms=arms), (days, cap + 1))
        found = [str(row[0]) for row in await cur.fetchall()]
        deferred = max(len(found) - cap, 0)
        sessions = found[:cap]
        if not sessions:
            return {"session_owners": 0, "session_turns": 0}, 0
        await cur.execute(_DELETE_SESSIONS.format(arms=arms), (sessions, days))
        counted = await cur.fetchone()
    await conn.commit()
    deleted = {
        "session_owners": int(counted[0]) if counted else 0,
        "session_turns": int(counted[1]) if counted else 0,
    }
    logger.info(
        "forgot %d session(s) nothing can reopen (%d abandoned turn lease(s) with them); %s",
        deleted["session_owners"],
        deleted["session_turns"],
        "more remain for the next pass" if deferred else "the backlog is drained",
    )
    return deleted, deferred


async def _prune_checkpoints(
    conn: AsyncConnection[TupleRow], days: int
) -> tuple[dict[str, int], list[str], int]:
    """Delete every trace of threads whose newest checkpoint has expired.

    Returns `(rows deleted per table, tables skipped with the reason, 1 if a tail remains else 0)`.

    **The pass analyzes `checkpoints` before it queries it, and that one statement is what bounds
    the work.** `_ANALYZE_THREADS` carries the measurement; the short version is that this table is
    created outside `infra/sql`, so nothing ever gives the planner statistics for it, and without
    them `_EXPIRED_THREADS` plans as a whole-table parallel hash aggregate that spills to disk
    (1 526 ms on 600 000 rows) instead of as a `LIMIT`-terminated scan of `checkpoints_pkey`
    (2.5 ms). Analyzing costs 242 ms there and 424 ms on 3 000 000 rows, once a day.

    On a *drained* backlog — every pass after the first, since this job runs daily — no statement
    can be bounded by the cap at all: the few expired threads may be anywhere in `thread_id` order,
    so finding them means visiting every thread. What the cap still buys is a bounded amount of
    *deletion*, and what the analyzed plan buys is that the visit is one streaming index pass
    (593 ms over 200 000 live threads) rather than a random probe per thread (8 147 ms, and
    cancelled under a 2 s statement timeout).

    **The cap is reported, for the reason `_prune_session_messages` reports its own** — and as a
    probe rather than a remainder (`RetentionOutcome` says why). One over the cap is selected
    purely to learn whether a tail exists and is never worked; without it, a first pass against a
    deployment with fifty thousand expired threads returns the cap as its deleted count and an
    empty `skipped`, which reads as a drained backlog rather than as one pass of many.

    **One transaction across all three tables, against this module's own per-table rule.** That rule
    exists so one table's failure cannot roll back another's, and it holds because those tables are
    independent. These three are not: they are one thread's state split across three keys with no
    foreign key to enforce it. Committing them separately gives a crash between two commits a choice
    of two bad outcomes — surviving `checkpoints` rows referring to blobs that are gone (a thread
    that now raises when read) or orphaned blobs no later pass can find (because the thread query
    runs over `checkpoints`, and that thread no longer has any). One transaction has neither, and it
    is bounded by the batch cap rather than by the backlog.

    **A malformed `ts` fails this pass loudly, and that is the answer rather than an oversight.**
    The thread query casts `checkpoint->>'ts'` to `timestamptz`, and Postgres has no `TRY_CAST` — a
    checkpoint payload whose `ts` is missing or unparseable raises, the activity fails, and Temporal
    surfaces it. Two things make that the right failure. Every table ahead of `checkpoints` in
    `_PRUNABLE` commits in its own statement, so the pass keeps the disposal it already did — and
    "ahead of" is the whole claim, because `checkpoints` is *not* last. `session_owners` is, on
    purpose, and its own entry says so forty lines up: it is the only way back to a session's rows,
    so it may go only once every table holding them has had its turn. This sentence used to say
    `checkpoints` was last, which made two comments in one module disagree about the order the
    module depends on, and left the next reader inserting a table taking a false invariant from
    whichever of them read as the more detailed. What this argument actually needs is only that the
    disposal already done has committed, which stays true however the list grows.
    And swallowing the error would turn a data-disposal job that *cannot run* into one that reports
    success while a table grows — the exact reading `sessions_deferred` and `threads_deferred` exist
    to prevent. No guard is written for it because none has been needed: `ts` is a field of
    LangGraph's own `Checkpoint`, written by `create_checkpoint` on every write, and a release that
    changed it would break `AsyncPostgresSaver` before it reached this sweep. The cast runs over
    every row the grouping scan reaches, so one malformed `ts` anywhere ahead of the cap fails the
    whole checkpoint pass rather than only the pass that would have deleted its thread — earlier
    and louder, which for a job that must not silently stop disposing is the right direction. A
    *missing* `ts` is not that case and needs no guard: `checkpoint->>'ts'` is then SQL `NULL`,
    `max()` ignores it, and a thread with no timestamp at all is simply never expired.

    **Skipped, not failed, when the tables are absent.** They are created by
    `AsyncPostgresSaver.setup()` rather than by a migration, so a deployment that has never run the
    graph engine does not have them — and a sweep that raised there would stop pruning the three
    tables it had already handled on every subsequent pass, which is the opposite of what a
    retention job is for. `core.db.existing_tables` is asked once, because the check cannot live
    inside the `DELETE` (Postgres resolves the relation at parse time).
    """
    async with conn.cursor() as cur:
        present = await existing_tables(cur, CHECKPOINT_TABLES)
        missing = sorted(set(CHECKPOINT_TABLES) - present)
        if missing:
            # All or nothing: the tables are created together by one `setup()`, so a partial set is
            # a schema nobody has, and guessing which half to prune would be inventing a case.
            return {}, [f"{', '.join(missing)} (no checkpointer in this schema)"], 0
        # Before the question, not after: `_EXPIRED_THREADS` only plans as a `LIMIT`-terminated
        # index scan when the planner has statistics for a table no migration can give them to.
        await cur.execute(_ANALYZE_THREADS)
        cap = settings.retention_max_sessions_per_pass
        await cur.execute(_EXPIRED_THREADS, (days, cap + 1))
        found = [str(row[0]) for row in await cur.fetchall()]
        deferred = max(len(found) - cap, 0)
        threads = found[:cap]
        if not threads:
            return dict.fromkeys(CHECKPOINT_TABLES, 0), [], 0
        deleted: dict[str, int] = {}
        for table in CHECKPOINT_TABLES:
            # `CHECKPOINT_TABLES` is a module constant of the checkpointer's own, never a caller's,
            # so the interpolation cannot carry untrusted input; the thread ids are bound.
            await cur.execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)",
                (threads,),
            )
            deleted[table] = max(cur.rowcount, 0)
    await conn.commit()
    logger.info(
        "pruned %d expired checkpoint thread(s); %s",
        len(threads),
        "more remain for the next pass" if deferred else "the backlog is drained",
    )
    return deleted, [], deferred


@durable_workflow("background")
@workflow.defn
class RetentionWorkflow:
    """Enforce the deployment's retention windows on a cadence (gap SCH-1)."""

    @workflow.run
    async def run(self) -> RetentionOutcome:
        """Run one retention pass and return what it removed."""
        return await workflow.execute_activity(
            prune_expired_rows,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout the heartbeats the activity now sends do nothing for
            # failure detection: a worker that dies mid-sweep would be noticed only when the
            # ten-minute start-to-close budget expired. `connectors/calc/workflows.py` states the
            # rule; core's own long work simply never applied it. The beat is derived from this
            # same number (`durable/heartbeat.py::beating`), so the two cannot drift.
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )
