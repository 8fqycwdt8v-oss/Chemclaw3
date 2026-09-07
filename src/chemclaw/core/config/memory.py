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
    # This window disposes of a thread **whole**, when its newest checkpoint is older than the
    # cutoff. It is not what bounds a thread that is still in use — that is
    # `checkpoint_retain_per_thread` below, and the two answer different questions: this one is
    # disposal (a policy a deployment states), that one is deduplication of superseded copies.
    # This comment used to carry the claim that in-thread pruning is impossible at all;
    # `D-2026-09-06-a-superseded-checkpoint-is-a-copy-not-a-record` measured it false.
    retention_checkpoints_days: int = Field(default=0, ge=0)
    # How many checkpoints per `(thread_id, checkpoint_ns)` survive one turn's prune. **Not a
    # retention window, which is why it has a non-zero default and is not gated on
    # `retention_enabled`.** Every superstep of every turn rewrites the whole `messages` channel, so
    # a thread stores `O(turns^2)` bytes of *superseded copies of state its newest checkpoint still
    # holds in full*: measured, a 40-turn thread carrying 139.6 kB of conversation stored 10.3 MB of
    # `checkpoint_blobs` across 520 `checkpoints` rows. Deleting those copies disposes of no
    # record — nothing a chemist, the model, `session_fork` or `plan_state` reads changes — so it
    # is not a policy decision the way "keep a conversation for N years" is, and defaulting it
    # to 0 would leave every shipped deployment paying the quadratic.
    #
    # 3 rather than 1: 1 was measured working, including under concurrent live turns, but a margin
    # costs ~25 kB a thread and removes the need to reason about a partially-written superstep at
    # all. 0 disables the prune, which is the escape hatch for a deployment that wants LangGraph
    # time-travel over a thread's whole history — nothing in `src/` uses it (`aget_state_history`
    # has no caller and the one `aget_tuple` passes `thread_id` alone), which is what makes the
    # default safe.
    checkpoint_retain_per_thread: int = Field(default=3, ge=0)
    # How many expired sessions one conversation-prune pass may work
    # (D-2026-08-05-a-sweep-that-commits-once). The conversation prune costs three round trips per
    # session — it cannot be one `DELETE`, because whether an expired row may go depends on rows
    # that are not expiring (D-145) — so the first pass against a deployment that has never pruned
    # would attempt an unbounded number of them inside one activity, exceed
    # `retention_timeout_seconds`, and spend an attempt having committed only what it reached.
    # Capped, each batch commits a bounded amount and reports whether a tail remains. 500 is roughly
    # a minute of round trips: far more than a steady state produces in a day, far less than a first
    # pass over a year of history.
    #
    # **This bounds a batch, not a pass**, and the two used to be the same thing. Ending the pass
    # here meant one pass disposed of at most 500 conversations against an arrival rate of 400–1 000
    # a day, so the backlog grew while every pass reported success; `_prune_expired_rows` now sweeps
    # again while a branch reports a tail and `retention_timeout_seconds` can still afford another
    # sweep. So this number bounds one transaction, one set of round trips and one set of row locks,
    # and the clock bounds the pass.
    retention_max_sessions_per_pass: int = Field(default=500, gt=0)
    # The same bound for the age-cutoff branch (`session_events`, `tool_result_blobs`,
    # `result_publications`), in rows rather than conversations: how many rows one `DELETE` removes
    # before committing and asking again. Its sibling above counts conversations because that branch
    # costs three round trips *per session*; this one is a single indexed `DELETE`, so the unit that
    # decides its cost is the row.
    #
    # **What it trades, measured on 300 000 `tool_result_blobs` rows (2.4 GB, PostgreSQL 16.15).**
    # Unbounded, the `DELETE` takes 11.5 s and is cancelled by a 5 s `statement_timeout` having
    # removed **0** rows — every attempt, so Temporal exhausts `activity_max_attempts` and the table
    # never shrinks. At 10 000 the same work commits in 11.04 s across 31 batches, worst batch
    # 1 385 ms. Too large reproduces that defect exactly, on a schema whose worst row is
    # `STORAGE EXTERNAL`; too small multiplies round trips against a fixed per-statement cost,
    # holding the pass open for its whole `retention_timeout_seconds` and disposing of no more.
    #
    # It is a setting and not a module constant because the right value follows the deployment's
    # row size and its `pg_statement_timeout_seconds`, and neither is knowable from here — the
    # shipped 10 000 is sized against *this* schema's worst row, which is the one thing a site may
    # not share. There is deliberately no cross-check against that timeout: a row count and a wall
    # clock have no convertible relationship without a rate, which is why this is a knob rather than
    # a derived value, and `_prune_by_age`'s own budget is what stops a batch series too slow for
    # the pass it is inside.
    retention_delete_batch_rows: int = Field(default=10_000, gt=0)
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
    # ...and in the third direction, which the two above do not cover: what every live session's
    # attachments cost *together*. The store's other bound is `service_max_live_sessions` (1000),
    # a count — and a count of entries that each hold up to `attachment_max_per_session` parsed
    # documents is a many-GB ceiling in a pod the chart limits to 1 GiB
    # (`deploy/helm/chemclaw/values.yaml`, `resources.service.limits.memory`). Measured, ~20 MB of
    # text is retained per fully-loaded session, so the shipped pod is over its limit at ~25 of
    # them — 2.5 % of the count bound, reachable inside the shipped rate limit by one authenticated
    # chemist. This is the bound in the unit that actually kills the pod: past it the
    # least-recently-used *sessions* lose their attachments (working material, recoverable by
    # re-uploading), instead of the pod losing every in-flight turn to an OOM kill. 64 MB is 6 % of
    # the shipped limit and about three fully-loaded sessions; raise it with the pod's memory limit,
    # not with `service_max_live_sessions`.
    #
    # **Bytes as the pod counts them, not characters**, and the difference is not a rounding one:
    # `agent/attachments._resident_bytes` measures with `sys.getsizeof`, because CPython stores a
    # string at 1, 2 or 4 bytes per codepoint and a `len()` budget therefore permitted 128 MB
    # resident on CJK text and 256 MB on astral. The sizing above is what a *byte* bound buys.
    #
    # It is deliberately **smaller than `document_max_expanded_bytes`** (64 MiB), which is what one
    # parsed upload may weigh: that ceiling is sized against the *parse*, which happens twice
    # concurrently at most, while this one is sized against what is *retained* for every live
    # session at once. The consequence — a single attachment can outweigh the whole store — is held
    # by `AttachmentStore.add`, which drops that session's older files first, and by
    # `core/bounded.py`, which no longer empties the map for an entry that cannot fit it.
    attachment_store_max_bytes: int = Field(default=64_000_000, gt=0)
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
