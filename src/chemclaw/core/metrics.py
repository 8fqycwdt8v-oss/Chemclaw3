"""Process metrics in Prometheus text format.

Counts what logs and traces cannot answer for an operator or autoscaler: load shedding (admission
503s, budget 429s), lost audit records, saturation (in-flight turns against the admission cap, the
signal the HPA should scale on rather than CPU), and latency percentiles, which sampled per-request
traces cannot give.

No `prometheus_client` dependency: the exposition format is a small, stable text protocol and this
module is the only place that knows it. Metrics are process-wide (one registry per pod, the scope a
scrape targets), stdlib-only, and used by every process kind, hence kernel material.

Not related to `evals/metric.py` / `evals/metrics.py`, which score eval criteria.
"""

import logging
import threading
from bisect import bisect_left
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)


def _escape(value: str) -> str:
    r"""Escape a label value for the exposition format.

    The format requires `\\`, `"` and newline escaped; done unconditionally because it is a property
    of the format, not of today's label values.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _sample(value: float) -> str:
    """Render one sample *value* in the Prometheus text format.

    `:g` is wrong twice: it renders `inf`/`nan` where the format needs `+Inf`/`NaN` (one rejected
    sample fails the whole scrape), and it keeps only six significant digits, silently rounding any
    counter past a million. `repr` is the shortest exact round-trip. The value is coerced to `float`
    first because a sample is a float64 anyway, and `str(True)` would emit `True` and break the
    scrape.
    """
    value = float(value)
    if value != value:  # NaN is the only value unequal to itself
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == float("-inf"):
        return "-Inf"
    return repr(value)


# Metric name -> help text. Declared up front so every metric is documented at its definition and
# the exposition always carries HELP/TYPE lines (a scrape without them is much harder to read).
_COUNTERS: dict[str, str] = {
    "chemclaw_turns_started_total": "Turns admitted and started.",
    "chemclaw_egress_refused_total": "Outbound connections the in-process egress guard refused.",
    # Per-source evidence accounting: a leg returning nothing on every query is a broken deployment,
    # not an empty corpus.
    "chemclaw_evidence_source_chunks_total": "Chunks each evidence source contributed to a sweep.",
    "chemclaw_evidence_source_failures_total": (
        "Evidence sources that raised during a sweep, by source — labelled so it can be read "
        "against the chunk counter above, which is the only way to tell a dark leg from a broken "
        "one."
    ),
    "chemclaw_evidence_source_skips_total": (
        "Evidence sources that declined a sweep (RetrieverSkip), by source — a stated refusal "
        "(unentitled caller, unsupported filter, absent index), distinct from both a zero-chunk "
        "answer and a failure. The third channel D-2026-08-01's class needed: without it a leg "
        "that always declines is indistinguishable from a healthy leg that never matches."
    ),
    # "emitted", not "ended in": a turn stopped by the loop cap emits an error and still delivers
    # its partial answer; `chemclaw_turn_loop_caps_total` counts that case precisely.
    "chemclaw_turns_failed_total": "Turns that emitted an error event.",
    # Queueing is the system absorbing a burst; shedding is declining one. Rising queue with flat
    # shed is capacity in use; both rising is capacity exceeded. Both are reported on the turn's own
    # stream, so no HTTP status counts them at the load balancer.
    "chemclaw_turns_queued_total": (
        "Turns that had to wait for an admission permit (a `queued` event was streamed)."
    ),
    "chemclaw_turns_shed_total": (
        "Turns ended by the admission timeout because no permit ever freed (D-166: an error "
        "event on an open stream, previously an HTTP 503)."
    ),
    "chemclaw_turns_refused_budget_total": "Turns refused with 429 by the turn/token budget.",
    # Separate from the budget refusal, whose alert's remedy is the token window: a conversation at
    # `session_max_thread_bytes` is cleared by a new session.
    "chemclaw_turns_refused_thread_size_total": (
        "Turns refused because their session's stored conversation reached "
        "session_max_thread_bytes."
    ),
    # Unlabelled: `/metrics` is unauthenticated and an `oid` is an unbounded caller-chosen key, so
    # an `actor` label would hit the series cap exactly when it matters. The identity is in the
    # WARNING. Separate from `chemclaw_turns_conflict_total`, which counts turns waiting in one
    # session's line.
    "chemclaw_turns_refused_actor_cap_total": (
        "Turns refused with 429 because the actor already held "
        "`service_max_concurrent_turns_per_actor` concurrent turns on this process."
    ),
    # Revision passes over a flagged answer. Separate from exhaustion because only "rejecting it
    # failed to help" is a defect: always-exhausting revisions pay double for nothing.
    "chemclaw_answer_revisions_total": "Revision passes run over an answer the verifier flagged.",
    # The denominator for the exhaustion ratio: revisions count passes, exhaustion counts turns, and
    # dividing passes into turns makes the alert go silent at total failure.
    "chemclaw_answer_review_turns_total": ("Turns whose flagged answer entered the revision loop."),
    "chemclaw_answer_review_exhausted_total": (
        "Turns whose answer was still unsupported after every allowed revision, and went out "
        "marked for review."
    ),
    # What happened to the escalation, by outcome. `joined` (a later exhausted turn joining an open
    # review of the same conversation) dominating `opened` means reviews pile into few threads;
    # `no_actor` dominating means an unauthenticated deployment escalating to nobody; `unavailable`
    # is the broker and is also a `degraded` call.
    "chemclaw_answer_review_escalations_total": (
        "Attempts to ask a person to read an answer the revision rounds could not clear, by what "
        "became of the request."
    ),
    # Questions refused because the knowledge they rest on moved, by end and reason: an `ask`
    # refusal tells a model to rewrite its citation, an `answer` refusal turns a chemist away with
    # no override. A climbing `answer` end means the guard refuses work it should admit.
    "chemclaw_premise_refusals_total": (
        "Questions refused because a note they cite was retired, not yet valid, or absent."
    ),
    # Check-in sweep runs that stopped before reaching everybody, counted per run rather than per
    # requester: the common deferral stops at a page boundary with zero requesters known to be left,
    # so a requester count would read zero on the case it exists to report.
    "chemclaw_work_check_in_deferrals_total": (
        "Check-in runs that stopped before reaching every blocked requester."
    ),
    # Requesters told their own work is still blocked, before its deadline rather than after.
    "chemclaw_work_check_ins_total": (
        "Check-ins delivered to a requester about questions of theirs still waiting."
    ),
    # Lead time before a 429, at `budget_warn_fraction` of any cap. Unlabelled because the scope is
    # a session id or `oid`, which may never be a label value; the log line carries the identity.
    "chemclaw_budget_warnings_total": (
        "Times a session or user crossed `budget_warn_fraction` of a turn or token cap."
    ),
    # Not a refusal: a message finding its session busy waits in the session's line. `scope` says
    # what was busy: this process's slot, another replica's durable claim, or an occupied line.
    "chemclaw_turns_conflict_total": (
        "Turns that found their session busy and joined its line (previously refused 409)."
    ),
    "chemclaw_turn_queue_refused_total": (
        "Messages refused a place in a line: 409 because the session's line was full or the sender "
        "already had a message waiting in it, 429 because this process held its most waiters."
    ),
    "chemclaw_turn_queue_withdrawn_total": (
        "Waiting messages that never ran: withdrawn, their sender removed or no longer signed in "
        "or at their concurrent-turn cap when it was their turn, or the session deleted."
    ),
    # A shared session's membership is read per request, and a watch is one request lasting a turn,
    # so it is read again while watching; this counts the views that check closed.
    "chemclaw_turn_watchers_removed_total": (
        "Views of a running turn closed because their watcher was removed from the session."
    ),
    # Fan-out's one failure mode, counted so a rising rate is visible: a participant's view of a
    # running turn filled its own buffer and was cut off rather than allowed to slow anybody else.
    "chemclaw_turn_readers_lagged_total": (
        "Views of a running turn cut off because their reader fell a full buffer behind."
    ),
    # The holder's poll for requests other replicas address to its turns. A failing poll turns a
    # Stop sent to another replica into a 503 and a reattach there into a wait.
    "chemclaw_turn_relay_poll_failures_total": (
        "Polls for requests addressed to this process's turns by other replicas that failed."
    ),
    "chemclaw_turn_timeouts_total": "Turns cancelled by the wall-clock turn timeout.",
    # Separate from the timeout above: this counts turns cut because the client stopped reading
    # (`service_sse_send_timeout_seconds`), not because the turn ran long.
    "chemclaw_turn_send_timeouts_total": (
        "Turn streams closed because a client stopped reading past the SSE send timeout."
    ),
    # A rising rate is an agent planning more work than a turn can close: a prompt or skill problem,
    # not an outage.
    "chemclaw_turn_loop_caps_total": "Turns stopped by the harness loop's iteration cap.",
    # The billed-token counterpart of the iteration cap; the two move independently. A rising rate
    # means turns are too expensive (retrieval or tool-result size). On by default; `0` for
    # `agent_max_turn_billed_tokens` disables it. The shipped value is a runaway backstop, so any
    # movement warrants checking the cap against this deployment's `turn_costs` rows.
    "chemclaw_turn_spend_caps_total": "Turns stopped by the per-turn billed-token cap.",
    # A disconnect detaches a turn rather than stopping it, so these separate clients dropping away
    # mid-turn (turn completes, billed whole) from someone pressing Stop.
    "chemclaw_turns_detached_total": (
        "Turns whose client disconnected mid-run and that continued to completion detached."
    ),
    "chemclaw_turns_stopped_total": "Turns cancelled by the explicit stop route.",
    # An unloading page's stop waits out `service_turn_unload_grace_seconds` for a reload to
    # reattach. Deferred = resumed + expired + turns ending on their own inside the window.
    "chemclaw_turns_stop_deferred_total": (
        "Unload stops that were deferred for a reload to reattach rather than applied at once."
    ),
    "chemclaw_turns_stop_resumed_total": (
        "Deferred unload stops cancelled because the turn's sender reattached inside the window."
    ),
    "chemclaw_turns_stop_expired_total": (
        "Deferred unload stops that cancelled the turn because nobody reattached in the window."
    ),
    # Distinct from the loop cap: a capped turn has a partial answer, while this one produced no
    # prose at all — a shape invisible in every other signal.
    "chemclaw_turn_empty_answers_total": "Turns that ended without producing any answer text.",
    # The transcript (`session_messages`, what a chemist sees on reload) is written once after the
    # answer; the checkpoint (what the model sees next turn) is written incrementally and never
    # rolled back, so a teardown between them leaves the model holding an exchange the chemist
    # cannot see. Only the branch that knowingly keeps such a turn is counted; other paths can
    # diverge too, so a flat series is not proof of agreement.
    "chemclaw_transcript_thread_divergence_total": (
        "Turns whose exchange was kept in the checkpointer while the transcript never got it, so "
        "the chemist's view of that session is one turn behind the model's."
    ),
    # Result publication: a record that could not be queued is a local database problem, one that
    # could not be delivered is the external store's, and a growing queue is neither. Queued against
    # published says whether the drain keeps up.
    "chemclaw_results_queued_total": (
        "Computed results projected and queued for an external results store."
    ),
    "chemclaw_results_published_total": (
        "Computed results confirmed durable at an external results store."
    ),
    "chemclaw_result_publish_failures_total": (
        "Result publications that could not be queued or delivered."
    ),
    # A projector that raises does so for every payload of that shape until the code changes, so it
    # is kept apart from destination failures, which are transient.
    "chemclaw_result_projection_failures_total": (
        "Stored payloads this release could not project into a record (a code gap, not an outage)."
    ),
    "chemclaw_audit_sink_failures_total": (
        "Audit records that could not be persisted (the trail is incomplete)."
    ),
    # Separate from refused batches: this counts the oldest buffered rows shed because the database
    # could not keep up. Reachability and throughput have different remedies.
    "chemclaw_audit_events_shed_total": (
        "Audit records dropped from the write buffer because it reached its bound."
    ),
    # Turns that could not reach Temporal. Turns only, Temporal only: request-path
    # `SubsystemUnavailableError`s (which include non-Temporal failures) have their own counter
    # below. `tests/test_metric_declarations.py` holds this to its one increment site.
    "chemclaw_durable_unreachable_total": (
        "Turns whose durable-subsystem health probe failed (Temporal did not answer)."
    ),
    # Requests a handler shed for `SubsystemUnavailableError`, like `chemclaw_db_unavailable_total`.
    # Which subsystem is in the `shedding` log line, not a label, to keep the label set bounded.
    "chemclaw_subsystem_unavailable_total": (
        "HTTP requests shed with 503 because a subsystem they needed was unavailable."
    ),
    # Non-zero means provider usage could not be parsed and those turns were metered at zero, so the
    # budget guard is not binding. Absent usage is legitimate and not counted.
    "chemclaw_usage_unreadable_total": (
        "Streamed usage contents that carried no readable token count (the turn metered zero)."
    ),
    # Non-zero means answers are scored by the citation gate instead of the LLM judge: a weaker
    # verdict (resolvability rather than faithfulness), not a slow path.
    "chemclaw_verifier_degraded_total": (
        "Answers scored by the deterministic citation gate because the LLM judge was unavailable."
    ),
    # Extra judge rolls at the review-band margin; over answers verified, the fraction of turns
    # paying for the band.
    "chemclaw_verifier_band_rerolls_total": (
        "Extra judge rolls taken because a verdict landed inside the review band."
    ),
    # Protocols handed to the condenser, by outcome: `extracted`, `degraded` (kept recorded figures)
    # or `oversized` (refused, never split). One labelled series because the question is the ratio.
    "chemclaw_protocol_digests_total": (
        "Protocols handed to the condenser, by outcome (extracted / degraded / oversized)."
    ),
    # Launches, counted when `start_workflow` returns. `turn_costs.jobs_started` counts something
    # different: jobs a turn left running past the inline wait (`connectors/jobs.py`). A job
    # finishing inside the turn moves this and not the row.
    "chemclaw_jobs_started_total": (
        "Durable jobs launched by an agent tool — every start, whether or not the run outlived "
        "the turn. Not the same population as `turn_costs.jobs_started`, which counts the jobs a "
        "turn left still running; a job that finishes inside its turn is in this one only."
    ),
    # `notify_session_best_effort` swallows a failed push-back by design; this is the only aggregate
    # that shows the job-to-session channel failing.
    "chemclaw_pushback_dropped_total": (
        "Job push-back notifications that could not be recorded and were dropped."
    ),
    # Whether a rejoin is announced turns on one `describe()` call whose failure is only a DEBUG
    # line.
    "chemclaw_rejoin_describe_failed_total": (
        "Rejoined durable runs whose describe() failed, so the rejoin went unannounced."
    ),
    # Consumption rather than launches: accumulated job seconds, so `rate()` reads as
    # compute-seconds per second. A counter, not a histogram, because the question is how much was
    # consumed and the shared buckets top out far below long searches. Not node-hours: parallelism
    # is not reported back.
    "chemclaw_job_runtime_seconds_total": (
        "Wall-clock seconds accumulated by finished durable jobs, by connector."
    ),
    "chemclaw_notes_recorded_total": "Notes written into the knowledge graph.",
    # Unparseable notes are dropped from the graph so one bad file cannot block queries; this makes
    # that visible on the tree a pod actually serves (`kg-validate` only covers the repository).
    "chemclaw_notes_unparseable_total": (
        "Note files skipped by the indexer because they failed to parse; they are not retrievable."
    ),
    # Two files claiming one note id: the indexer keeps the first in path order and the second
    # becomes unreachable. A corpus problem, typically a sync landing a renamed note before removing
    # the old one.
    "chemclaw_notes_duplicate_id_total": (
        "Note files skipped by the indexer because another file already claimed their id; "
        "one of the two is not retrievable."
    ),
    # Every tournament stage degrades silently by design (a run where every judge failed still
    # returns a ranked table from the prior), so outcomes are counted to tell a broken run from a
    # healthy one.
    "chemclaw_hypothesis_tournaments_total": (
        "Hypothesis tournaments that finished, by what they were able to produce."
    ),
    # A generator that stops producing usable refutation conditions after a model change is
    # invisible without this: the screen is the only stage that deletes, and it deletes quietly.
    "chemclaw_hypothesis_screen_rejections_total": (
        "Hypotheses removed before the tournament, by which mechanical rule removed them."
    ),
    # Why hypothesis checks were refused, by reason: missing structures, a missing role for an
    # expensive trigger, and an undeclared field are three different repairs.
    "chemclaw_hypothesis_check_refusals_total": (
        "Discriminating checks that were not run, by the grounding rule that refused them."
    ),
    "chemclaw_notes_publish_failures_total": (
        "Knowledge notes that could not be written into the graph; the knowledge was lost."
    ),
    # A fan-out child that exhausted its retries and was dropped (`durable/orchestrator.py`).
    # Isolate-and-drop is right, but the parent then completes successfully with a short list, so
    # without this a failing child (for example every `PublishNoteWorkflow` on an expired git
    # credential) is invisible.
    "chemclaw_fan_out_children_dropped_total": (
        "Fan-out children that failed their retries and were dropped; their work is missing from "
        "an otherwise successful parent."
    ),
    # A turn whose connectors did not come up still answers from the remaining tools,
    # indistinguishably in the transcript. Counted per unreachable connector so one dark connector
    # and a dark fleet differ.
    "chemclaw_connectors_unreachable_total": (
        "Connectors that failed to come up when a turn or template step opened them, by connector; "
        "their tools were absent from that turn."
    ),
    "chemclaw_event_streams_rejected_total": (
        "Push-back event streams rejected with 429 at the per-user or per-process cap."
    ),
    # The push-back stream's own send timeout, separate from the turn stream's: the push-back stream
    # is long-lived and holds a per-user slot, so a half-open connection parks it invisibly.
    "chemclaw_event_stream_send_timeouts_total": (
        "Push-back event streams closed because a client stopped reading past the SSE send timeout."
    ),
    # Entra replaces `groups` with `_claim_names` past roughly 150 memberships, and resolving that
    # needs a Graph call this system does not make, so the user silently loses group-derived
    # entitlements. Unlabelled: an `oid` label would key an unauthenticated exposition on user
    # identity.
    "chemclaw_group_claim_overage_total": (
        "Validated tokens that carried a group-claim overage (`_claim_names`) instead of `groups`, "
        "so no group-derived entitlement could be read for that user."
    ),
    "chemclaw_db_unavailable_total": (
        "Requests shed with 503 because a pooled Postgres connection could not be obtained."
    ),
    # The upload cap's own shed, separate from turn admission: parse slots meter worker-thread CPU,
    # permits meter LLM turns.
    "chemclaw_attachment_parses_shed_total": (
        "Uploads refused with 503 because every parse slot was still busy after "
        "`attachment_parse_queue_seconds`."
    ),
    # Evictions from the two bounded agent-writable tables, counted apart because a memory is a file
    # a turn authored and a preference is how one person works. An eviction is a chemist losing
    # something they were told was remembered, so it is a WARNING and a series.
    "chemclaw_memory_evictions_total": (
        "Durable memory files dropped because a namespace was over `agent_memory_max_files`."
    ),
    "chemclaw_preference_evictions_total": (
        "Preferences dropped because one owner was over `preferences_max_per_owner`."
    ),
    # A helper's file cut on its way into the caller's checkpointed state; separate from tool-result
    # truncation because it bounds checkpoint size, not context.
    "chemclaw_subagent_file_truncations_total": (
        "Files a helper wrote that were cut on their way into its caller's state."
    ),
    "chemclaw_document_parse_kills_total": (
        "Document parses whose reader process was killed for outrunning its deadline."
    ),
    # Refusals before a turn exists, per request. Unlabelled: a per-principal series would key the
    # unauthenticated `/metrics` on user identity; who hit it is in the log.
    "chemclaw_requests_rate_limited_total": (
        "Requests refused with 429 by the per-principal request budget."
    ),
    # The shared bucket's database was unreachable and this replica limited on its own: the limit
    # was per replica for that request.
    "chemclaw_rate_limit_shared_unavailable_total": (
        "Requests limited by this replica's own bucket because the shared one was unreachable."
    ),
    # A finished turn's bookkeeping that outlived `service_turn_bookkeeping_timeout_seconds`: the
    # answer went out without it, and a kill in that window loses the write.
    "chemclaw_bookkeeping_unsettled_total": (
        "Bookkeeping writes (cost row, budget booking, spent approval) still running when the "
        "turn stopped waiting for them."
    ),
    "chemclaw_requests_too_large_total": (
        "Requests refused with 413 because the body exceeded service_max_request_bytes."
    ),
    # The cross-process turn guard is a lease that holds only while refreshed; a failed refresh is
    # the guard narrowing.
    "chemclaw_turn_claim_refresh_failures_total": (
        "Failed refreshes of a running turn's session claim (D-121): the claim may lapse and "
        "another worker start a turn on the same session."
    ),
    # The event the refresh-failure counter warns about: a refresh matching no row raises nothing,
    # since the takeover has already happened.
    "chemclaw_turn_claims_lost_total": (
        "Running turns whose session claim was taken over by another worker (D-121): the lease "
        "lapsed while the turn was still going, so two workers may have run on one session."
    ),
    # Repeated identical tool calls refused within a turn; the turn still answers, so only this
    # shows a looping agent.
    "chemclaw_repeated_tool_calls_total": (
        "Tool calls refused because the turn had already made the identical one "
        "`max_identical_tool_calls` times, labelled by tool."
    ),
    # Spend as an observable rate; the budget guard's own metering only refuses turns.
    "chemclaw_tokens_total": "Model tokens reported across all turns (prompt + completion).",
    # Spend split along the dimensions it is priced along: input, output and cache-read tokens
    # differ in price (cache reads are much cheaper), so one total cannot answer what a deployment
    # costs.
    "chemclaw_input_tokens_total": "Prompt tokens sent to the model, excluding cache reads.",
    "chemclaw_output_tokens_total": "Completion tokens generated by the model.",
    "chemclaw_cache_read_tokens_total": (
        "Prompt tokens served from the provider's cache — priced well below a fresh input token, "
        "so this is the number that shows caching working."
    ),
    "chemclaw_cache_write_tokens_total": (
        "Prompt tokens written to the gateway's cache — priced above a fresh input token, so a "
        "cache that is written and never read is a net loss this makes visible. **Expect a flat "
        "zero**: an OpenAI-compatible endpoint caches implicitly and reports reads, and only some "
        "report a write count at all, so a zero here is the normal reading rather than a fault "
        "(REV-9). Kept because the column and the counter are what would show a gateway that does "
        "report one."
    ),
    # What the gateway billed and never reported: usage arrives only on the terminal stream chunk,
    # so an abandoned turn's prompt is estimated (`agent/turn_usage.InFlightPrompts`), charged to
    # the budget and recorded in `turn_costs.estimated_tokens`. A separate counter rather than a
    # label or a sum, so measured series stay measured and panels can show inferred spend beside
    # them.
    "chemclaw_estimated_tokens_total": (
        "Model tokens a turn was billed for that the provider never reported — the estimated "
        "prompt of every request still in flight when a turn was torn down (a disconnect, the "
        "Stop button, a wall-clock deadline). **Inferred, never measured**: read it beside "
        "`chemclaw_tokens_total`, never added into it without saying so. A flat zero means every "
        "turn reached its terminal usage frame, which is the healthy reading."
    ),
    # Outbound delivery, by channel: accepted against failed. A channel taking nothing while another
    # takes everything is a broken webhook; both failing is an outage.
    "chemclaw_deliveries_total": ("Messages a delivery channel accepted, by channel."),
    # Refused records deleted by the ledger's per-source growth bound (`ingest/rejections._EVICT`
    # keeps the newest `_MAX_ROWS_PER_SOURCE`). Keeping only the newest assumes a source's refusals
    # are one systematic defect; an evicted record then reads as never refused, so this checks the
    # assumption.
    "chemclaw_ingest_rejections_evicted_total": (
        "Refused records deleted by the per-source growth bound of the ingest rejection ledger."
    ),
    # Uploads dropped from a live session past `attachment_max_per_session` or
    # `attachment_store_max_bytes`; `read_attachment` then reports the file as never sent.
    "chemclaw_attachment_evictions_total": (
        "Uploads dropped from a session past its per-session count or byte bound."
    ),
    "chemclaw_delivery_failures_total": (
        "Messages a delivery channel refused or could not be sent, by channel. A failure here is "
        "swallowed so one channel's outage is not everyone's, which is exactly why it must count."
    ),
    "chemclaw_context_compactions_total": (
        "Model calls whose message list was reduced to stay inside the context token budget."
    ),
    "chemclaw_context_reclaimed_tokens_total": (
        "Estimated prompt tokens reclaimed by context compaction (char/4 estimate, not billed "
        "tokens — the billed figure is chemclaw_input_tokens_total)."
    ),
    # A flat zero on the compaction counters means "never reduced", not "never over budget": both
    # edits can run and reclaim nothing. This counts requests the policy could not bring inside
    # `agent_context_token_budget` as `context_budget.effective_trigger` converts it, charging the
    # request's own prefix unconditionally. A flat zero is evidence every request stayed inside the
    # budget; declaring `llm_context_window_tokens` makes this a leading indicator of context-length
    # failures rather than spend overruns.
    "chemclaw_context_unreducible_total": (
        "Model calls whose whole request — this call's prefix plus the thread — stayed over "
        "agent_context_token_budget after the context policy had reduced all it could. Where "
        "llm_context_window_tokens is declared the budget is additionally capped by what the model "
        "can hold, so a tick is the leading indicator of a context-length failure at the provider."
    ),
    # The rate is the signal: a tool that truncates on every call has a ceiling set wrong for what a
    # model can read.
    "chemclaw_tool_results_truncated_total": (
        "Tool results cut to agent_max_tool_result_chars before the model read them, by tool."
    ),
    # Separate from tool-result truncation because the fix differs: a truncated template prompt is a
    # `${steps.<id>.result}` reference interpolating too much, fixed in the template.
    "chemclaw_template_prompt_truncated_total": (
        "Template agent-step prompts cut to agent_max_tool_result_chars before the step's model "
        "read them, by template. A rate above zero means a step is interpolating a result too "
        "large to read: narrow the step, or reference a field of the result rather than all of it."
    ),
    # Everything done deliberately and invisibly: catch, log a warning, continue with less. Each
    # swallow is right (telemetry must not fail a turn), which is why it must leave a number. One
    # counter with a `subsystem` label so "is anything degraded, and what" is one `sum by
    # (subsystem)`. A lost audit record keeps its own `chemclaw_audit_sink_failures_total` with its
    # own alert.
    "chemclaw_degraded_total": (
        "Operations that failed and were continued past with reduced function, by subsystem."
    ),
    # --- the registry's own health -------------------------------------------------------------
    # Failures of the metrics surface itself: a bound gauge that raises, and a label set refused at
    # the cardinality cap.
    "chemclaw_gauge_read_failures_total": (
        "Gauge reads that raised and were omitted from a scrape, by metric — the reading is "
        "missing, and without this the omission is indistinguishable from an unbound gauge."
    ),
    "chemclaw_metric_series_dropped_total": (
        "Samples discarded because their metric had already reached the per-metric label-set cap; "
        "that metric is undercounting from this point on."
    ),
    # --- the HTTP surface ----------------------------------------------------------------------
    # `route` is the *template*, never the raw path.
    "chemclaw_http_requests_total": "HTTP requests served, by route template and status class.",
    # Refusals before any handler, plus the authorization refusal `deps.py` deliberately returns as
    # 404 (so existence does not leak); this record is the only place a session-enumeration scan
    # shows.
    "chemclaw_authz_refusals_total": (
        "Requests refused because the caller does not own the resource, by resource kind "
        "(answered 404 to avoid an existence leak, so this counter is the only trace)."
    ),
    "chemclaw_auth_failures_total": (
        "Requests refused at authentication, by reason (missing / invalid / provider_unavailable / "
        "network_exposed) — a client sending no header at all used to be indistinguishable from a "
        "healthy service, and the last of those is the service refusing to hand its dev principal "
        "to a request that arrived from the network."
    ),
    "chemclaw_request_validation_failures_total": (
        "Requests rejected with 422 by request-body validation, by route template. Nothing logged "
        "or counted these, so a client looping on a malformed body looked exactly like silence."
    ),
    # --- the model call ------------------------------------------------------------------------
    # Graph model calls only, as the HELP text says: observed by the `RecordModelCalls` middleware,
    # so calls outside graph nodes (the verifier's judge and band rerolls, `agent/condense.py`
    # inside tool bodies) are not counted here. The token counters answer what every call cost; this
    # answers how the graph's own model traffic behaves.
    "chemclaw_model_calls_total": (
        "Model calls **made from a graph node**, by outcome (ok / rate_limited / context_length / "
        "timeout / transport / auth / error) — `auth` is the gateway refusing the credential "
        "(401/403). Calls made outside the graph are not here — the verifier's "
        "judge and any call a tool body makes — so this is not the gateway's whole request rate; "
        "`chemclaw_tokens_total` is metered on the callback seam and does see all of them. There "
        "is no `provider` label: every call goes to one gateway "
        "(D-2026-09-04-a-gateway-is-the-only-provider), and a label with one value is cardinality "
        "that answers nothing."
    ),
    "chemclaw_model_fallbacks_total": (
        "Model calls served by the fallback endpoint after the primary raised — the signal that "
        "makes endpoint failover something an operator knows about rather than infers."
    ),
    # --- the tool chain ------------------------------------------------------------------------
    # Four outcomes, separating a refusal from a crash. `cancelled` is included so this agrees with
    # `audit_events`: a turn abandoned mid-tool still made the call.
    "chemclaw_tool_calls_total": (
        "Tool invocations, by tool and outcome (ok / refused / error / cancelled)."
    ),
    "chemclaw_tool_refusals_total": (
        "Tool calls stopped by a governance gate, by reason (authz / dry_run / undeclared_write / "
        "plan_gate / repeat) — four of the five moved no metric at all before this."
    ),
    "chemclaw_invalid_tool_calls_total": (
        "Tool calls the model emitted with unparseable arguments, by tool. LangChain puts these on "
        "`AIMessage.invalid_tool_calls` rather than `tool_calls`, and nothing read that field — so "
        "the call vanished with no `tool_failed`, no `tool_result` and no trace of any kind. Such "
        "a call is now *promoted* onto `tool_calls` and refused by the tool chain, so it has an "
        "audit row, a span and a `tool_failed` like any other failing call; this counts how often "
        "the model mis-serialised one, which is the rate an operator alerts on and the tool "
        "metrics cannot show."
    ),
    "chemclaw_skill_reads_denied_total": (
        "Skill body reads refused by the role gate. The gate lives on the skills backend because "
        "that is the enforcement point, and a refusal there was entirely silent."
    ),
    "chemclaw_skill_loads_total": (
        "Skill bodies the model actually read, by skill — the other half of the denial counter "
        "above, and the only persisted signal that a skill is used at all. Before it, which "
        "procedure a turn opened was reconstructible from an INFO log line on a live pod and from "
        "nowhere else, so no skill could be ranked, promoted or retired on evidence. Counted on a "
        "skill body that was actually delivered — the read resolved, the path lies inside a skill "
        "directory rather than beside the tree, and lines were requested. The label needs all "
        "three because the skill name is the first segment of a model-written path and the "
        "visibility predicate only ever narrows, so an unconfigured deployment permits every "
        "string a model can invent, and `skills/README.md` resolves."
    ),
    "chemclaw_exhibit_writes_total": (
        "Artefact revisions written, by who wrote them (`agent` / `human`) and whether the write "
        "created the artefact or revised it (`created` / `revised`). The agent's share against the "
        "chemists' is the question the feature exists to answer — is it drafting what people then "
        "correct — and before this it was a log line on the REST path and nothing on the agent's."
    ),
    "chemclaw_exhibit_refusals_total": (
        "Artefact writes refused, by reason: `invalid` (a spec, binding, citation or cap the write "
        "broke, or any other refusal the writer can correct — a 422, or a worded refusal to the "
        "model), `stale_revision` (written against anything but the head), `exhibit_limit` (the "
        "session's or the artefact's cap) and `not_found` (an artefact the session does not "
        "hold). A climbing `invalid` with flat writes is a model that cannot write the shape it is "
        "offered."
    ),
    "chemclaw_behaviour_proposals_total": (
        "Proposed changes to what the agent does, by kind and by what became of them. The only "
        "answer to the question this queue exists to make answerable — is the agent proposing "
        "anything, and is anybody deciding? A deployment where `proposed` climbs and `accepted` "
        "and `rejected` stay flat has a queue nobody reads, which is worse than no queue: the "
        "agent is told its proposal is waiting and it is not. `superseded` is the system's own "
        "outcome rather than a person's, and the same text proposed again must not read as a fresh "
        "proposal either — `already_open` is the model repeating itself and `already_decided` "
        "is the "
        "idempotent path. The outcomes are `proposed`, `revived`, `already_open`, "
        "`already_decided`, `superseded`, `accepted` and `rejected` — not counted here, because a "
        "count in prose is a claim about its author's afternoon and this one was already wrong "
        "once. `revived` is a re-proposal of a *superseded* body, which is a genuine state change "
        "and booked `already_open` until it had its own label: the queue read as being repeated "
        "at while it was in fact being refilled."
    ),
    "chemclaw_local_skill_loads_total": (
        "Skill bodies a chemist's *own* tier delivered — the same question as the counter above, "
        "asked of the tier that counter cannot see. `chemclaw_skill_loads_total` lives on "
        "`NarrowedSkillsBackend`, and the personal tier is a `StoreBackend`, so a local skill load "
        "moved nothing at all: measured, a shipped skill and a personal one read through the same "
        "mount in one process left one series at 1 and the other absent. **Bare, and that is the "
        "whole reason it is a second series rather than a label.** A local skill's name is written "
        "by a person, clamped by nothing, and would be a per-chemist identifier minting a series "
        "per private project name in a shared exposition — the rule `Chemclaw3-mcp` states for its "
        "own fleet, which this repository had no occasion to state until a caller-named skill "
        "existed. What an operator needs from this is whether the tier is used at all, and a bare "
        "count answers it; who used which is a question for that person's own listing route."
    ),
    # --- the turn ------------------------------------------------------------------------------
    "chemclaw_turns_finished_total": (
        "Turns that ended, by outcome — the one series that separates `answered` from "
        "`loop_capped`, `empty_answer`, `errored`, `timed_out` and `abandoned`, which "
        "`turn_costs.completed` collapsed into a boolean — and `interrupted`, a turn whose own "
        "process died mid-turn, counted by whichever process noticed it."
    ),
    "chemclaw_turns_resumed_total": (
        "Turns whose pod died mid-turn and that their sender's reattach continued from the last "
        "checkpoint. Each is also counted once as started by the attempt that died."
    ),
    "chemclaw_turn_resume_refused_total": (
        "Dead turns that ended `interrupted` instead of being resumed, by reason, counted once "
        "when the turn is marked — `acted` is a turn with a call that is not on the repeatable "
        "list (a write, an unknown tool, a helper), the rest are a thread or checkpoint that does "
        "not support continuing."
    ),
    # --- the durable tier ----------------------------------------------------------------------
    # Completions by outcome, the counterpart to `chemclaw_jobs_started_total`, so a connector whose
    # every job fails is distinguishable from an idle one.
    "chemclaw_jobs_finished_total": (
        "Durable jobs that ended, by connector and outcome (completed / failed / cancelled) — the "
        "counterpart `chemclaw_jobs_started_total` never had."
    ),
    "chemclaw_activity_failures_total": (
        "Temporal activity attempts that failed, by activity — one row per attempt, so a retry "
        "storm is visible as a rate rather than only in the broker's own history."
    ),
    "chemclaw_worker_activities_cancelled_on_drain_total": (
        "In-flight activities cancelled because a worker's graceful-shutdown budget expired. Not "
        "lost — Temporal redelivers — but paid for twice, which is the cost `durable/serve.py` "
        "names and nothing measured."
    ),
    # --- the calculation cache ----------------------------------------------------------------- "A
    # persisted result is never recomputed" is the largest cost lever in the system; this shows it
    # working.
    "chemclaw_calc_cache_total": (
        "Calculation-cache lookups, by outcome (hit / shared / miss) — `shared` is a concurrent "
        "miss on one key that `cached_compute` single-flighted onto another caller's computation, "
        "in this process or in another (`chemclaw_calc_claims_total` separates the two)."
    ),
    # The value, not the count: compute seconds avoided, read from `StoredResult.compute_seconds` on
    # each hit. One avoided CREST search outweighs a thousand avoided lookups.
    "chemclaw_calc_cache_seconds_saved_total": (
        "Calculator wall-clock seconds a cache lookup did not have to spend — the "
        "`compute_seconds` of the stored result on a hit, and of the computation a `shared` "
        "waiter joined. Zero for a row that records no cost (a measured value, a backfill), never "
        "a fabricated zero. Read beside `chemclaw_calc_cache_total`: that one says how often the "
        "cache answered, this one says what it was worth."
    ),
    "chemclaw_calc_claims_total": (
        "Cross-process claims on a calculation miss, by outcome: `won` (this pod computes), "
        "`awaited` (another pod holds the claim; this one waits for its result), `taken_over` "
        "(this pod computes because the previous holder's lease lapsed or its attempt failed), "
        "`lost` (a holder's heartbeat found its claim replaced and finishes anyway), "
        "`peer_failed` (a waiter received the holder's failure) and `wait_timed_out` (a waiter's "
        "budget ran out while the holder was still working). A high `awaited` beside a flat "
        "`won` is the dedup working; `taken_over` is a crashed or stalled pod."
    ),
    # The backend refusing for capacity rather than bad data. Kept out of `chemclaw_degraded_total`
    # because a busy backend is ordinary operation; this is the calculation tier's saturation
    # signal.
    "chemclaw_calc_backend_at_capacity_total": (
        "Calculation-backend calls refused because every slot was busy, by tool. Retried with "
        "backoff rather than failed; a sustained rate means the backend is under-provisioned."
    ),
    # Tool calls routed through a connector's interactive queue (`connectors/queued.py`), by tool
    # (bounded by the manifests' `queued:` lists).
    "chemclaw_queued_tool_calls_total": (
        "Tool calls a turn sent through a connector's interactive queue, by tool."
    ),
    # A queued tool called directly because its run could not be started (broker unreachable).
    # Non-zero means bursts are met by refusals again.
    "chemclaw_queued_tool_calls_direct_total": (
        "Queued tool calls sent straight to the server because the queue was unreachable, by tool."
    ),
    # --- ingest and retrieval ------------------------------------------------------------------
    "chemclaw_ingest_records_total": (
        "Records seen by an ingest pass, by source and outcome (ingested / rejected / skipped)."
    ),
    "chemclaw_ingest_citation_only_total": (
        "Records an ingest pass stored in the citation-only tier, by source: a subset of "
        "`outcome=ingested` whose source named a species without its structure, so the record is "
        "citable and in no structure index."
    ),
    "chemclaw_evidence_source_kept_total": (
        "Chunks from each source that survived merge and the evidence budget. Read against "
        "`chemclaw_evidence_source_chunks_total`, which counts what a leg *handed over* before "
        "RRF and the cap — so a leg contributing 30 and surviving 0, which is exactly the state "
        "D-2026-08-01 was written about, still read as healthy on the pre-merge counter alone."
    ),
    "chemclaw_vector_unresolved_points_total": (
        "Ranked points an external vector store returned that no `document_chunks` row could "
        "resolve. Non-zero means the store and its catalogue have drifted, which otherwise "
        "presents as an honest zero-chunk answer from a healthy-looking leg."
    ),
    "chemclaw_embedding_calls_total": (
        "Calls to the configured embedding provider through core.embeddings, by outcome "
        "(ok / error). Not every embedding this deployment performs: a warehouse binding "
        "declaring vector: {embedding: server} embeds inside its own SQL and books nothing "
        "here — chemclaw_evidence_source_seconds{source} times that leg."
    ),
    "chemclaw_db_query_failures_total": (
        "Pooled database operations that failed, by kind (unavailable / cancelled / deadlock / "
        "error) — statement timeouts and serialization failures had no handler and no counter."
    ),
    "chemclaw_job_lock_skipped_total": (
        "Runs of a single-instance job that did nothing because another worker held its "
        "cluster-wide lock (`core/job_lock.py`), by job. Steady skips beside a job that never "
        "completes mean the holder is wedged, not that the work is being shared."
    ),
    "chemclaw_results_dead_lettered_total": (
        "Result publications retired to `failed` after exhausting their attempts. Distinct from "
        "`chemclaw_result_publish_failures_total`, which counts one row per *attempt*, so a "
        "permanent retirement was indistinguishable from a transient blip."
    ),
}

# Latency histogram buckets, in seconds. A turn (what a chemist waits on) and a tool call (what
# explains a slow turn) are different distributions, so each has its own set; finer detail is the
# trace pipeline's. Not a `Settings` field: buckets are part of a histogram's identity, so varying
# them per deployment breaks aggregation and history.
#
# `histogram_quantile` cannot interpolate into `+Inf`, so the top finite bucket must exceed
# `service_turn_timeout_seconds`. `_TURN_BUCKETS` brackets the timeout so saturation shows as mass
# moving into the timeout bucket rather than a quantile that stops moving.
_TURN_BUCKETS: tuple[float, ...] = (
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    45.0,
    60.0,
    90.0,
    120.0,
    180.0,
    300.0,
    450.0,
    600.0,
    900.0,
)
# Six orders of magnitude, as the tool surface spans: sub-millisecond skill loads up to
# `calc_server_timeout_seconds`.
_TOOL_BUCKETS: tuple[float, ...] = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    900.0,
)
# The same argument one tier up: brackets the job timeout ceiling on both sides so a deployment
# saturating it is visible as mass in the top bucket, while the low end stays fine-grained for
# cache-hit re-runs. `tests/test_durable_observability.py` asserts the bracket against the setting.
_JOB_BUCKETS: tuple[float, ...] = (
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1200.0,
    1800.0,
    3600.0,
    7200.0,
    14400.0,
    21600.0,
    25200.0,
    28800.0,
)
# A generic set for the histograms added since, whose range is "a network call": an embedding
# batch, a database statement, one delivery to a result sink, one model call.
_CALL_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)

_HISTOGRAMS: dict[str, str] = {
    # The only in-run proxy for judge quality: a judge preferring whichever side it saw first turns
    # the ranking into noise while every other signal stays green. 0.0 is no order effect; 1.0 is
    # the order deciding every pair.
    "chemclaw_hypothesis_position_bias": (
        "How far a tournament's judge preferred whichever hypothesis it was shown first."
    ),
    "chemclaw_turn_duration_seconds": "Wall-clock duration of one streamed agent turn.",
    "chemclaw_tool_duration_seconds": "Wall-clock duration of one tool invocation.",
    "chemclaw_http_request_duration_seconds": (
        "Wall-clock duration of one HTTP request, by route template."
    ),
    "chemclaw_model_call_duration_seconds": (
        "Wall-clock duration of one model call **made from a graph node** — the same population "
        "as `chemclaw_model_calls_total` and, for the same reason, not every request the gateway "
        "serves. An operator asking 'is the endpoint slow' is sampling the graph's traffic."
    ),
    "chemclaw_evidence_source_seconds": (
        "Wall-clock duration of one retrieval leg within an evidence sweep, by source."
    ),
    "chemclaw_embedding_duration_seconds": "Wall-clock duration of one embedding provider call.",
    "chemclaw_db_query_duration_seconds": (
        "Wall-clock duration of one borrowed database connection — the caller's whole "
        "db.connection() block, not one statement — by operation name. Pooled and dedicated "
        "alike. A call site that holds a connection across work of its own is measured doing "
        "exactly that, so this is hold time, not query latency."
    ),
    "chemclaw_job_duration_seconds": (
        "Wall-clock duration of one finished durable job, by connector — the distribution "
        "`chemclaw_job_runtime_seconds_total` deliberately does not give, so a p95 exists for the "
        "most expensive work in the system."
    ),
    "chemclaw_sink_delivery_seconds": "Wall-clock duration of one delivery to a result sink.",
    "chemclaw_calc_claim_wait_seconds": (
        "Wall-clock time a calculation miss spent waiting on another pod's claim, from the first "
        "sighting of the claim to the result, the holder's failure, a takeover or the wait budget. "
        "Dominated by the computation itself; the wake-up after the holder commits is "
        "sub-second."
    ),
    # How long a turn waited on the checkpointer lock. `asyncio.Lock` has no timeout, so this is the
    # only bound on that wait; nothing raises.
    "chemclaw_checkpointer_lock_wait_seconds": (
        "Wall-clock time one checkpointer statement spent waiting for the saver's lock."
    ),
}

#: Position bias is a fraction on [0, 1], so it gets linear buckets; the interesting region is the
#: top, where the order decides the judgement.
_FRACTION_BUCKETS: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9, 1.0)

_HISTOGRAM_BUCKETS: dict[str, tuple[float, ...]] = {
    "chemclaw_hypothesis_position_bias": _FRACTION_BUCKETS,
    "chemclaw_turn_duration_seconds": _TURN_BUCKETS,
    "chemclaw_tool_duration_seconds": _TOOL_BUCKETS,
    "chemclaw_job_duration_seconds": _JOB_BUCKETS,
    "chemclaw_calc_claim_wait_seconds": _JOB_BUCKETS,
    "chemclaw_http_request_duration_seconds": _CALL_BUCKETS,
    "chemclaw_model_call_duration_seconds": _CALL_BUCKETS,
    "chemclaw_evidence_source_seconds": _CALL_BUCKETS,
    "chemclaw_embedding_duration_seconds": _CALL_BUCKETS,
    "chemclaw_checkpointer_lock_wait_seconds": _CALL_BUCKETS,
    "chemclaw_db_query_duration_seconds": _CALL_BUCKETS,
    "chemclaw_sink_delivery_seconds": _CALL_BUCKETS,
}

# Histograms that carry labels, and the label names each accepts, enforced like `_COUNTER_LABELS`.
# Labels let latency be attributed per tool, route, source and sink. Every label is bounded by
# configuration or a source literal, never a caller's string; `route` is the FastAPI route template,
# never the raw path.
_HISTOGRAM_LABELS: dict[str, tuple[str, ...]] = {
    "chemclaw_tool_duration_seconds": ("tool",),
    "chemclaw_http_request_duration_seconds": ("route",),
    "chemclaw_evidence_source_seconds": ("source",),
    "chemclaw_db_query_duration_seconds": ("operation",),
    "chemclaw_job_duration_seconds": ("connector",),
    "chemclaw_sink_delivery_seconds": ("sink",),
}

# Counters that carry labels, and the label names each accepts. A counter absent here is unlabelled,
# pre-seeded to zero and rendered as one bare line. An undeclared label name raises, since a typo
# would otherwise create a silent second series.
#
# Only `profile`, not `model`: per-turn model attribution lives in the `turn_costs` ledger, and a
# second, lossier answer as a label would be another system to reconcile.
_COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    # `completed`, `unseparated` (ranked inside its own uncertainty — the common real answer),
    # `unrated` (ranking is the prior) and `empty`. Only `unrated` means the judge never answered.
    "chemclaw_hypothesis_tournaments_total": ("outcome",),
    "chemclaw_hypothesis_screen_rejections_total": ("rule",),
    "chemclaw_hypothesis_check_refusals_total": ("code",),
    "chemclaw_tokens_total": ("profile",),
    "chemclaw_input_tokens_total": ("profile",),
    "chemclaw_output_tokens_total": ("profile",),
    "chemclaw_cache_read_tokens_total": ("profile",),
    "chemclaw_cache_write_tokens_total": ("profile",),
    # The same `profile` label as the four measured series, so measured and inferred spend can be
    # summed over the same axis and compared without a join.
    "chemclaw_estimated_tokens_total": ("profile",),
    # Bounded by `CHEMCLAW_DELIVERY_CHANNELS` — a deployment's own list of channel folder names,
    # never a caller's string. Same rule as every label here.
    "chemclaw_deliveries_total": ("channel",),
    # A source name is an operator-configured registry entry, never a caller's string. Attachment
    # evictions carry no label: the only candidate, a session id, is unbounded.
    "chemclaw_ingest_rejections_evicted_total": ("source",),
    "chemclaw_delivery_failures_total": ("channel",),
    # Source literals only: `end` is `ask`/`answer` and `reason` one of `BrokenPremise`'s three.
    # Never a note id.
    "chemclaw_premise_refusals_total": ("end", "reason"),
    # Source literals in `api/runner._escalate_for_review`: `opened`, `joined`, `no_claims`,
    # `no_actor`, `unavailable`. Session and actor stay in the log line.
    "chemclaw_answer_review_escalations_total": ("outcome",),
    # Fixed by `agent/condense.py`'s `DigestSource` literal: `extracted`, `degraded`, `oversized`.
    "chemclaw_protocol_digests_total": ("outcome",),
    # Bounded by configuration: a connector is a bundle the chart enables, never a caller's string.
    "chemclaw_job_runtime_seconds_total": ("connector",),
    # What was busy: this process's slot, another replica's durable claim, or a line already
    # occupied.
    "chemclaw_turns_conflict_total": ("scope",),
    # Bounded by the registered tool surface, which is configuration (the enabled connectors and
    # profile) rather than anything a caller can name.
    "chemclaw_repeated_tool_calls_total": ("tool",),
    # A tool name the graph dispatched, never a string a caller invented.
    "chemclaw_tool_results_truncated_total": ("tool",),
    # A template's filename in `data/templates/`, bounded by what a deployment ships.
    "chemclaw_template_prompt_truncated_total": ("template",),
    # A retriever's registry `name` (the knowledge graph, lexical and dense indexes, fingerprint
    # store).
    "chemclaw_evidence_source_chunks_total": ("source",),
    # The same `source` label, so a source contributing nothing can be joined against a source
    # raising and a dark leg told from a broken one.
    "chemclaw_evidence_source_failures_total": ("source",),
    # The decline counter completes the triple: chunks, failures, skips, one `source` label each,
    # so the three series join on the same key.
    "chemclaw_evidence_source_skips_total": ("source",),
    # A string literal at each `degraded()` call site, so the set is enumerable from source;
    # `tests/test_degraded.py` enumerates it across call spellings and argument forms.
    "chemclaw_degraded_total": ("subsystem",),
    # Bounded by this module's own declarations: the label domain is `declared_metric_names()`.
    "chemclaw_gauge_read_failures_total": ("metric",),
    # The metric that hit the cardinality cap: one of the declared names.
    "chemclaw_metric_series_dropped_total": ("metric",),
    # The route template from `request.scope["route"].path`, bounded by `app.routes`. Never
    # `request.url.path`, which is caller-controlled and unbounded.
    "chemclaw_http_requests_total": ("route", "status_class"),
    "chemclaw_request_validation_failures_total": ("route",),
    # Four literals in `api/deps.py`, four in `api/auth.py` — source-fixed, like `subsystem`.
    "chemclaw_authz_refusals_total": ("resource",),
    "chemclaw_auth_failures_total": ("reason",),
    # Outcome only: there is one gateway endpoint, and per-model spend lives in `turn_costs`.
    "chemclaw_model_calls_total": ("outcome",),
    "chemclaw_tool_calls_total": ("tool", "outcome"),
    # A directory name under a configured skills tree, clamped by the read having succeeded rather
    # than by the visibility predicate, which is inert in a deployment that configures no gate.
    "chemclaw_behaviour_proposals_total": ("kind", "outcome"),
    "chemclaw_skill_loads_total": ("skill",),
    # Closed sets fixed in source (`exhibits/telemetry.py`'s `WriteOp` and `RefusalReason`, the
    # store's `AuthorKind`); artefact ids and sessions stay on the `exhibit.*` log line.
    "chemclaw_exhibit_writes_total": ("author_kind", "op"),
    "chemclaw_exhibit_refusals_total": ("reason",),
    "chemclaw_tool_refusals_total": ("reason",),
    "chemclaw_invalid_tool_calls_total": ("tool",),
    "chemclaw_turns_finished_total": ("outcome",),
    # A closed set fixed in source: `agent/turn_resume.Refusal`.
    "chemclaw_turn_resume_refused_total": ("reason",),
    "chemclaw_jobs_finished_total": ("connector", "outcome"),
    "chemclaw_activity_failures_total": ("activity",),
    # A bundle name from the connector registry, matching the `chemclaw_connector_unhealthy` gauge's
    # label, so alerts can say which connector went dark.
    "chemclaw_connectors_unreachable_total": ("connector",),
    "chemclaw_calc_cache_total": ("outcome",),
    "chemclaw_calc_claims_total": ("outcome",),
    "chemclaw_calc_backend_at_capacity_total": ("tool",),
    "chemclaw_queued_tool_calls_total": ("tool",),
    "chemclaw_queued_tool_calls_direct_total": ("tool",),
    "chemclaw_ingest_records_total": ("source", "outcome"),
    "chemclaw_ingest_citation_only_total": ("source",),
    "chemclaw_evidence_source_kept_total": ("source",),
    "chemclaw_embedding_calls_total": ("outcome",),
    "chemclaw_db_query_failures_total": ("kind",),
    "chemclaw_job_lock_skipped_total": ("job",),
}

# The most label sets one counter may hold. Label values come from configuration and possibly
# provider responses, so an unbounded map would leak; past the cap a new series is refused and
# reported once. Sized for the largest legitimate domain, `chemclaw_http_requests_total` over the
# route table, with room to double (`tests/test_api_observability.py` reports the current worst
# case). Any other metric reaching it is generating values it should not.
_MAX_SERIES_PER_COUNTER = 256

_GAUGES: dict[str, str] = {
    "chemclaw_turns_in_flight": "Turns currently streaming.",
    "chemclaw_egress_guard_armed": "1 when the in-process egress guard is installed, else 0.",
    # Whether the compiled `LD_PRELOAD` interposer (`netguard_preload.c`) is loaded, separate from
    # the Python guard: sockets opened below the interpreter (gRPC, the OTLP exporter, the Temporal
    # client) bypass the Python guard. Read from the dynamic linker, never from `LD_PRELOAD`,
    # because a preload naming a missing path is silently ignored.
    "chemclaw_egress_preload_armed": (
        "1 when the LD_PRELOAD egress interposer is loaded into this process, else 0."
    ),
    # A blocked lookup (a name nothing declared) and a blocked dial (an address nothing resolved to)
    # are different events with different next steps. Gauges because the counting happens in C
    # atomics; the scrape reads them directly.
    "chemclaw_egress_preload_refused_connect": (
        "Dials (connect/sendto/sendmsg) the LD_PRELOAD egress interposer refused in this process."
    ),
    "chemclaw_egress_preload_refused_resolve": (
        "Name lookups the LD_PRELOAD egress interposer refused in this process."
    ),
    "chemclaw_turn_capacity": "Configured maximum concurrent turns (the admission cap).",
    # Published so an unset fairness cap reads as an explicit 0 rather than as an absence of
    # refusals.
    "chemclaw_turn_actor_capacity": (
        "Configured maximum concurrent turns one actor may hold on this process (0 = disabled)."
    ),
    # `sum()` of the in-flight gauge across pods against this declared ceiling catches what config
    # validation cannot see: a hand-scaled Deployment or an edited HPA. 0 when undeclared, which
    # disables the alert.
    "chemclaw_fleet_turn_ceiling": "Declared fleet-wide ceiling on concurrent turns (0 = none).",
    "chemclaw_live_sessions": "Sessions held in the front door's in-process LRU.",
    # `billed / estimated` tokens, smoothed over this process's calls: the chars/4 estimate and the
    # provider's tokenizer disagree by content type. `agent/context_budget.effective_trigger`
    # divides the configured budget by it; 1.0 means not enough calls observed yet, and nothing is
    # adjusted.
    "chemclaw_context_estimator_ratio": (
        "Billed input tokens divided by this system's own estimate of the same request (1.0 = "
        "not yet calibrated)."
    ),
    # Out-of-process capability can fail independently of the chat service, so its reachability
    # is a first-class signal rather than something to find in a log (`connectors.health`).
    "chemclaw_connectors_unhealthy": "Enabled connectors that could not be reached (0 = all up).",
    # Age of the synced knowledge corpus. The sync loop swallows a failed refresh, so a pod can
    # serve a frozen graph; read from the volume by the serving process (`kg/graph.py`), needing no
    # sidecar.
    "chemclaw_knowledge_sync_age_seconds": (
        "Age of the newest note on this pod's knowledge tree, in seconds (-1 = the tree holds no "
        "note at all). Measures what this pod knows, not when the sync last ran."
    ),
    # Pool saturation: `requests_waiting` above zero means `pg_pool_max_size` is too small for the
    # load, which otherwise shows only as connect timeouts against an idle database.
    "chemclaw_pg_pool_size": "Connections held across this process's Postgres pools.",
    "chemclaw_pg_pool_available": "Pooled connections currently idle and available.",
    "chemclaw_pg_pool_requests_waiting": "Callers blocked waiting for a pooled connection.",
    # The checkpointer's own queue: `AsyncPostgresSaver._cursor` serialises every statement on one
    # `asyncio.Lock` before touching the pool, so pool gauges read idle during a checkpointer stall.
    "chemclaw_checkpointer_statements_waiting": (
        "Turns queued on the checkpointer's own lock, waiting to run one statement. Above zero "
        "means the pod is serializing turn-state writes, which no pool metric can show."
    ),
    # The connection budget's two sides: `sum()` of per-process maxima against the server's declared
    # ceiling catches scaling that config validation cannot see. 0 when undeclared, disabling the
    # alert.
    "chemclaw_pg_pool_max_size": "This process's configured maximum pooled connections.",
    "chemclaw_pg_fleet_max_connections": (
        "Declared fleet-wide ceiling on Postgres connections (0 = none)."
    ),
    # The session server's ceiling, when `session_store_dsn` points front-door and checkpointer
    # pools at another server. 0 when there is no split.
    "chemclaw_pg_session_fleet_max_connections": (
        "Declared ceiling on Postgres connections at a split session store (0 = none)."
    ),
    # The pools aimed at the session server, so each server is checked against its own ceiling; a
    # sum against a sum of ceilings can miss one server being over. A separate gauge rather than a
    # label, so it stays a pure configuration reading. Which pools land here uses the same
    # `pg_endpoint` comparison as `fleet_connections_per_server` in `core/config`.
    "chemclaw_pg_session_pool_max_size": (
        "This process's maximum pooled connections that land on a split session store's own "
        "server (0 = no split)."
    ),
    # The calculation backend's admission budget. The left side is live rather than configured,
    # since calc workers and the bundle's MCP server pods dispatch under different (or no) local
    # caps; `sum()` across pods is what `servers/calc` is asked to serve.
    "chemclaw_calc_requests_in_flight": (
        "Calculation-backend sessions this process is currently holding open."
    ),
    "chemclaw_calc_backend_max_concurrent_requests": (
        "Declared ceiling on concurrent calculation-backend requests (0 = none)."
    ),
    # The event-stream cap's two sides, so "near the per-pod cap" is visible before rejections
    # start.
    "chemclaw_event_streams_open": "Push-back event streams currently open on this process.",
    "chemclaw_event_stream_capacity": "Configured maximum concurrent push-back streams.",
    # In-flight durable work, deployment-wide and published identically by every worker: take
    # `max()`, never `sum()`. Read from the broker's visibility count (`durable/job_metrics.py`),
    # because launches and completions are booked in different processes and cannot be subtracted
    # per process.
    "chemclaw_jobs_in_flight": "Durable jobs open in this deployment (the same reading per pod).",
}

# Gauges that carry one label, read as a whole family from a single callable returning `{label
# value: reading}`: outbox backlog per sink, ingest lag per source, connector health per connector.
_GAUGE_FAMILIES: dict[str, str] = {
    "chemclaw_outbox_pending": "Result publications queued and not yet delivered, by sink.",
    # Separates a fast-turning backlog from a stuck one. `min(enqueued_at)` over the partial index
    # `result_publications_pending` is an index-only scan, cheap per scrape.
    "chemclaw_outbox_oldest_pending_seconds": (
        "Age of the oldest undelivered result publication, by sink."
    ),
    "chemclaw_outbox_dead_lettered": (
        "Result publications retired to `failed`, by sink. These never leave the "
        "queued-minus-published difference, which is why that difference is not a backlog."
    ),
    "chemclaw_ingest_cursor_lag_seconds": (
        "How far behind its source each ingest cursor is, in seconds. A source whose fetch has "
        "wedged advances no cursor and logs `ingested=0`, which is what a quiet source also does."
    ),
    # The prefix share `tests/test_context_floor.py` cannot ratchet: endpoint tool schemas arrive
    # from the MCP server at handshake. Measured there, by connector; its `sum()` plus the ratcheted
    # floor is what a turn pays before the chemist says anything.
    "chemclaw_connector_tool_schema_tokens": (
        "Estimated tokens of bound tool schema advertised by each connector at handshake."
    ),
    # Whether the store is filling: `pg_total_relation_size` per table in `durable/retention.py`'s
    # register (swept or not), read once per retention pass rather than per scrape. A gauge only:
    # rows deleted are already in `RetentionOutcome`, and reclaimed bytes are structurally near
    # zero. The query an operator runs is `topk(5, chemclaw_table_bytes)`.
    "chemclaw_table_bytes": (
        "Total relation size of each durable table in bytes, as of the last retention pass "
        "(heap, indexes and TOAST — `pg_total_relation_size`)."
    ),
    "chemclaw_connector_unhealthy": (
        "1 per enabled connector that could not be reached, by connector. The unlabelled "
        "`chemclaw_connectors_unhealthy` says how many; this says which, which is the half "
        "`open_reachable` had in hand and discarded."
    ),
}

_GAUGE_FAMILY_LABELS: dict[str, str] = {
    "chemclaw_table_bytes": "table",
    "chemclaw_outbox_pending": "sink",
    "chemclaw_outbox_oldest_pending_seconds": "sink",
    "chemclaw_outbox_dead_lettered": "sink",
    "chemclaw_ingest_cursor_lag_seconds": "source",
    "chemclaw_connector_unhealthy": "connector",
    "chemclaw_connector_tool_schema_tokens": "connector",
}


def declared_metric_names() -> frozenset[str]:
    """Every metric name this registry declares — counters, histograms and gauges together.

    Public because metric names are cited outside this process (runbook, ADRs) and `make
    prose-validate` resolves those citations against this set. One function so a new metric kind
    cannot escape it.
    """
    return (
        frozenset(_COUNTERS)
        | frozenset(_HISTOGRAMS)
        | frozenset(_GAUGES)
        | frozenset(_GAUGE_FAMILIES)
    )


def declared_histogram_names() -> frozenset[str]:
    """Just the histograms, for readers that must reason about their derived series.

    PromQL cites the derived `_bucket`, `_sum` and `_count` names; a reader folding those suffixes
    back needs the exact histogram set, or it would accept them on any metric.
    """
    return frozenset(_HISTOGRAMS)


class Metrics:
    """A tiny, thread-safe counter/gauge/histogram registry rendering Prometheus exposition text.

    Gauges are read through callables so they cannot drift from the structure they describe;
    counters and histograms are accumulated.
    """

    def __init__(self) -> None:
        """Start with every declared counter at zero and no gauge sources bound."""
        self._lock = threading.Lock()
        self._counts: dict[str, float] = dict.fromkeys(_COUNTERS, 0.0)
        self._gauges: dict[str, Callable[[], float]] = {}
        self._gauge_families: dict[str, Callable[[], Mapping[str, float]]] = {}
        # Per histogram, per label set (`()` when unlabelled): one tally per bucket, an overflow
        # slot, and the running sum; cumulative counts are derived at render time. Only unlabelled
        # histograms are pre-seeded, since an invented zero series is indistinguishable from an
        # observed one.
        self._histograms: dict[str, dict[tuple[tuple[str, str], ...], list[float]]] = {
            name: (
                {(): [0.0] * (len(_HISTOGRAM_BUCKETS[name]) + 1)}
                if name not in _HISTOGRAM_LABELS
                else {}
            )
            for name in _HISTOGRAMS
        }
        self._histogram_sums: dict[str, dict[tuple[tuple[str, str], ...], float]] = {
            name: ({(): 0.0} if name not in _HISTOGRAM_LABELS else {}) for name in _HISTOGRAMS
        }
        # Labelled series, per counter, keyed by sorted label pairs. Not pre-seeded: a series exists
        # once observed, the Prometheus convention.
        self._series: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._capped: set[str] = set()

    def increment(
        self, name: str, amount: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a declared counter. An undeclared name or label is a programming error, so raises.

        Binding in both directions: a counter in `_COUNTER_LABELS` must be incremented with its
        labels, and one absent without any. A bare sample beside labelled ones would be read as
        another series and double-count under `sum()`.
        """
        if name not in _COUNTERS:
            raise KeyError(f"undeclared counter {name!r}")
        given = dict(labels or {})
        declared = _COUNTER_LABELS.get(name, ())
        if set(given) != set(declared):
            raise KeyError(
                f"counter {name!r} takes label(s) {sorted(declared)}, got {sorted(given)}"
            )
        if not declared:
            with self._lock:
                self._counts[name] += amount
            return
        key = tuple(sorted((label, str(value)) for label, value in given.items()))
        with self._lock:
            series = self._series.setdefault(name, {})
            if key not in series and len(series) >= _MAX_SERIES_PER_COUNTER:
                self._note_series_cap(name)
                return
            series[key] = series.get(key, 0.0) + amount

    def _note_series_cap(self, name: str) -> None:
        """Record that `name` hit the series cap, labelled with it. Caller holds the lock.

        Written straight into `_series` rather than via `increment`: re-entering would deadlock on
        the plain lock, and bypassing the cap check keeps this counter recording after others stop.
        Its label domain is the declared metric names, so it needs no cap.
        """
        key = (("metric", name),)
        series = self._series.setdefault("chemclaw_metric_series_dropped_total", {})
        series[key] = series.get(key, 0.0) + 1.0
        if name not in self._capped:
            self._capped.add(name)
            log.warning(
                "metric %s reached %d label sets; further series are dropped. A label value here "
                "is meant to be low-cardinality (a profile, a tool, a route template), so this "
                "means something is generating values it should not.",
                name,
                _MAX_SERIES_PER_COUNTER,
            )

    def bind_gauge(self, name: str, source: Callable[[], float]) -> None:
        """Bind a gauge to a live source; reading it always reflects current state."""
        if name not in _GAUGES:
            raise KeyError(f"undeclared gauge {name!r}")
        with self._lock:
            self._gauges[name] = source

    def bind_gauge_family(self, name: str, source: Callable[[], Mapping[str, float]]) -> None:
        """Bind a one-label gauge family to a live source returning `{label value: reading}`.

        Read on every scrape; a source that raises omits its family and increments
        `chemclaw_gauge_read_failures_total` rather than failing the response.
        """
        if name not in _GAUGE_FAMILIES:
            raise KeyError(f"undeclared gauge family {name!r}")
        with self._lock:
            self._gauge_families[name] = source

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one latency sample. An undeclared name or label is a programming error, so raises.

        Binding in both directions, as for `increment`: a bare sample beside labelled ones would mix
        a duplicate of the whole into `histogram_quantile` over `sum by (le)`.
        """
        if name not in _HISTOGRAMS:
            raise KeyError(f"undeclared histogram {name!r}")
        given = dict(labels or {})
        declared = _HISTOGRAM_LABELS.get(name, ())
        if set(given) != set(declared):
            raise KeyError(
                f"histogram {name!r} takes label(s) {sorted(declared)}, got {sorted(given)}"
            )
        key = tuple(sorted((label, str(value)) for label, value in given.items()))
        boundaries = _HISTOGRAM_BUCKETS[name]
        # `bisect_left` puts a sample on a boundary into that bucket, matching `le` (less than or
        # equal); past the last boundary it lands in the `+Inf` overflow slot.
        index = bisect_left(boundaries, seconds)
        with self._lock:
            series = self._histograms[name]
            if key not in series:
                if len(series) >= _MAX_SERIES_PER_COUNTER:
                    self._note_series_cap(name)
                    return
                series[key] = [0.0] * (len(boundaries) + 1)
                self._histogram_sums[name][key] = 0.0
            series[key][index] += 1.0
            self._histogram_sums[name][key] += seconds

    def value(self, name: str) -> float:
        """A counter's total across every label set (tests assert on this, not on the text).

        Summed, as Prometheus aggregates server-side; per-series reads are a query concern.
        """
        with self._lock:
            return self._counts[name] + sum(self._series.get(name, {}).values())

    def observations(self, name: str) -> tuple[int, float]:
        """A histogram's `(count, sum)` across every label set.

        Summed, for the same reason as `value()`.
        """
        with self._lock:
            count = sum(sum(buckets) for buckets in self._histograms[name].values())
            return int(count), sum(self._histogram_sums[name].values())

    def render(self) -> str:
        """Render the Prometheus text exposition format (one HELP/TYPE/value block per metric)."""
        with self._lock:
            counts = dict(self._counts)
            gauges = dict(self._gauges)
            histograms = {
                name: {key: list(buckets) for key, buckets in series.items()}
                for name, series in self._histograms.items()
            }
            histogram_sums = {name: dict(sums) for name, sums in self._histogram_sums.items()}
            families = dict(self._gauge_families)
            series = {name: dict(values) for name, values in self._series.items()}
        lines: list[str] = []
        for name, help_text in _COUNTERS.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} counter"]
            if name not in _COUNTER_LABELS:
                lines.append(f"{name} {_sample(counts[name])}")
                continue
            # A labelled counter emits one line per observed series and never a bare one; an
            # unobserved one is absent rather than zero.
            for key, total in sorted(series.get(name, {}).items()):
                rendered = ",".join(f'{label}="{_escape(value)}"' for label, value in key)
                lines.append(f"{name}{{{rendered}}} {_sample(total)}")
        for name, help_text in _GAUGES.items():
            source = gauges.get(name)
            if source is None:
                # A gauge whose source is not bound is omitted rather than reported as 0 — a
                # fabricated zero would be indistinguishable from a genuinely idle service.
                continue
            # One failing source must not take the scrape down: a raising callable (for example
            # `pool.get_stats()` during shutdown) would otherwise turn `/metrics` into a 500 during
            # the very incident. The gauge is omitted and the failure counted.
            try:
                reading = float(source())
            except Exception:
                self.increment("chemclaw_gauge_read_failures_total", labels={"metric": name})
                log.warning("gauge %s could not be read; it is absent from this scrape", name)
                continue
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name} {_sample(reading)}",
            ]
        for name, help_text in _GAUGE_FAMILIES.items():
            family = families.get(name)
            if family is None:
                continue
            label = _GAUGE_FAMILY_LABELS[name]
            try:
                readings = dict(family())
            except Exception:
                self.increment("chemclaw_gauge_read_failures_total", labels={"metric": name})
                log.warning("gauge %s could not be read; it is absent from this scrape", name)
                continue
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
            for value, reading in sorted(readings.items()):
                lines.append(f'{name}{{{label}="{_escape(str(value))}"}} {_sample(float(reading))}')
        for name, help_text in _HISTOGRAMS.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
            boundaries = _HISTOGRAM_BUCKETS[name]
            for key, buckets in sorted(histograms[name].items()):
                # The label pairs for a bucket line; `le` must sit in the same brace group, hence a
                # prefix.
                declared = "".join(f'{label}="{_escape(value)}",' for label, value in key)
                suffix = "".join(f'{label}="{_escape(value)}"' for label, value in key)
                braced = f"{{{suffix}}}" if suffix else ""
                # Buckets are cumulative ("samples <= le"); the final `+Inf` bucket equals the
                # count.
                cumulative = 0.0
                # `le` keeps `:g` deliberately: it is a label, so re-spelling `3600` as `3600.0`
                # would mint new series. Boundaries are small human-chosen numbers, and `+Inf` is
                # spelled literally below.
                for boundary, tally in zip(boundaries, buckets[:-1], strict=True):
                    cumulative += tally
                    lines.append(
                        f'{name}_bucket{{{declared}le="{boundary:g}"}} {_sample(cumulative)}'
                    )
                cumulative += buckets[-1]  # the overflow slot: samples past the last boundary
                lines += [
                    f'{name}_bucket{{{declared}le="+Inf"}} {_sample(cumulative)}',
                    f"{name}_sum{braced} {_sample(histogram_sums[name][key])}",
                    f"{name}_count{braced} {_sample(cumulative)}",
                ]
        return "\n".join(lines) + "\n"


# The process-wide registry: a scrape targets a process, and code deep in the call tree must be able
# to count without a registry threaded down to it.
METRICS = Metrics()

# Exposition content type, per the Prometheus text format spec. Kept beside the renderer so the
# route and the format cannot disagree.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
