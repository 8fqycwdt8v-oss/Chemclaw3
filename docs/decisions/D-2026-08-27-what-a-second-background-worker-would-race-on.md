# D-2026-08-27-what-a-second-background-worker-would-race-on — the `background-jobs` queue audited under two workers

## Status

Accepted. Closes the `BACKLOG` row left open by
`D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed`, which asked for an audit of the other
activities that queue owns before `workers.background.replicas` may exceed 1.

## Context

`workers.background.replicas` has been pinned at 1 since the chart existed, and the comment holding
it there cited the PR-gate's host-local checkout `flock` (D-069). That reason is closed:
`D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed` serializes submissions to one remote
across pods on a Postgres session-level advisory lock, leaving the `flock` to guard only this pod's
own worktree sweep. The `BACKLOG` row that survived it asked for the rest of the audit, naming two
suspects: "ELN sync's cursor advance and retention's prune batches were written under a one-worker
assumption and nobody has argued they are safe under two."

A suspicion is not a finding. This is the audit, taken over what the queue *actually* owns — the
registry's 20 workflows and 36 activities, read from `registered_workflows("background")` rather
than from the row's two examples.

## The premise both suspects rest on is false

**One replica was never a serialization guarantee.** The background worker is constructed with
`max_concurrent_activities=settings.worker_max_concurrent_activities` (default 8) and Temporal's
default workflow-task concurrency, so a single process already runs eight activities and many
workflow runs at once. Anything two *threads of execution* can do to a shared Postgres row, one
worker can already do. What a single replica buys is exclusion over state that lives **in the pod** —
a checkout, a temp dir, a module-level dict — and nothing else.

**And the periodic jobs are serialized by the server, not by the worker.** Every Schedule
`durable/schedules.py` applies is built with `SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP)`.
SKIP is enforced by the Temporal service against the schedule, so however many workers poll the
queue, a fire is dropped while the previous run is still going. Worker count is irrelevant to it.

Those two facts decide most of the inventory before any individual job is read.

## Inventory and verdict

| What the queue owns | How concurrency is bounded | Verdict at replicas > 1 |
| --- | --- | --- |
| `ElnSyncWorkflow` + `plan_eln_sync`, `sync_eln_entries`, `load_sync_cursor`, `store_sync_cursor` | One Schedule under SKIP; the workflow awaits one activity at a time; the only other starter (`cli.live_data.backfill`) passes an explicit `since` and never touches `sync_cursors` | **safe** — see the measurement below |
| `RetentionWorkflow` + `prune_expired_rows` | SKIP; every deletion re-checks its own predicate *inside* the `DELETE` under READ COMMITTED (`_DELETE_SESSIONS`), batches are `LIMIT`-capped, commits are per table and per session | **safe** |
| `NoteReindexWorkflow` + `reindex_notes_activity` | SKIP for the scheduled fire, a per-minute workflow id for the merge-webhook fire — **but the corpus it prunes against is per pod** | **races** — the blocker |
| `PublishResultsWorkflow` + `drain_result_publications`, `publish_job_result` | `SELECT … FOR UPDATE SKIP LOCKED` claim with attempt accounting; enqueue is `ON CONFLICT DO NOTHING` | safe — written for concurrency |
| `DigestWorkflow` + `collect_digests`, `acknowledge_digest` | SKIP; the watermark advance is one `UPDATE … CASE` statement, written because "two digest runs overlapping would otherwise each read the same list" | safe |
| `DocumentShareSyncWorkflow`, `ReactionCorpusWorkflow`, `ReactionLabelWorkflow` (+ their plan/drain/prune activities) | SKIP; no cross-run cursor — the crawl cursor rides the run state, and the stale set *is* the cursor. Writes are upserts | safe |
| `ObservationSynthesisWorkflow`/`ObservationPromotionWorkflow` + the three mining activities | SKIP; upsert by content-derived id, and retirement is windowed in days | safe (see the window rule below) |
| `ArtifactEvictionWorkflow` + `evict_cold_artifacts` | SKIP; Postgres `DELETE` over a regenerable cache | safe. Two overlapping runs would each measure the same overshoot and each evict it — over-eviction of a cache, and SKIP means it does not arise |
| `EvalDriftWorkflow` + `check_eval_drift` | SKIP; reads the case set and baseline from the image, writes nothing durable | safe |
| `PublishNoteWorkflow`, `CampaignSynthesisWorkflow`, `PlaybookDistillationWorkflow`, `OptimizationCampaignWorkflow`, `DevelopmentReportWorkflow`/`ReportSectionWorkflow` + their activities | No Schedule at all (D-2026-08-25); started on demand under a deterministic workflow id with `ALLOW_DUPLICATE_FAILED_ONLY`, so a duplicate request rejoins. Their one shared-resource write is the PR-gate, now cluster-locked | safe |
| `ConnectorJobWorkflow`, `TemplateWorkflow` + `authorize_job_step`, `run_tool_step`, `run_agent_step`, `record_job`, `resolve_fan_out_limit`, `resolve_notes_per_run`, `record_session_event_activity` | Many run concurrently *today*, by design, keyed by content-derived workflow ids; all state is Temporal's or Postgres's | safe |

Nothing on this queue writes to the local filesystem — `grep` for `write_text`/`mkdir`/`open(` over
`durable/` returns nothing. The only two local-state dependencies reachable from it are the PR-gate
checkout (closed) and the knowledge checkout the reindex reads.

## The two suspects, measured

**The ELN cursor.** `store_cursor`'s upsert is `DO UPDATE SET cursor = EXCLUDED.cursor` — blind,
not high-water. `tests/test_cursor.py` now puts two overlapping transactions on real Postgres
behind it: the row lock *does* serialize them (the lagging upsert is asserted still blocked while
the leader holds the row), and the lagging value then wins. So the cursor is not monotonic.

That is a lost update, and it is not a skip, which is the distinction that decides whether a lock is
warranted. Every value ever stored is a mark somebody had ingested through, so a lost update moves
the cursor *backwards*, into territory that has already been read. The second test drives the
interleaving end to end — drain A ingests the whole corpus and stores its mark; drain B, which
loaded the same older mark, stores a chunk mark behind it and stops — and asserts the invariant:
the next drain re-ingests `e3…e6` and the union of what the three drains ingested is the whole
corpus. **Re-ingestion against an idempotent ingest, never an entry nobody read.**

And it does not arise anyway: one Schedule under SKIP, one sequential workflow, one writer. The
module docstring said this needed no locking and gave a reason about the *reader* (idempotent
re-fetch). It now gives the reason that is actually load-bearing, and the tests pin the write
behaviour so a later high-water spelling would be a decision rather than a drift.

**Retention's prune batches.** The concern does not survive reading the statements. Each table is
deleted and committed on its own; `session_messages` is pruned per session through the pairing
closure and committed per session (D-2026-08-05); `_DELETE_SESSIONS` repeats the whole `NOT EXISTS`
chain inside the `DELETE` precisely so that "a session that claimed a turn lease between the two
statements is no longer disposable at the moment of deletion". A second sweep can only recompute
the same candidates and delete zero of the rows the first already removed. The ordering invariant —
an ownership row goes only behind everything it keys — is enforced by the predicate *inside* the
delete, not by the phase order of one sweep, so two sweeps cannot break it either.

## The finding the row did not name

`NoteReindexWorkflow` is the one job on this queue that is unsafe at replicas > 1, and it is unsafe
for the reason the whole audit turns on: it derives a **shared** store from a **per-pod** corpus.

`retrieval/vector_index.py::reindex_notes` calls `index.retire_absent({note.id for note in notes})`
— it deletes every `note_index` row whose note is not on *this pod's* disk. That disk is
`chemclaw.noteRepoVolume`, an `emptyDir`, refreshed by the pod's own sidecar every
`knowledge.sync.intervalSeconds` (300 s by default, and unbounded if one sidecar wedges).

The interleaving, concretely: note `N` is merged. Pod A's sidecar fetches it; pod B's has not yet. A
reindex run lands on A and indexes `N`. The next run — the schedule's next fire, or the merge route's
`request_note_reindex` — lands on B, where `N` is absent, and retires it. Hybrid retrieval's dense
and lexical legs lose `N` until a run lands on A again, and with two pods that alternates. Nothing
errors: the run logs `retired 1 note(s) no longer on disk`, a sentence about a note that exists. The
fingerprint half flaps the same way — a pod holding an older copy of a note sees a mismatched
fingerprint and re-embeds the stale text, so the index can be written backwards at the cost of an
embedding call.

The existing guards do not cover this. They refuse to prune when the scan finds *nothing* (a
mis-mounted volume); they say nothing about a scan that is merely behind.

**The general rule this yields**, and the reason the observation miners are safe where the reindex is
not: *a corpus-derived store that prunes against a window (`retire_stale`, days) tolerates a lagging
replica, because the window dwarfs the sidecar's lag. One that prunes on the spot does not.*

## Decision

1. **`workers.background.replicas` stays at 1** — and the chart comment now says why for a reason
   that is currently true. Citing the closed checkout lock made the pin look like a leftover; the
   live blocker is the reindex prune.
2. **No lock is added.** Neither suspect races, and an advisory lock on the ELN cursor or the
   retention sweep would be a bottleneck on the background queue plus a standing claim that
   something was unsafe. The measurement is the deliverable, not a guard.
3. **The claim is written where a reader meets it**: `ingest/eln/cursor.py`'s docstring gives the
   real reason it needs no locking, `durable/background_worker.py` states the queue's concurrency
   contract, and `tests/test_cursor.py` holds the two measurements.
4. **What raising the replica count needs** is one change, outside this ADR's scope: the reindex's
   prune must key on something cluster-wide — the merged commit the index was built from, so a pod
   whose checkout predates it declines to prune — or the reindex must be pinned to one pod. Until
   then the number stays 1.

## Consequences

- The `BACKLOG` row's two named suspects are closed with evidence rather than with a lock, and its
  premise ("written under a one-worker assumption") is corrected: one worker was never single.
- `chemclaw_ingest_cursor_lag_seconds` is per pod, since `_OBSERVED` is a module-level dict fed only
  by the process that ran a sync. At one replica this is invisible; at two, an alert must aggregate
  (`min by (source)`) or it will read a pod that simply did not run the last sync as a stalled sync.
  Recorded beside the dict.
- The reindex defect is real at **replicas: 1** too, in a milder form: a merge-webhook reindex fires
  against a checkout up to `intervalSeconds` stale, so a just-merged note is missed until the next
  run. It cannot *retire* anything there, because one pod's clone only ever moves forward.
