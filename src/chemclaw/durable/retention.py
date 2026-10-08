"""Bounded growth for the durable stores.

Every table in the schema is in exactly one of `_PRUNABLE` (swept here, age-based against a
per-table window on `background-jobs`) and `_NOT_PRUNED` (what bounds it instead);
`tests/test_retention.py` holds that against the migrations. The swept tables, and why:

- `session_events` — a **consumed** push-back mailbox row is spent. An unconsumed one is what will
  wake the stream waiting on it, so age alone never disposes of it. Exception: `exhibit` pushes
  are notifications over the artefact list and go on age alone, in `prune_exhibit_pushes`.
- `session_messages` — conversation history, pruned per session through `droppable_rows`: a
  `tool_use` and its `tool_result` are one unit (deleting either half breaks the thread
  permanently), and a pair can straddle the cutoff, so a row goes only with its partner.
- `session_exhibits` — artefacts beside the chat; working drafts, not records. Dated by
  `updated_at`, so an artefact still being edited is never aged out; revisions cascade.
- `session_attachments` — uploaded files, on the conversation's window and dated by
  `created_at`, so an upload lives exactly as long as the message it came with.
- `tool_result_blobs` — full tool output kept for rendering; it holds no record (answers live in
  `calculation_results` and `job_records`), so a plain `created_at` cutoff suffices. Link rows
  cascade from the blob.
- `result_publications` — **delivered rows only**, dated by `delivered_at`. A `pending` or
  `failed` row is the only record that something has not been published, so a clock must never
  take it.
- `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` — LangGraph turn state, created by
  `AsyncPostgresSaver.setup()` rather than a migration. Pruned per **thread**, when its newest
  checkpoint expires, all three tables in one transaction. Superseded checkpoints inside a live
  thread are bounded by the writer (`agent/checkpointer`); `_REPAIR_ORPHANED` reaches blob/write
  rows with no `checkpoints` row. No migration can index or analyze these tables, so the sweep
  analyzes them itself.
- `session_owners` — the row that makes a session reopenable, so it is pruned **behind**
  everything it keys: only once the session is past the window, nothing session-scoped holds a
  row for it, and no live turn lease names it (`_prune_session_owners`).
- `session_turns` is **not** in `_PRUNABLE`: a lease is deleted on clean release; a crashed
  worker's lease is swept with its session's ownership row. A live lease is never touched.

The argued refusals:

- `audit_events` is **refused**: it is the record of who ran what. Disposal is a records-owner
  decision (archive, then record), not a cleanup job's.
- `job_records` is **refused**: it exists so a durable run's result does not expire with
  Temporal history; ageing it out would restore that failure.
- `calculation_results` is **refused**: evicting a cached result turns a hit into a possibly
  hours-long recomputation (D-011). A cache needs a cost-based eviction design, not an age cutoff.
- `bo_campaigns` and `bo_suggestions` are **refused**: the campaign is its sequence of asks, the
  erasure path (`agent/leaver._RETAINED`) already keeps them, and deleting a campaign would make
  `resume_campaign` silently restart it under the same id.

Each pass reports rows deleted, then vacuums what it deleted from and reports `bytes_on_disk` and
`bytes_reclaimed`, and publishes `chemclaw_table_bytes`; a `DELETE` alone reclaims nothing.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic
from typing import Literal

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
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.exhibits.models import PUSH_KIND as EXHIBIT_PUSH_KIND

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)

# Tables this job may prune: the column (or expression) that dates a row and the predicate that
# decides whether a row is disposable at all. Closed: a new table is a deliberate addition.
#
# `tool_result_blobs`' link rows cascade from the blob and must not be listed separately.
# `checkpoints`, `session_messages` and `session_owners` are pruned by their own functions, not the
# plain cutoff; their entries record scope and dating. `checkpoints` has no timestamp column, so the
# payload's own `ts` dates it. Exhibit pushes have their own pass because the map is keyed by table.
_EXHIBIT_PUSHES = f"kind = '{EXHIBIT_PUSH_KIND}'"

_PRUNABLE: dict[str, tuple[str, str]] = {
    "session_events": ("created_at", "consumed_at IS NOT NULL"),
    "session_messages": ("created_at", "TRUE"),
    "tool_result_blobs": ("created_at", "TRUE"),
    # Delivered rows only, dated by `delivered_at` — the module docstring carries why, with every
    # other swept table's argument.
    "result_publications": ("delivered_at", "state = 'delivered'"),
    "checkpoints": ("(checkpoint->>'ts')::timestamptz", "TRUE"),
    # Dated by the last revision, so an artefact still being edited is never aged out; revisions
    # cascade from the header.
    "session_exhibits": ("updated_at", "TRUE"),
    # Uploads, dated by when they arrived, on the conversation's window — the module docstring
    # carries why. Held or dropped alike: a dropped upload's row is only its name.
    "session_attachments": ("created_at", "TRUE"),
    # **Last on purpose.** An ownership row is the only way back to a session's rows, so it may be
    # disposed of only after the tables holding them have had their turn in this pass. Iteration
    # order is insertion order; moving this entry up would strand rows.
    "session_owners": ("created_at", "TRUE"),
}

# Every other table, mapped to what bounds its growth instead — or to the finding that nothing does.
# Together with `_PRUNABLE` this names every table; `tests/test_retention.py` checks the key set in
# both directions against the migrations, and checks that a table the erasure path retains is not
# swept. The reasons are not tested.
#
# Upstream libraries' own version ledgers (`checkpoint_migrations`, `store_migrations`,
# `vector_migrations`) are out of scope.
_NOT_PRUNED: dict[str, str] = {
    # Records. Deleting a row does not reclaim a cache, it ends the ability to answer a question
    # about the past — so disposal belongs to whoever owns the record, never to a clock.
    "audit_events": "refused: the record of who ran what — see the docstring above",
    # The record of changes this system made in systems it does not own, and who approved them; the
    # change may outlive this deployment. Bounded by how often the system acts outside itself.
    "effects": (
        "refused: what this system changed outside itself and who approved it. The change on "
        "the far side outlives any window this could be pruned on"
    ),
    # A mirror upserted on `(source, external_id)`, bounded by the portfolio it reflects; a
    # delivered milestone is what answers "what did we deliver". `commitment_sync.sweep_withdrawn`
    # removes what a snapshot source stopped exporting — convergence on the source, not retention by
    # age.
    "commitments": (
        "refused: a mirror that converges rather than accumulating, bounded by the size of "
        "the portfolio it reflects and swept down by `commitment_sync.sweep_withdrawn` where "
        "the source is a snapshot. Staleness is reported by `observed_at`, not pruned on a clock"
    ),
    # Refused: a settled request is the attribution for an answer that released a durable workflow,
    # and the erasure path retains it. Growth is human-paced.
    "pending_requests": (
        "refused: the attribution for an answer that released a durable workflow, retained "
        "for the reason `plan_approvals` is. Bounded by how often a person is asked "
        "something, which is human-paced"
    ),
    # Inherits `pending_requests`' refusal: these rows are its attribution, archived so the question
    # can be asked again. One row per re-asked answered cycle.
    "pending_request_answers": (
        "refused: `pending_requests`' attribution, archived on a re-ask so the answer is not "
        "blanked — the same record, so the same refusal"
    ),
    # One row per principal, reset in place by `api/budget_store.py`'s upsert, so size tracks
    # distinct users, not turns. Erased with its subject (`leaver._ERASE`), which is the only
    # removal.
    "budget_usage": (
        "refused: one row per principal, reset in place rather than appended, so a clock would "
        "reclaim nothing; erased with its subject instead (`agent/leaver.py::_ERASE`)"
    ),
    # One row per principal, updated in place by every authenticated request; a bucket idle long
    # enough to be full is the same as no row, so the only removal that matters is erasure.
    "request_buckets": "refused: one row per principal, updated in place rather than appended, so "
    "its size is the number of people served and a clock would reclaim only departed users; "
    "erased with its subject (`agent/leaver.py::_ERASE`)",
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
    # The prescriptive pair, mirror of `reaction_records`: a design and the expert's corrections to
    # it are one signal. Enforced by grant: the app holds no DELETE on either table, and revisions
    # cascade only from the (also refused) header.
    "composed_workflows": (
        "refused: a procedure a chemist asked to have written down and is still using. Bounded "
        "by composed.MAX_PER_OWNER at the write instead, because how many working procedures "
        "somebody keeps is their own decision and a clock is the wrong owner of it. Two things "
        "delete one and neither is a clock: the owner saying so (`DELETE /workflows/{name}`, "
        "`/forget-workflow`) and offboarding (agent/leaver.py)"
    ),
    "experiment_protocols": "refused: the design of an experiment somebody may still run, kept "
    "through erasure (`leaver._RETAINED`); no DELETE on it is granted, so the refusal is enforced",
    "experiment_protocol_revisions": "refused: the append-only history of a design, whose human "
    "revisions are an expert's corrections of a generated protocol — INSERT-only by grant, so "
    "neither a clock nor an UPDATE can reach one",
    "experiment_arm_results": "refused: what a designed arm actually produced — the only record "
    "that a design was ever run, and the corpus the deferred protocol-diff miner needs. A "
    "re-measured well is a second observation rather than a correction, so a sweep that pruned the "
    "older row would delete the evidence that two assays disagree. INSERT-only by grant, like the "
    "revisions it points at, and it cascades from a header nothing deletes",
    "experiment_protocol_status_events": "refused: who approved, ran or abandoned which revision "
    "of a design, and why — the only record of a sign-off, because a later revision moves the "
    "header's status off it. INSERT-only by grant, like the revisions it points at",
    # Disposed of with their thread by `_prune_checkpoints`; rows whose thread has no `checkpoints`
    # row (left by a restore or hand surgery) are reached by `_REPAIR_ORPHANED` instead.
    "checkpoint_blobs": "swept by `_prune_checkpoints` with the thread it belongs to, not by a "
    "cutoff of its own — `checkpoints` in `_PRUNABLE` is the key that finds it. A row whose "
    "thread has no `checkpoints` row at all is reached by neither delete and is repaired by "
    "`_repair_orphaned_checkpoint_rows` instead",
    "checkpoint_writes": "swept by `_prune_checkpoints` with its thread, as `checkpoint_blobs` "
    "is, and repaired by `_repair_orphaned_checkpoint_rows` on the same terms when its thread "
    "has no `checkpoints` row",
    "artifact_blobs": "`durable/artifact_eviction.py`, by idle window and size budget",
    "document_files": "`ingest/documents/sync.py`, mark-and-sweep — rows a *complete* crawl did "
    "not see are removed, so a file deleted from the share leaves the index",
    "subscriptions": "deleted on unsubscribe, which is an event rather than an age",
    "observations": "stale rows are retired by status, not deleted",
    # A memory is written to persist, so the bound is a row count per namespace enforced by the
    # writer; `updated_at` only picks what goes.
    "store": "bounded by its writer (`agent/scratchpad.BoundedStoreBackend`): at most "
    "`agent_memory_max_files` per actor namespace, the least recently updated evicted on write. "
    "A clock is the wrong bound — a memory is written to persist, so age says nothing about "
    "worth. Eventual rather than atomic: the store shares the checkpointer's autocommit pool, so "
    "the invariant is the cap plus whatever is in flight",
    # Cascades. A `ON DELETE CASCADE` parent is the whole policy, and listing the child separately
    # would be a second, racing definition of one disposal.
    "bo_suggestions": "cascades from `bo_campaigns`",
    "calculation_artifacts": "cascades from `artifact_blobs`",
    "tool_result_links": "cascades from `tool_result_blobs` (042)",
    "session_exhibit_revisions": "cascades from `session_exhibits` (115)",
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
    "session_members": "bounded by the sessions it belongs to: at most the people one owner "
    "admitted per session, cascading from the session's ownership row "
    "(`infra/sql/110_shared_sessions.sql`), so `_prune_session_owners` takes it with the session "
    "— never on a clock of its own, because a membership is what lets a person back into a live "
    "conversation",
    "plan_authors": "one row per distinct plan a session's turns wrote, cascading from the "
    "session's ownership row as `session_members` does and for the same reason",
    "session_turn_queue": "a lease per waiting message, deleted when it runs; a lapsed one is "
    "swept by the next enqueue on its session, and the whole line cascades from the session's "
    "ownership row (`infra/sql/113_session_turn_queue.sql`) — never on a clock of its own, "
    "because a live row is a person waiting",
    "session_turn_remotes": "a lease per request another replica addresses to a running turn, "
    "withdrawn by its asker when the follow or the stop ends; a lapsed one is swept by the "
    "holder's next poll, and the whole set cascades from the session's ownership row "
    "(`infra/sql/121_session_turn_remotes.sql`) — never on a clock of its own, because a live row "
    "is somebody following or stopping a turn",
    "session_turn_frames": "frames in transit between two replicas, deleted by the asker as it "
    "reads them and by cascade with their request (`infra/sql/121_session_turn_remotes.sql`)",
    "calculation_claims": "a claim per calculation being computed, deleted by its holder when the "
    "result is persisted, fails to a waiter or is cancelled; a crashed holder's row lapses on the "
    "database clock and the next claim on its key replaces it in place, so the only residue is a "
    "key-sized row per key whose last attempt died and was never asked for again "
    "(`infra/sql/125_calculation_claims.sql`) — never on a clock of its own, because a live row is "
    "a computation another pod is waiting on",
    "audit_anchors": "retired with the audit hash chain; nothing writes it and the table is empty",
    "store_vectors": "not created in this deployment — the memory store is built without an "
    "`index_config`, so `AsyncPostgresStore.setup()` never makes it",
    # Traffic cannot grow the fingerprint pair (upsert on a structural key), but the definition is
    # in the key so two generations coexist during a rebuild; the bound is corpus times definitions
    # ever written, and the app holds no DELETE. A `STANDARDIZATION_VERSION` bump is a permanent
    # doubling.
    "molecule_fingerprints": "bounded by the corpus times the fingerprint definitions ever "
    "written: the key is `(id, definition)` (094), so traffic cannot grow it and a definition "
    "bump forks it permanently — the runtime role holds no DELETE, so nothing reclaims the "
    "superseded generation",
    "reaction_fingerprints": "same as `molecule_fingerprints`, keyed `(source, id, definition)`",
    "user_preferences": "bounded by its writer (`agent/preferences.py`): at most "
    "`preferences_max_per_owner` per person, the least recently updated evicted in the same "
    "transaction as the write. Agent-writable on a *model-chosen* key, which is why one row per "
    "person per key was never a bound; `preferences_recall_limit` separately bounds what re-enters "
    "the prompt",
    # `predictions` keeps `calc_version` in its key because a calibration compares versions; the
    # right bound would be a version-retirement policy, a scientific decision. `measurements` is
    # lab-paced.
    "predictions": "unbounded, accepted — the calibration ledger's evidence, keyed by "
    "`calc_version`, so a version bump forks it and the old rows are what a calibration compares "
    "against. The bound that would be right is a `calc_version` retirement policy, which is a "
    "scientific decision and is not made here",
    "measurements": "unbounded, accepted — the calibration ledger's other half, written at human "
    "pace: one row per measurement somebody actually made",
    "note_proposals": "refused: the PR-gate's record of what was proposed and who decided it, for "
    "as long as there was a gate to decide anything "
    "(`D-2026-09-05-the-gate-is-deleted-not-dormant` retired it and nothing writes a row now). "
    "**Retired is not disposable**: a deployment that ran the gate holds real sign-offs by real "
    "people, `leaver._RETAINED` keeps the table through an erasure request, and a clock may not "
    "take what an erasure may not",
    "plan_approvals": "refused: who authorized a plan to spend anything, kept through erasure "
    "(`leaver._RETAINED`); consumed rows are marked, never removed",
    "behaviour_proposals": "refused: who decided what this system was allowed to become — the "
    "`plan_approvals` reason, one layer up, since a proposal is about the agent's behaviour rather "
    "than one plan's spend. Kept through erasure (`leaver._RETAINED`), and a decision is never "
    "overwritten, so a rejection survives the same text arriving again. **It retains more than the "
    "row above it and that is stated rather than inherited**: a plan approval keeps a hash and a "
    "verdict, while this keeps `content` — a whole document, about one person's chemistry, after "
    "they leave. The justification is real (a rejection is only evidence if the text it rejected "
    "is still there, which is `note_proposals`' own argument for keeping the body verbatim) and it "
    "is a larger claim, so it is written here rather than left to the neighbour's sentence",
    "turn_costs": "refused: what a person's turns cost, the record an operator bills against — "
    "kept through erasure (`leaver._RETAINED`), so not disposable on a clock",
}

# The expired threads: a thread is expired exactly when its newest checkpoint is older than the
# cutoff, and the thread is the unit of disposal.
#
# `GROUP BY thread_id ORDER BY thread_id` matches `checkpoints_pkey`, so with statistics the planner
# streams the index and the `LIMIT` stops it (see `_ANALYZE_THREADS`); `ORDER BY` also makes a
# capped pass deterministic. On a sparse backlog every thread is visited regardless; this is one
# streaming index pass.
#
# `thread_id > %s` resumes after the last thread the previous sweep of this pass reached, so a
# drain scans the table once rather than once per capped batch. The position deliberately dies with
# the pass: every pass starts at the beginning, so coverage is whole and nothing can starve.
#
# One over the cap is asked for only to learn whether a tail exists.
_EXPIRED_THREADS = (
    "SELECT thread_id FROM checkpoints "
    "WHERE thread_id > %s "
    "GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s) "
    "ORDER BY thread_id LIMIT %s"
)

# Gives the planner statistics so `_EXPIRED_THREADS` streams the primary key instead of a
# whole-table hash aggregate that spills to disk.
#
# `checkpoints` is created outside `infra/sql`, so no migration analyzes it and a fresh deployment
# has no statistics until autovacuum runs. `ANALYZE` samples, so it is cheap, and takes effect
# inside the sweep's own uncommitted transaction. Run once per pass, on the sweep that starts at the
# top of the table. A role that does not own the table gets a warning and a no-op, so no privilege
# guard is needed.
_ANALYZE_THREADS = "ANALYZE checkpoints"


# The two statements that keep the checkpoint sweep safe against a turn landing mid-sweep.
#
# The checkpointer's pool is autocommit, so a live turn commits while this transaction runs and the
# candidate list is stale by the time it is deleted. Losing a thread's blobs while its checkpoints
# survive reads back as an empty conversation, silently. So each statement re-asks in its own
# snapshot:
#
# 1. `_DELETE_EXPIRED_CHECKPOINTS` re-runs the expiry predicate on the candidates; a thread that
#    took a turn meanwhile survives to the next pass. Its `RETURNING` drives the next statement.
# 2. `_DELETE_ORPHANED` deletes blobs and writes only for threads with **no** `checkpoints` row
#    left, so a racing turn's committed row protects its blobs.
#
# `aput` writes blobs and checkpoint in one pipelined transaction, so no half-written pair is ever
# visible. `agent/checkpointer._refuse_if_values_are_missing` guards the read against torn threads
# from other producers (restores, hand surgery).

# The one table of the three that dates a thread, and therefore the one the expiry re-check runs
# against; the other two are swept only where it has left nothing behind.
_CHECKPOINTS = "checkpoints"

_DELETE_EXPIRED_CHECKPOINTS = (
    "DELETE FROM checkpoints WHERE thread_id = ANY(%s) AND thread_id IN ("
    "SELECT thread_id FROM checkpoints WHERE thread_id = ANY(%s) GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval(days => %s)"
    ") RETURNING thread_id"
)

# `{table}` is one of `CHECKPOINT_TABLES`, a constant, never caller input; thread ids are bound.
# `tests/test_retention.py` asserts the sweep covers exactly that tuple.
_DELETE_ORPHANED = (
    "DELETE FROM {table} WHERE thread_id = ANY(%s) "
    "AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.thread_id = {table}.thread_id)"
)

# Repairs blob/write rows whose thread has no `checkpoints` row, once per pass.
#
# Both deletes above are restricted to candidates selected out of `checkpoints`, so such rows are
# otherwise permanent and pin their session's ownership row through `_untouched_arms`. Deleting an
# unmatched row is safe because `aput` writes a thread's rows in one transaction; orphans come only
# from restores, hand surgery or partial deletes elsewhere. One batch per pass: orphans do not
# arrive continuously, and a full batch is reported as a tail. `ctid = ANY(ARRAY(... LIMIT))`
# because Postgres has no `LIMIT` on `DELETE`.
_REPAIR_ORPHANED = (
    "DELETE FROM {table} WHERE ctid = ANY(ARRAY("
    "SELECT o.ctid FROM {table} o WHERE NOT EXISTS ("
    "SELECT 1 FROM checkpoints c WHERE c.thread_id = o.thread_id) LIMIT %s))"
)

# Every table this register names, and its total size on disk.
#
# Resolved through `to_regclass`, so it follows the connection's `search_path` and an absent table
# (the checkpoint tables before the graph engine ran) is simply missing from the answer.
# `pg_total_relation_size` includes indexes and TOAST, which dominate the blob tables.
_TABLE_BYTES = (
    "SELECT t.name, pg_total_relation_size(to_regclass(quote_ident(t.name))) "
    "FROM unnest(%s::text[]) AS t(name) "
    "WHERE to_regclass(quote_ident(t.name)) IS NOT NULL"
)

# Vacuums the tables this pass deleted from.
#
# `SKIP_LOCKED`, so it never waits behind DDL or blocks a turn. Never `FULL`: that takes an
# `ACCESS EXCLUSIVE` lock and needs room for a second copy. A plain `VACUUM` makes freed space
# reusable, so the table stops growing; it returns bytes only from the end of the relation, while
# retention deletes the oldest rows at the front, so `bytes_reclaimed` is usually zero and a flat
# `chemclaw_table_bytes` is the signal. A role without vacuum rights gets a warning and a skip.
_VACUUM = "VACUUM (SKIP_LOCKED) {table}"

# The last pass's reading of every table's size, published as `chemclaw_table_bytes`.
#
# Not queried on a scrape, so `/metrics` never depends on the database. Not seeded, so a process
# that has never swept publishes no series — the absence `ChemclawRetentionNotSweeping` fires on.
_TABLE_SIZES: dict[str, float] = {}


def bind_table_size_gauges() -> None:
    """Publish `chemclaw_table_bytes` off the last pass's reading (no query on a scrape).

    Called at import, so the process that sweeps is the process that reports, with no startup hook
    to forget.
    """
    record_metric(lambda m: m.bind_gauge_family("chemclaw_table_bytes", lambda: _TABLE_SIZES))


bind_table_size_gauges()

# The per-session conversation prune's statements; only sessions with an expired row are visited.
#
# `LIMIT` bounds one activity's work: an unbounded first pass would time out, retry and exhaust its
# attempts having deleted nothing. A bounded batch always makes progress, and a tail is reported.
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
# The reachability set: erasure and `session_store.delete_session` both start from `session_owners`,
# so an ownership row removed while any of these holds a row puts that row beyond erasure. The
# ownership row goes last, and only when nothing here is left. `tests/test_retention.py` holds this
# against `session_store._session_delete_statements()`. `tool_result_links`, not the blobs, because
# a blob is content-addressed and may be shared. The checkpoint tables may be absent, and an absent
# table holds no rows.
_SESSION_SCOPED_ROWS: dict[str, str] = {
    "session_messages": "session_id",
    "session_exhibits": "session_id",
    "session_attachments": "session_id",
    "session_events": "session_id",
    "tool_result_links": "session_id",
    **dict.fromkeys(CHECKPOINT_TABLES, "thread_id"),
}


# Which other window must be set before an ownership row can become disposable, per session-scoped
# table, as the ENV name an operator would set.
#
# The dependency is invisible where it bites: e.g. `tool_result_links` empties only behind its blob,
# so a deployment that sets only a conversation window never disposes of a session that called a
# tool, while every pass reports clean.
_OWNERSHIP_DEPENDENCIES: dict[str, tuple[str, str] | None] = {
    # Never the advice (`session_owners`' window is this setting), but listed so the map stays the
    # same set as `_SESSION_SCOPED_ROWS`.
    "session_messages": ("session_messages", "CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS"),
    # `None`: what blocks here is the *unconsumed* events, which no window prunes, so there is no
    # knob to name. `docs/planning/BACKLOG.md` tracks it.
    "session_events": None,
    "tool_result_links": ("tool_result_blobs", "CHEMCLAW_RETENTION_TOOL_RESULTS_DAYS"),
    "session_exhibits": ("session_exhibits", "CHEMCLAW_RETENTION_SESSION_EXHIBITS_DAYS"),
    # The conversation's own window (`_window_days`), so like `session_messages` this entry can
    # never be the advice; recorded so the map stays the same set as `_SESSION_SCOPED_ROWS`.
    "session_attachments": ("session_attachments", "CHEMCLAW_RETENTION_SESSION_MESSAGES_DAYS"),
    **dict.fromkeys(CHECKPOINT_TABLES, ("checkpoints", "CHEMCLAW_RETENTION_CHECKPOINTS_DAYS")),
}


def unwindowed_ownership_dependencies(present: set[str]) -> list[str]:
    """The settings that must also be set before any session holding such a row can be forgotten.

    Answers "why is `session_owners` not shrinking?" as a list of ENV names. Reads the hand-written
    `_OWNERSHIP_DEPENDENCIES`; a test holds its key set equal to `_SESSION_SCOPED_ROWS`. A `None`
    entry means no window empties that table.

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

    One builder for the candidate query and the `DELETE`, so both ask the same question. The
    live-lease arm protects a resumed session whose turn has not written its transcript yet; an
    expired lease is a crash artifact and is swept with the row.

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


# The candidate ownership rows, capped and ordered by the primary key so the planner streams
# `session_owners_pkey` and the `LIMIT` stops it. An index on `created_at` does not change the plan
# (the cutoff is not selective; the anti-joins filter), so none is added.
_DISPOSABLE_SESSIONS = (
    "SELECT o.session_id FROM session_owners o "
    "WHERE o.created_at < now() - make_interval(days => %s){arms} "
    "ORDER BY o.session_id LIMIT %s"
)
# The disposal: the ownership row and, behind it, the lease nobody released.
#
# The predicate is re-asked here in a fresh snapshot, so a session that claimed a lease or wrote a
# row since the candidate query is no longer disposable. One statement, so a lease goes only if its
# ownership row went; both counts come back in one row.
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
    # What the pass cost the store: `bytes_on_disk` per table after the pass and `bytes_reclaimed`
    # returned to the filesystem. A zero reclaimed beside many deleted rows is normal (see
    # `_VACUUM`); `bytes_on_disk` going flat over passes is the signal.
    bytes_on_disk: dict[str, int] = {}
    bytes_reclaimed: dict[str, int] = {}
    skipped: list[str] = []
    sessions_deferred: int = 0
    threads_deferred: int = 0
    owners_deferred: int = 0
    # The same probe for the age-cutoff tables (`session_events`, `tool_result_blobs`,
    # `result_publications`): `1` means a batch came back full, so there is very likely more.
    rows_deferred: int = 0

    def has_tail(self) -> bool:
        """Whether a branch stopped at its cap with work left.

        Not on its own a reason to sweep again; see `made_progress`.
        """
        return bool(
            self.sessions_deferred
            or self.threads_deferred
            or self.owners_deferred
            or self.rows_deferred
        )

    def made_progress(self) -> bool:
        """Whether this sweep actually disposed of anything.

        The pass continues on progress, not on a tail: a capped selection can return the same
        sessions every sweep while every row in them is skipped (unreadable, or a pairing straddling
        the cutoff), so a tail alone would spin until the clock stopped it.
        """
        return any(count for count in self.deleted.values())


class _Budget:
    """The pass's wall-clock allowance, and whether it can afford one more unit of work.

    `retention_timeout_seconds` is the activity's start-to-close timeout, so the pass stops one unit
    short of it rather than being killed. The margin is the slowest unit this pass has already run,
    which adapts to the backlog. Sweeps and batches share it, so the batch loop reserves a sweep's
    worth once one is measured — erring early, which is safe because every unit commits its own
    work. The first unit is always allowed, so a pass never does nothing.
    """

    def __init__(self) -> None:
        """Start the clock at the activity's own budget."""
        self._deadline = monotonic() + settings.retention_timeout_seconds
        self._worst = 0.0

    @contextmanager
    def measuring(self) -> Iterator[None]:
        """Time the block and remember it if it is the slowest unit of work so far."""
        started = monotonic()
        try:
            yield
        finally:
            self._worst = max(self._worst, monotonic() - started)

    def affords_more(self) -> bool:
        """Whether another unit as slow as the slowest seen would still land inside the budget."""
        return monotonic() + self._worst <= self._deadline


def _window_days(table: str) -> int:
    """The configured retention window for `table`, in days. 0 disables pruning for that table."""
    return {
        "session_events": settings.retention_session_events_days,
        "session_messages": settings.retention_session_messages_days,
        "tool_result_blobs": settings.retention_tool_results_days,
        "result_publications": settings.retention_result_publications_days,
        "checkpoints": settings.retention_checkpoints_days,
        "session_exhibits": settings.retention_session_exhibits_days,
        # An upload is a turn's input and is kept as long as the conversation it came with — the
        # module docstring argues why a knob of its own could only agree with this one or be wrong.
        "session_attachments": settings.retention_session_messages_days,
        # The conversation's window, not a knob of its own: the guards, not the clock, decide when
        # an ownership row goes, so this is only a floor, and a session should not be forgotten
        # sooner than its conversation.
        "session_owners": settings.retention_session_messages_days,
    }[table]


@durable_activity("background")
@activity.defn
async def prune_expired_rows() -> RetentionOutcome:
    """Run one retention sweep, heartbeating so a dead worker is noticed in a minute not ten.

    The sweep has no meaningful progress boundary, so it is wrapped as an opaque call by
    `durable/heartbeat.py`, whose `finally` stops the work if the beat fails.
    """
    return await beating(
        _prune_expired_rows(),
        "retention sweep",
        settings.background_activity_heartbeat_timeout_seconds,
    )


async def _table_sizes(conn: AsyncConnection[TupleRow]) -> dict[str, int]:
    """`pg_total_relation_size` for every table this module's register names, by table.

    Every table, not just the prunable ones: the one filling the volume is often one nothing prunes.

    Args:
        conn: The pass's connection; tables resolve through its `search_path`.

    Returns:
        `{table: bytes}` for the tables that exist here; a table this deployment does not have is
        absent rather than zero.
    """
    async with conn.cursor() as cur:
        await cur.execute(_TABLE_BYTES, (sorted(set(_PRUNABLE) | set(_NOT_PRUNED)),))
        return {str(row[0]): int(row[1]) for row in await cur.fetchall()}


async def _vacuum_swept_tables(tables: list[str], budget: _Budget) -> list[str]:
    """`VACUUM (SKIP_LOCKED)` each table this pass deleted from. Returns the ones it skipped.

    A `DELETE` reclaims nothing, and the stock autovacuum threshold may never be crossed; the
    checkpoint tables cannot be tuned by migration either, so the sweep vacuums itself. On its own
    autocommit connection, because `VACUUM` cannot run in a transaction block; the flag is restored
    in `finally` since the pool does not reset it.

    Args:
        tables: The tables this pass deleted rows from, in the order it swept them.
        budget: The pass's clock. A table is vacuumed only while another unit as slow as the
            slowest one seen would still land inside the activity's own timeout.

    Returns:
        The tables the budget did not reach, so the pass can report them.
    """
    deferred: list[str] = []
    async with connection(settings.postgres_dsn, operation="retention_vacuum") as conn:
        await conn.commit()
        await conn.set_autocommit(True)
        try:
            for table in tables:
                if not budget.affords_more():
                    deferred.append(table)
                    continue
                with budget.measuring():
                    async with conn.cursor() as cur:
                        # `table` is a key of the closed register, never caller input; `VACUUM`
                        # takes no bound parameters.
                        await cur.execute(_VACUUM.format(table=table))
        finally:
            await conn.set_autocommit(False)
    return deferred


async def _sized_or_empty() -> dict[str, int]:
    """Read every register table's size, or report nothing if it cannot be read.

    Telemetry around a disposal job must never stop rows being deleted; a failure costs only the
    byte figures.
    """
    try:
        async with connection(settings.postgres_dsn, operation="retention_sizes") as conn:
            return await _table_sizes(conn)
    except Exception:
        logger.warning("retention: could not read table sizes; this pass reports rows only")
        return {}


async def _reclaim(outcome: RetentionOutcome, before: dict[str, int], budget: _Budget) -> None:
    """Vacuum what the pass deleted from, then record what the store holds and what it gave back.

    The vacuum runs before the second reading, so `bytes_reclaimed` includes it. A table that grew
    during the pass reports `0` reclaimed (concurrent writers); the growth shows in `bytes_on_disk`.
    Never raises: the disposal is the job.

    Args:
        outcome: The pass's totals, updated in place with `bytes_on_disk` and `bytes_reclaimed`.
        before: Sizes read at the start of the pass; empty when that read failed.
        budget: The pass's clock, so the vacuum stops short of the activity's own timeout.
    """
    swept = [table for table, rows in outcome.deleted.items() if rows]
    if swept:
        try:
            deferred = await _vacuum_swept_tables(swept, budget)
        except Exception:
            logger.exception("retention: the vacuum pass failed; the deletions above still stand")
            deferred = swept
        if deferred:
            # Reported, because a pass that ran out of clock before reclaiming looks the same in
            # bytes as one with nothing to reclaim.
            outcome.skipped.append(
                f"{', '.join(deferred)} (not vacuumed: the pass budget was spent)"
            )
    after = await _sized_or_empty()
    outcome.bytes_on_disk = after
    outcome.bytes_reclaimed = {
        table: max(0, size - after.get(table, size)) for table, size in before.items()
    }


def _publish_store_size(outcome: RetentionOutcome) -> None:
    """Republish `chemclaw_table_bytes` off this pass's reading, so disposal is visible outside it.

    One gauge family, no counters: row counts are in `RetentionOutcome`, and reclaimed bytes are
    structurally near zero, so neither could carry a useful alert (`core/metrics.py` has the
    argument). Republished on every pass, because the absence is what
    `ChemclawRetentionNotSweeping` fires on. Never raises.
    """
    _TABLE_SIZES.clear()
    _TABLE_SIZES.update({table: float(size) for table, size in outcome.bytes_on_disk.items()})


async def _prune_expired_rows() -> RetentionOutcome:
    """Sweep until the backlog is drained or the pass budget is spent, and report the total.

    The per-branch cap (`retention_max_sessions_per_pass`) bounds one transaction and one batch; any
    fixed cap can sit below the arrival rate, so the pass sweeps again while a branch reports a
    tail, progress was made and `_Budget` can afford another sweep. The cap bounds a batch and the
    clock bounds the pass. Every branch commits its own work, so stopping early loses nothing
    counted.
    """
    budget = _Budget()
    total = RetentionOutcome(deleted={}, skipped=[])
    # Sizes before the pass, so what it gave back is measured. Its own connection, and never allowed
    # to fail the pass.
    before = await _sized_or_empty()
    # Where this pass's checkpoint-thread scan has got to. It starts at the beginning of the table
    # on every pass and is not kept between them — `_EXPIRED_THREADS` carries why.
    resume_threads_from = ""
    while True:
        with budget.measuring():
            outcome, resume_threads_from = await _sweep_once(budget, resume_threads_from)
        for table, count in outcome.deleted.items():
            total.deleted[table] = total.deleted.get(table, 0) + count
        # The skips are a property of the configuration, not of the sweep, so the last pass's list
        # is the whole truth and repeating it per iteration would just multiply it.
        total.skipped = outcome.skipped
        total.sessions_deferred = outcome.sessions_deferred
        total.threads_deferred = outcome.threads_deferred
        total.owners_deferred = outcome.owners_deferred
        total.rows_deferred = outcome.rows_deferred
        # Progress *and* a tail *and* budget. Dropping the first is what let a sweep that removed
        # nothing be asked to run again for as long as the clock allowed.
        if not outcome.made_progress() or not outcome.has_tail() or not budget.affords_more():
            break
    await _reclaim(total, before, budget)
    _publish_store_size(total)
    return total


async def _sweep_once(
    budget: _Budget, resume_threads_from: str = ""
) -> tuple[RetentionOutcome, str]:
    """Delete rows past their table's retention window; return the per-table counts.

    `resume_threads_from` is where the previous sweep of this pass left the checkpoint-thread scan;
    the second element of the return is where this one left it.

    Each table is pruned and committed on its own (the three checkpoint tables together, as one
    thread's state), so one failure cannot roll back another table's deletions and each transaction
    holds one table's locks. A failing table is logged and rolled back — an aborted transaction
    would fail every later statement — and the sweep continues to the rest; the first exception is
    re-raised at the end so Temporal still retries. The cutoff is computed in SQL so app and
    database clocks cannot disagree.
    """
    outcome = RetentionOutcome(deleted={}, skipped=[])
    first_error: BaseException | None = None
    async with connection(settings.postgres_dsn) as conn:
        # No budget check here: a started sweep finishes its tables (each branch is capped). The
        # budget decides only whether to sweep again; checking it here too let a tiny budget do
        # nothing.
        for table, (column, disposable) in _PRUNABLE.items():
            days = _window_days(table)
            if days <= 0:
                outcome.skipped.append(f"{table} (retention disabled)")
                continue
            try:
                if table == "session_messages":
                    # Per session, through the pairing closure, since a row's disposability depends
                    # on rows that may not be expiring. Commits per session, so no `commit()`
                    # follows.
                    deleted, deferred = await _prune_session_messages(conn, days)
                    outcome.deleted[table] = deleted
                    outcome.sessions_deferred = deferred
                    continue
                if table == "session_owners":
                    # Last in `_PRUNABLE`, and only when nothing holds a row for the session.
                    # Commits itself.
                    owners, deferred = await _prune_session_owners(conn, days)
                    outcome.deleted.update(owners)
                    outcome.owners_deferred = deferred
                    continue
                if table == "checkpoints":
                    # Three tables, one thread, one transaction; reported per table. Commits itself.
                    counts, skipped, deferred, resume_threads_from = await _prune_checkpoints(
                        conn, days, resume_threads_from
                    )
                    outcome.deleted.update(counts)
                    outcome.skipped.extend(skipped)
                    outcome.threads_deferred = deferred
                    continue
                deleted, more = await _prune_by_age(conn, table, column, disposable, days, budget)
                outcome.deleted[table] = outcome.deleted.get(table, 0) + deleted
                if more:
                    outcome.rows_deferred = 1
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
    return outcome, resume_threads_from


async def _prune_by_age(
    conn: AsyncConnection[TupleRow],
    table: str,
    column: str,
    disposable: str,
    window: int,
    budget: _Budget,
    *,
    unit: Literal["days", "hours"] = "days",
) -> tuple[int, bool]:
    """Delete `table`'s expired rows in committed batches. Returns `(deleted, more may remain)`.

    Batched because one unbounded `DELETE` over a large backlog exceeds `statement_timeout`, is
    retried, and never deletes anything; batches make progress and keep it. `ctid = ANY(ARRAY(SELECT
    … LIMIT))` because Postgres has no `LIMIT` on `DELETE`; the inner select uses the predicate's
    index. The batch size, `retention_delete_batch_rows`, is read once per call and is a setting
    because the right size depends on the deployment's row size.

    Args:
        conn: The sweep's connection; each batch commits on it before the next is issued.
        table: A key of `_PRUNABLE` — never a caller's string, which is what makes the
            interpolation below safe. The bound value is the window.
        column: That table's dating column or expression, from the same map.
        disposable: The extra predicate deciding whether a row of this table may go at all.
        window: The retention window, in `unit`; rows older than it are candidates.
        budget: The pass's clock. Batches stop when another one as slow as the slowest so far
            would not land inside it.
        unit: What `window` counts — days for every table window, hours for the artefact pushes.

    Returns:
        `(rows deleted, whether a full batch came back)`: a full batch means "very likely more".
    """
    # Read once, not per batch: a `.env` reload mid-pass would otherwise change what "a full batch"
    # means between the `LIMIT` and the comparison below, and a shrunk limit would read as drained.
    batch_size = settings.retention_delete_batch_rows
    deleted = 0
    while True:
        async with conn.cursor() as cur:
            # Table and column come from the closed `_PRUNABLE` map, never a caller; the value is
            # bound.
            with budget.measuring():
                await cur.execute(
                    f"DELETE FROM {table} WHERE ctid = ANY(ARRAY("
                    f"SELECT ctid FROM {table} "
                    f"WHERE {disposable} AND {column} < now() - make_interval({unit} => %s) "
                    f"LIMIT %s))",
                    (window, batch_size),
                )
                await conn.commit()
            batch = cur.rowcount
        deleted += batch
        if batch < batch_size:
            return deleted, False
        if not budget.affords_more():
            return deleted, True


async def _prune_session_messages(conn: AsyncConnection[TupleRow], days: int) -> tuple[int, int]:
    """Delete expired conversation rows, never splitting a tool-call pairing.

    Returns `(rows deleted, 1 if expired sessions remain beyond this pass's cap else 0)`.

    Per session, because whether an expired row may go depends on its paired rows, which may be
    newer than the cutoff — so it reads the session's whole history, one conversation in memory at a
    time. Each session commits before the next is read, keeping progress and bounding lock duration.
    The batch is capped and a tail reported.
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
            # Call ids only, read without deserialising: a session's rows may be in either stored
            # message shape.
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

    The row is what makes a session reopenable, so age alone does not decide: it goes when the
    session is past the window, nothing in `_SESSION_SCOPED_ROWS` holds a row for it, and no live
    lease names it. Every session-scoped sweep (including erasure) starts from this table, so this
    runs last and never ahead of the rows it keys. The lease goes only with its ownership row; an
    unexpired lease is a running turn and is never collected on its own. Capped and reported.
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
    conn: AsyncConnection[TupleRow], days: int, resume_from: str = ""
) -> tuple[dict[str, int], list[str], int, str]:
    """Delete every trace of threads whose newest checkpoint has expired, after `resume_from`.

    Returns `(rows deleted per table, tables skipped with the reason, 1 if a tail remains else 0,
    where the next sweep of this pass should start)`. The last element is `""` when the scan reached
    the end of the table, which is also what a caller with no pass to resume passes in.

    On the first sweep of a pass it analyzes `checkpoints` (`_ANALYZE_THREADS`) and runs
    `_REPAIR_ORPHANED`, whose rows count into the per-table totals. The cap is reported as a probe.

    All three tables go in one transaction: they are one thread's state with no foreign key, and
    separate commits could leave checkpoints without blobs or blobs no later pass can find. Races
    with a live turn are handled by `_DELETE_EXPIRED_CHECKPOINTS` and `_DELETE_ORPHANED`.

    A malformed `ts` raises and fails the pass loudly rather than letting the job report success
    while the table grows; tables earlier in `_PRUNABLE` have already committed. A missing `ts` is
    `NULL` and never expires. Skipped, not failed, when the tables do not exist (never ran the graph
    engine); checked up front because Postgres resolves the relation at parse time.
    """
    repaired: dict[str, int] = {}
    async with conn.cursor() as cur:
        present = await existing_tables(cur, CHECKPOINT_TABLES)
        missing = sorted(set(CHECKPOINT_TABLES) - present)
        if missing:
            # All or nothing: the tables are created together by one `setup()`, so a partial set is
            # a schema nobody has, and guessing which half to prune would be inventing a case.
            return {}, [f"{', '.join(missing)} (no checkpointer in this schema)"], 0, ""
        # Before the question, not after: `_EXPIRED_THREADS` only plans as a `LIMIT`-terminated
        # index scan when the planner has statistics for a table no migration can give them to.
        if not resume_from:
            # Once per pass, at its first sweep: later sweeps resume inside the same plan.
            # Statistics staled by this pass's deletions only overstate the table, which keeps the
            # conservative plan.
            await cur.execute(_ANALYZE_THREADS)
            # Once per pass too: a thread with no `checkpoints` row is invisible to every statement
            # below.
            for table in (name for name in CHECKPOINT_TABLES if name != _CHECKPOINTS):
                await cur.execute(
                    _REPAIR_ORPHANED.format(table=table),
                    (settings.retention_delete_batch_rows,),
                )
                repaired[table] = max(cur.rowcount, 0)
        cap = settings.retention_max_sessions_per_pass
        await cur.execute(_EXPIRED_THREADS, (resume_from, days, cap + 1))
        found = [str(row[0]) for row in await cur.fetchall()]
        deferred = max(len(found) - cap, 0)
        threads = found[:cap]
        if not threads:
            # The repair still counts: a pass with no expired thread is exactly the steady state,
            # and an orphaned row is disposal this pass really did.
            return {**dict.fromkeys(CHECKPOINT_TABLES, 0), **repaired}, [], 0, ""
        # Both statements re-ask their question inside this transaction;
        # `_DELETE_EXPIRED_CHECKPOINTS` carries the measurement and what the pair does not close.
        await cur.execute(_DELETE_EXPIRED_CHECKPOINTS, (threads, threads, days))
        checkpoint_rows = await cur.fetchall()
        swept = sorted({str(row[0]) for row in checkpoint_rows})
        # By name, not position: the two statements differ (expiry re-check vs orphan guard), so
        # indexing `CHECKPOINT_TABLES` would misassign them if it were reordered.
        deleted: dict[str, int] = {_CHECKPOINTS: len(checkpoint_rows)}
        for table in (name for name in CHECKPOINT_TABLES if name != _CHECKPOINTS):
            await cur.execute(_DELETE_ORPHANED.format(table=table), (swept,))
            # Plus whatever the repair above took for a thread that has no `checkpoints` row at
            # all, so the reported count is what the table actually lost this pass.
            deleted[table] = max(cur.rowcount, 0) + repaired.get(table, 0)
    await conn.commit()
    logger.info(
        "pruned %d of %d expired checkpoint thread(s)%s; %s",
        len(swept),
        len(threads),
        "" if len(swept) == len(threads) else " (the rest took a turn mid-sweep and stay)",
        "more remain for the next pass" if deferred else "the backlog is drained",
    )
    # The last thread this scan reached, and only when the cap cut it short: a scan that came back
    # under its cap has seen the table to its end, so the next sweep starts at the beginning.
    return deleted, [], deferred, threads[-1] if deferred else ""


@durable_workflow("background")
@workflow.defn
class RetentionWorkflow:
    """Enforce the deployment's retention windows on a cadence."""

    @workflow.run
    async def run(self) -> RetentionOutcome:
        """Run one retention pass and return what it removed."""
        return await workflow.execute_activity(
            prune_expired_rows,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Required for the activity's heartbeats to detect a dead worker; the beat interval is
            # derived from this same value (`durable/heartbeat.py::beating`).
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )


@durable_activity("background")
@activity.defn
async def prune_exhibit_pushes() -> int:
    """Delete `exhibit` push rows older than `exhibit_push_retention_hours`; return how many went.

    Its own job rather than a branch of the retention sweep, because the sweep is scheduled only
    when a deployment states a window, and the push window must apply regardless. A push is a
    notification over the artefact list, so it goes on age alone, consumed or not. Batched and
    budgeted like every age cutoff; what one run leaves, the next takes.
    """
    async with connection(settings.postgres_dsn) as conn:
        deleted, more = await _prune_by_age(
            conn,
            "session_events",
            "created_at",
            _EXHIBIT_PUSHES,
            settings.exhibit_push_retention_hours,
            _Budget(),
            unit="hours",
        )
    log_event(
        logger,
        "retention.exhibit_pushes",
        "pruned %d artefact push rows older than %d hours%s",
        deleted,
        settings.exhibit_push_retention_hours,
        "; more remain for the next run" if more else "",
        deleted=deleted,
    )
    return deleted


# Parks rather than fails on a workflow-code bug, like `RetentionWorkflow`: Schedule-only,
# idempotent, and nothing reads the run.
@durable_workflow("background")
@workflow.defn
class ExhibitPushPruneWorkflow:
    """Expire artefact push notifications on their own window (`prune_exhibit_pushes`)."""

    @workflow.run
    async def run(self) -> int:
        """Run one prune of expired artefact pushes and return how many rows went."""
        return await workflow.execute_activity(
            prune_exhibit_pushes,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
