"""Settings for the memory layers: synthesis, observations, retention and attachments.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class MemorySettings(BaseSettings):
    """The memory layers: playbook and campaign synthesis, and the stores they write.

    The similarity thresholds define what the semantic and episodic layers may claim ("same
    transformation" vs "related chemistry").
    """

    # DRFP similarity floor for distilling a playbook (reactions must also recur across >=2
    # projects); above the search floor because a playbook claims "same transformation".
    playbook_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # DRFP similarity for grouping an optimization campaign (the same transformation re-run);
    # tighter than the playbook floor so distinct transformations are not merged.
    optimization_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Memory one block of `memory.similarity`'s pairwise product may hold. The clustering is O(n^2)
    # in comparisons and the sparse product can exceed a dense matrix, so blocking keeps peak memory
    # at this plus O(n). Changes no result; raise it to trade memory for time.
    memory_similarity_block_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    memory_job_timeout_seconds: float = Field(default=300.0, gt=0)
    # Most notes one synthesis run may write (0 = unbounded). The window rotates by run date
    # (`_slice_for_this_run`), so the cap does not starve the corpus tail.
    memory_max_notes_per_run: int = Field(default=25, ge=0)
    # Most reactions one `read_corpus` holds in memory (0 = unbounded); the miners are whole-corpus
    # and cost ~40 kB per reaction. Hitting it marks the read incomplete: the note carries
    # `memory.jobs.PARTIAL_READ_CAVEAT` and the retirement pass is skipped. Lower it to fit the pod;
    # streaming miners are in `docs/planning/BACKLOG.md`.
    memory_corpus_max_reactions: int = Field(default=100_000, ge=0)
    # Backfilled notes per commit (`cli/backfill_corpus` only); one commit and push per note is what
    # bounds a backfill. Measured against a real remote: 327 ms/note unbatched, ~8.5 at 50, where
    # the curve flattens. The conversational path is not batched: a queued note is one a chemist
    # cannot read yet.
    backfill_commit_batch_size: int = Field(default=50, ge=2)
    # The observations tier: cross-project patterns no single run supports, opted into per
    # deployment (`memory/observations.py`). `promote_min_*` (evidence count and project count, both
    # required) promote an observation into a playbook note; `retire_after_days` closes one nothing
    # re-observes.
    observations_enabled: bool = False
    observation_promote_min_evidence: int = Field(default=3, ge=1)
    observation_promote_min_projects: int = Field(default=2, ge=1)
    observation_retire_after_days: int = Field(default=30, ge=0)
    observation_max_results: int = Field(default=10, ge=1)
    # Cadence of the observation lifecycle job (mine, then retire); daily since it rescans the
    # corpus. Promotion writes notes nobody asked for, so it runs on demand only.
    observation_schedule_minutes: float = Field(default=1440.0, gt=0)
    # Fraction of a Schedule's interval used as a deterministic per-job phase offset, so equal
    # cadences do not fire together. 0 disables.
    schedule_jitter_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    # Retention windows in days; 0 disables pruning for that table, the default, because retention
    # is a policy a deployment must state. `audit_events`, `calculation_results` and `job_records`
    # are absent; durable/retention.py says why each needs its own design.
    retention_enabled: bool = False
    retention_schedule_minutes: float = Field(default=1440.0, gt=0)
    retention_timeout_seconds: float = Field(default=600.0, gt=0)
    retention_session_events_days: int = Field(default=0, ge=0)
    retention_session_messages_days: int = Field(default=0, ge=0)
    # Artefacts (`session_exhibits`, revisions cascading), dated by last revision; their own window
    # because they may be kept longer than the chat. While 0, the ownership sweep also keeps the
    # session (`durable/retention._OWNERSHIP_DEPENDENCIES`).
    retention_session_exhibits_days: int = Field(default=0, ge=0)
    # Hours an `exhibit` push row on `session_events` is kept, consumed or not: it is a notification
    # and the list route is the source of truth. Never unbounded.
    exhibit_push_retention_hours: int = Field(default=24, ge=1)
    # Stored tool results (`api/tool_results.py`), the highest-volume table swept. 0 like every
    # window, so it grows until an operator sets it (`infra/sql/README.md` says so).
    retention_tool_results_days: int = Field(default=0, ge=0)
    # Retention of delivered result publications only; pending or failed rows are the record of what
    # has not reached its store.
    retention_result_publications_days: int = Field(default=0, ge=0)
    # LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`), created
    # by `AsyncPostgresSaver.setup()` rather than a migration. Disposes of a whole thread once its
    # newest checkpoint is older than the cutoff; in-thread pruning is
    # `checkpoint_retain_per_thread`.
    retention_checkpoints_days: int = Field(default=0, ge=0)
    # Checkpoints per `(thread_id, checkpoint_ns)` kept after each turn's prune. Not a retention
    # window: every superstep rewrites `messages`, so older checkpoints are superseded copies and a
    # thread grows quadratically without this. 3 leaves a margin over a partly written superstep. 0
    # disables it, for LangGraph time-travel, which nothing in `src/` uses.
    checkpoint_retain_per_thread: int = Field(default=3, ge=0)
    # Expired sessions one conversation-prune batch handles; each costs three round trips because
    # whether a row may go depends on rows that are not expiring. Bounds one transaction;
    # `_prune_expired_rows` repeats batches while `retention_timeout_seconds` allows.
    retention_max_sessions_per_pass: int = Field(default=500, gt=0)
    # Rows one age-cutoff `DELETE` removes before committing (`session_events`, `tool_result_blobs`,
    # `result_publications`). Too large and the statement hits `pg_statement_timeout_seconds` having
    # deleted nothing, every attempt; too small multiplies round trips. Sized against this schema's
    # worst row; a knob because row size and timeout are deployment facts.
    retention_delete_batch_rows: int = Field(default=10_000, gt=0)
    # Mid-turn durable-job resume: a turn that launches a job waits this long for its result and
    # continues with it. Off by default since a held turn holds an admission permit; must stay below
    # `service_turn_timeout_seconds`.
    mid_turn_resume_enabled: bool = False
    mid_turn_resume_timeout_seconds: float = Field(default=60.0, gt=0)
    # Predicted-vs-actual calibration ledger; needs the `predictions` table, so off by default.
    # `calibration_min_observations` is the floor below which figures are reported as not
    # meaningful.
    calibration_enabled: bool = False
    calibration_min_observations: int = Field(default=8, ge=1)
    # Ceiling on `find_calculations` results; the store is never evicted, and the tool clamps its
    # `limit` to this.
    calc_find_max_results: int = Field(default=50, ge=1)
    # Characters of one artifact `fetch_artifact` puts into context: small by-products fit, a large
    # Hessian does not.
    calc_artifact_max_chars: int = Field(default=20_000, ge=1)
    # Characters of one stored payload `find_calculations` renders in a listing (fetching that
    # calculation directly is unbounded). A larger payload is reported as `result_omitted`.
    calc_find_max_result_chars: int = Field(default=4_000, ge=1)
    # Ceiling on one `calculator_outliers` page, sized to be read.
    calc_outliers_max_results: int = Field(default=25, ge=1)
    # Standing-query digests. On by default so a `watch_for` subscription is actually evaluated;
    # with no subscriptions the daily workflow is one indexed read (`digest._match_corpus` returns
    # early). `GET /digests` and the UI's `/review` card read them. When off, `watch_for` says so.
    digest_enabled: bool = True
    digest_schedule_minutes: float = Field(default=1440.0, gt=0)
    digest_timeout_seconds: float = Field(default=300.0, gt=0)
    # Uploaded working files, bounded per upload and per session. Stored in `session_attachments`
    # (readable from every replica, on the conversation's retention) where sessions are durable,
    # otherwise in pod memory.
    attachment_max_bytes: int = Field(default=2_000_000, gt=0)
    attachment_max_per_session: int = Field(default=10, ge=1)
    # Per-session attachment byte bound in both stores (oldest uploads dropped,
    # `agent/attachments._uploads_to_drop`). The in-memory store also uses it as the cross-session
    # budget, evicting least-recently-used sessions' attachments rather than OOM-killing the pod;
    # raise it with the pod's memory limit. Bytes as resident (`sys.getsizeof`), not characters.
    # Smaller than `document_max_expanded_bytes`; a single larger file drops that session's older
    # ones first.
    attachment_store_max_bytes: int = Field(default=64_000_000, gt=0)
    # Parsing an upload is CPU-bound work over untrusted bytes, so it runs off the event loop
    # (`chemclaw.agent.attachments.parse_attachment_off_loop`) under a small concurrency cap that
    # keeps the default thread pool (also used for token validation) free. Past the cap an upload
    # waits `attachment_parse_queue_seconds`, then is shed with 503. The timeout kills the parse: it
    # runs in a `forkserver` child (`chemclaw.ingest.documents.isolate`), so the slot always comes
    # back.
    attachment_parse_timeout_seconds: float = Field(default=30.0, gt=0)
    attachment_parse_queue_seconds: float = Field(default=10.0, ge=0)
    attachment_max_concurrent_parses: int = Field(default=2, ge=1)
    # How long the caller waits beyond the parse deadline before abandoning its worker thread;
    # covers the forkserver's first start, which precedes the child's clock.
    attachment_parse_reap_grace_seconds: float = Field(default=5.0, ge=0)
