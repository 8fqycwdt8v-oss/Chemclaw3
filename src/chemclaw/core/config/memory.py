"""The memory layers (plan Phase 5): playbook and campaign synthesis.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class MemorySettings(BaseSettings):
    """The memory layers (plan Phase 5): playbook and campaign synthesis.

    Grouped because these thresholds define what the semantic/episodic layers may claim ("same
    transformation" vs "related chemistry"), plus the synthesis jobs' timeout and Schedule
    cadence.
    """

    # The semantic layer distils a playbook only from reactions whose DRFP similarity clears
    # this floor and that recur across >=2 projects — higher than the search floor, since a
    # playbook claims "same transformation", not just "related".
    playbook_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # The episodic layer groups an *optimization campaign* — repeated runs of the **same
    # transformation** (a screen varying conditions/reagents) — by DRFP similarity. Higher than
    # the playbook floor: an optimization series is the same reaction re-run, not merely related
    # chemistry, so the grouping must be tight to avoid merging distinct transformations.
    optimization_similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    memory_job_timeout_seconds: float = Field(default=300.0, gt=0)
    # Most notes one synthesis run may propose (0 = unbounded). The three jobs rescan the whole
    # corpus daily with no cursor, so a large import would open a PR per cluster on the first
    # night. The window rotates by run date rather than truncating, so the cap bounds the flood
    # without the tail of the corpus being proposed *never* — see `_slice_for_this_run`.
    memory_max_notes_per_run: int = Field(default=25, ge=0)
    # The ungated observations tier (D-161). Off by default and deliberately: it is the first
    # knowledge surface no human signs off before the agent can read it, and a deployment must
    # choose that rather than inherit it. `promote_min_*` are the two thresholds at which an
    # observation earns a human's review as a playbook PR — evidence count says the finding is not
    # a coincidence, project count says it is not one team's local habit, and neither alone does.
    # `retire_after_days` is how long an observation nothing re-observes stays open; without it the
    # tier only ever grows and becomes a write-only log.
    observations_enabled: bool = False
    observation_promote_min_evidence: int = Field(default=3, ge=1)
    observation_promote_min_projects: int = Field(default=2, ge=1)
    observation_retire_after_days: int = Field(default=30, ge=0)
    observation_max_results: int = Field(default=10, ge=1)
    # Cadence for the observation lifecycle job (mine, then retire). Daily, because it re-scans
    # the whole corpus. Promotion is not on this timer — it opens pull requests, so it is started
    # on demand (D-2026-08-25).
    observation_schedule_minutes: float = Field(default=1440.0, gt=0)
    # Fraction of a Schedule's interval used as a deterministic per-job phase offset (gap
    # SCH-3). Two schedules sharing a cadence would otherwise fire together against one background
    # worker. 0 disables the spread.
    schedule_jitter_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    # Retention windows in days (gap SCH-1). Nothing in the system deleted anything before this,
    # so every durable table grew for the deployment's lifetime. 0 disables pruning for that
    # table, which is the default: a retention period is a *policy* decision ("keep for N years,
    # then dispose, provably"), so a deployment must state it rather than inherit a number
    # from code. `audit_events`, `calculation_results` and `job_records` are deliberately absent —
    # see durable/retention.py for why each needs its own design rather than an age cutoff.
    retention_enabled: bool = False
    retention_schedule_minutes: float = Field(default=1440.0, gt=0)
    retention_timeout_seconds: float = Field(default=600.0, gt=0)
    retention_session_events_days: int = Field(default=0, ge=0)
    retention_session_messages_days: int = Field(default=0, ge=0)
    # Stored tool results (`api/tool_results.py`, migration 042) — the highest-volume table this
    # sweep touches, at up to one row per tool call.
    #
    # It was written with a 30-day default first, on the argument that this table holds no *record*
    # of anything — the answers are in `calculation_results` (D-011) and `job_records` (D-157), so
    # there is no retention policy to defer and deferring it only means an unbounded table. The
    # argument is sound and it buys nothing: `retention_enabled` is False by default, so on a
    # default deployment a number here deletes exactly as much as 0 does. The only deployment the
    # two differ for is one that switched retention on and did not state this window — and that is
    # precisely the case `test_retention_is_off_until_a_policy_is_stated` exists to refuse. One
    # rule for every window is worth more than a default that changes nothing.
    #
    # The cost is stated rather than hidden: until an operator sets this, the table grows, and it
    # grows faster than the two above it. `infra/sql/README.md` says so in its Disposal column.
    retention_tool_results_days: int = Field(default=0, ge=0)
    # How long a *delivered* result publication is kept. Only delivered rows are ever pruned: a
    # pending or failed one is the only record that something has not reached its results store,
    # and deleting that would turn an outage into a silent gap. 0 disables it, like every window
    # here.
    retention_result_publications_days: int = Field(default=0, ge=0)
    # The LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`).
    # They held the same standing as the fingerprint tables in this module's opening complaint —
    # nothing deleted from them, ever — with one difference that made it easy to miss: they are
    # created by `AsyncPostgresSaver.setup()` rather than by a migration in `infra/sql`, so they are
    # not in the schema anybody reviews. Erasure already reached them (`agent/leaver.py`); only
    # disposal did not, so a deployment that erased no one accumulated every turn's state forever.
    #
    # Pruned by **thread**, not by row: a checkpoint's `parent_checkpoint_id` chains it to the one
    # before, so deleting the old rows inside a live thread would leave a chain pointing at nothing.
    # A thread expires when its newest checkpoint does.
    retention_checkpoints_days: int = Field(default=0, ge=0)
    # How many expired sessions one conversation-prune pass may work
    # (D-2026-08-05-a-sweep-that-commits-once). The conversation prune costs three round trips per
    # session — it cannot be one `DELETE`, because whether an expired row may go depends on rows
    # that are not expiring (D-145) — so the first pass against a deployment that has never pruned
    # would attempt an unbounded number of them inside one activity, exceed
    # `retention_timeout_seconds`, and spend an attempt having committed only what it reached.
    # Capped, each pass commits a bounded amount, reports the remainder in its own result, and the
    # schedule drains the tail. 500 is roughly a minute of round trips: far more than a steady
    # state produces in a day, far less than a first pass over a year of history.
    retention_max_sessions_per_pass: int = Field(default=500, gt=0)
    # Mid-turn durable-job resume (gap AGT-2): when a turn launches a durable job, wait this
    # long for its result and continue the *same* turn with it, so "compute this, then reason
    # about the result" is one exchange. Off by default — holding a turn open holds an admission
    # permit, so a deployment opts in deliberately. Must stay below
    # `service_turn_timeout_seconds`, which bounds the whole streamed turn regardless.
    mid_turn_resume_enabled: bool = False
    mid_turn_resume_timeout_seconds: float = Field(default=60.0, gt=0)
    # Predicted-vs-actual calibration ledger (gap IDEA-2). Off by default: it needs the
    # `predictions` table (migration 016), and a deployment without it must not log warnings on
    # every prediction. `calibration_min_observations` is the floor below which the figures are
    # reported as not-yet-meaningful — a bias from three points is not a bias.
    calibration_enabled: bool = False
    calibration_min_observations: int = Field(default=8, ge=1)
    # Ceiling on what one `find_calculations` call can return. The calculation store is never
    # evicted (D-011), so it is the one table that only grows — a browse query with no cap is a
    # full scan of it, and every returned row spends the model's context. The tool clamps its own
    # `limit` to this rather than trusting the argument.
    calc_find_max_results: int = Field(default=50, ge=1)
    # Ceiling on how much of one artifact `fetch_artifact` puts into the model's context. The
    # by-products worth reading are small — an `xtbopt.xyz` is a few kB, a `vibspectrum` under ten
    # — while a 76-atom Hessian is single-digit megabytes of text that no answer is built by
    # reading. This is what separates them, since both are text and neither can be refused on type.
    calc_artifact_max_chars: int = Field(default=20_000, ge=1)
    # How many characters of one stored calculation's payload `find_calculations` may render. A
    # *listing* budget, not a payload budget: the same result read by asking for that calculation
    # directly is unbounded, because then a chemist asked for exactly it.
    #
    # It exists because this was the largest unbounded model-facing payload in the system. A stored
    # `xtb.conformers` row holds every member the search found — 66,520 characters on one 40-atom
    # molecule — and `calc_find_max_results` is 50, so one call could render ~830,000 tokens: past
    # every provider's context limit, where the failure is hard rather than graceful. Geometries
    # project to their addresses first (D-2026-08-21), which is most of the reduction; this catches
    # whatever is still large, and the record says `result_omitted` rather than cutting silently.
    calc_find_max_result_chars: int = Field(default=4_000, ge=1)
    # Ceiling on one `calculator_outliers` page. The listing exists to be *read* — a chemist looks
    # at the worst misses and asks what they have in common — and a hundred rows is not read, it is
    # scrolled past while spending the model's context.
    calc_outliers_max_results: int = Field(default=25, ge=1)
    # Standing-query digests (gap IDEA-1). Off by default: it needs the `subscriptions` table
    # (migration 017), and a deployment nobody has subscribed on would just run an empty sweep.
    digest_enabled: bool = False
    digest_schedule_minutes: float = Field(default=1440.0, gt=0)
    digest_timeout_seconds: float = Field(default=300.0, gt=0)
    # Uploaded working files (gap AGT-3). Bounded in both directions: one oversized upload must
    # not blow a pod's memory, and a chemist uploading all morning must not either. Attachments
    # are session-scoped working material, so they are lost with the pod by design.
    attachment_max_bytes: int = Field(default=2_000_000, gt=0)
    attachment_max_per_session: int = Field(default=10, ge=1)
    # Parsing an upload is CPU-bound work over untrusted bytes in third-party libraries, so it runs
    # in a worker thread with these two bounds rather than inline on the request's event loop
    # (`chemclaw.agent.attachments.parse_attachment_off_loop`). The concurrency cap is what keeps
    # a burst of hostile uploads from occupying the whole default thread pool — the same pool
    # `chemclaw.api.auth` validates every bearer token in — so it is deliberately small: the front
    # door runs one uvicorn worker, and parsing is not what the pod is for. Past the cap an upload
    # waits `attachment_parse_queue_seconds` for a slot and is then shed (503, retryable) — the
    # turn admission's queue-briefly-then-shed discipline, and for the same reason: shedding at
    # the cap itself punishes the ordinary burst (four spreadsheets dropped on the UI at once
    # measured as two 200s and two 503s) while doing nothing extra against a sustained flood.
    # Queueing is safe here only because a waiter holds a future rather than a thread.
    # The timeout bounds the *wait*, not the thread: Python cannot kill one, so a parse past this
    # limit is refused to its client while the thread runs to completion against the cap.
    attachment_parse_timeout_seconds: float = Field(default=30.0, gt=0)
    attachment_parse_queue_seconds: float = Field(default=10.0, ge=0)
    attachment_max_concurrent_parses: int = Field(default=2, ge=1)
