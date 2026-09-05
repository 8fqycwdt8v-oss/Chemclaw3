"""Process metrics in Prometheus text format (gap DEP-4).

Observability was structured logs plus opt-in OTel *traces*. There was no metrics surface at all,
which left three things invisible in operations and one thing actively mis-tuned:

- **Load shedding is silent.** Admission control (AG-15) sheds excess turns with a 503 and the
  budget guard refuses with a 429. Both are working as designed and neither is countable, so
  "the service is at capacity" looks identical to "the service is fine" from outside.
- **Lost audit records are silent.** `chemclaw.agent.audit` deliberately swallows a sink
failure to
  keep tool calls working (SEC-3) and logs an ERROR marker — but nothing counts it, so an audit
  trail can be quietly incomplete for a long time.
- **The HPA scales on the wrong signal.** `values.yaml` autoscales the front door on CPU at 70%,
  which for an SSE-streaming, LLM-latency-dominated service is close to noise: a pod blocked on
  the model uses almost no CPU while being completely full. In-flight turns against the admission
  cap is the signal that actually describes saturation.

**No new dependency.** `prometheus_client` would be one more package to install, scan, and pin for
what is ~80 lines of text formatting. The exposition format is a stable, trivially-generated text
protocol, and this module is the only place that knows it.

Metrics are process-wide (one registry per pod), because that is the scope a scrape targets — and
that is why this is kernel material rather than front-door material. It imports only the standard
library, *every* process has something to count (the front door, the background worker, each
connector worker), and each of those reads it through `core/worker_http.py` or `core/logging.py`'s
neighbours rather than through anything in `chemclaw.api`. It lived in `chemclaw.api` until the R2
layering move, which is what forced `core/metrics_bridge.py` and `core/worker_http.py` to import it
lazily; both are ordinary imports now.

**This is not the eval layer's metrics, and there are three files in that family.**
`evals/metric.py` is the `@metric` decorator and registry for scored eval criteria;
`evals/metrics.py` holds the seed criteria themselves. This one counts turns, tokens, jobs and
refusals for an operator, and shares nothing with them but a word.

This module used to say histograms belonged in the OTel trace pipeline rather than here. That was
wrong twice over: `api/app.py` never called `configure_telemetry`, so `CHEMCLAW_OTEL_ENABLED`
did nothing at the front door and there was no latency signal at all; and traces are sampled and
per-request, so they cannot answer "what is p95 right now" for an alert or an autoscaler. A load
test had to derive turn latency from the client side because the server exposed none. So there are
now two histograms, and the trace pipeline keeps the per-request detail they deliberately drop.
"""

import logging
import threading
from bisect import bisect_left
from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)


def _escape(value: str) -> str:
    r"""Escape a label value for the exposition format.

    Prometheus requires `\\`, `"` and newline escaped inside a label value. A profile name will
    never contain one, which is exactly why it is done here rather than trusted: the escape is a
    property of the format, and the next label to be declared may not be so well behaved.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# Metric name -> help text. Declared up front so every metric is documented at its definition and
# the exposition always carries HELP/TYPE lines (a scrape without them is much harder to read).
_COUNTERS: dict[str, str] = {
    "chemclaw_turns_started_total": "Turns admitted and started.",
    "chemclaw_egress_refused_total": "Outbound connections the in-process egress guard refused.",
    # Per-source evidence accounting (M10). A leg that returns nothing on one query is normal; a
    # leg that returns nothing on *every* query is a broken deployment, and before this there was
    # no way to tell those apart from outside — which is exactly the blind spot
    # D-2026-08-01-a-cap-that-starves-a-source was found in, late and by hand.
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
    # "emitted", not "ended in": a turn stopped by the harness loop's iteration cap emits an error
    # event and then still delivers its partial answer, so it is counted here and, more precisely,
    # by `chemclaw_turn_loop_caps_total` below.
    "chemclaw_turns_failed_total": "Turns that emitted an error event.",
    # The two halves of a contended front door, and they mean different things: queueing is the
    # system absorbing a burst, shedding is it declining one. A rising queue rate with a flat shed
    # rate is capacity being used; both rising together is capacity being exceeded. Since D-166
    # both are reported on the turn's own stream, so neither has an HTTP status left to be counted
    # by at the load balancer.
    "chemclaw_turns_queued_total": (
        "Turns that had to wait for an admission permit (a `queued` event was streamed)."
    ),
    "chemclaw_turns_shed_total": (
        "Turns ended by the admission timeout because no permit ever freed (D-166: an error "
        "event on an open stream, previously an HTTP 503)."
    ),
    "chemclaw_turns_refused_budget_total": "Turns refused with 429 by the turn/token budget.",
    "chemclaw_turns_conflict_total": "Turns rejected with 409 (a turn was already running).",
    "chemclaw_turn_timeouts_total": "Turns cancelled by the wall-clock turn timeout.",
    # A separate series from the one above, and deliberately so: this counts turns cut because the
    # *client* stopped reading (`service_sse_send_timeout_seconds`), not because the turn ran long.
    # Two populations in one counter is a denominator nobody can interpret.
    "chemclaw_turn_send_timeouts_total": (
        "Turn streams closed because a client stopped reading past the SSE send timeout."
    ),
    # The wall-clock timeout's sibling, and the reason it needed one: a turn stopped by the
    # harness loop's iteration cap used to return normally and emit nothing, so the runaway guard
    # firing was invisible to everything outside the process. A rising rate here is an agent that
    # keeps planning more work than a turn can close — a prompt or skill problem, not an outage.
    "chemclaw_turn_loop_caps_total": "Turns stopped by the harness loop's iteration cap.",
    # The same guard in the unit that costs money. The iteration cap above bounds how many times a
    # turn thinks; this bounds what it bills, and the two move independently — a turn that fans out
    # wide over large results reaches this one inside a handful of iterations, and a turn that
    # plans in circles reaches that one having billed almost nothing. A rising rate here is a
    # deployment whose turns are too expensive rather than too long, which is a retrieval or
    # tool-result-size problem; flat at zero while `turn_costs` shows large turns means the cap is
    # unset (`agent_max_turn_billed_tokens` ships at 0) rather than never reached.
    "chemclaw_turn_spend_caps_total": "Turns stopped by the per-turn billed-token cap.",
    # The detach/stop split (D-2026-08-27-a-disconnect-is-a-detach-not-a-stop). A disconnect no
    # longer cancels a turn, so these two are what tell an operator how often clients drop away
    # mid-turn (the turn completed unwatched, billed whole) versus how often someone actually
    # pressed Stop. A rising detach rate with a flat stop rate is a flaky network or a tab-closing
    # habit, not dissatisfaction with answers.
    "chemclaw_turns_detached_total": (
        "Turns whose client disconnected mid-run and that continued to completion detached."
    ),
    "chemclaw_turns_stopped_total": "Turns cancelled by the explicit stop route.",
    # A plan that could not be read at all, as distinct from a session proposing none. Alertable
    # because it is the one state in which a one-shot approval is left unspent (`agent/plan_gate.py`
    # says what that would cost if it passed silently as "no plan").
    # Distinct from the cap above, and the distinction is the point: a capped turn *has* an answer
    # and is marked partial, while this one produced no prose at all. Counted because the shape is
    # invisible in every other signal — a live turn made 29 tool calls and emitted an empty answer
    # with no error, and only a count makes that a trend anyone can watch rather than an anecdote.
    "chemclaw_turn_empty_answers_total": "Turns that ended without producing any answer text.",
    # The result-publication path (D-2026-08-25). Three counters and a gauge, because the three
    # failure modes are genuinely different and one series carrying all of them would be
    # unactionable: a record that could not be *queued* is a local database problem, a record that
    # could not be *delivered* is the external store's, and a queue that is growing is neither
    # failing nor working. `chemclaw_results_queued_total` against
    # `chemclaw_results_published_total` is what says whether the drain is keeping up.
    "chemclaw_results_queued_total": (
        "Computed results projected and queued for an external results store."
    ),
    "chemclaw_results_published_total": (
        "Computed results confirmed durable at an external results store."
    ),
    "chemclaw_result_publish_failures_total": (
        "Result publications that could not be queued or delivered."
    ),
    # The fourth, and it is a different question from the three above: those are about a
    # *destination* (down, slow, refusing), this one is about this release's own code. A projector
    # that raises does so for every payload of that shape, forever, until someone changes the
    # projection — so folding it into the counter above made a permanent gap read as a transient
    # publish failure. Measured: `_microstate_pka` emitted three properties the registry did not
    # define, so `to_canonical` raised on **every** microstate pKa (two CREST metadynamics
    # searches, minutes to hours) and every one of them was dropped at the enqueue behind a series
    # that also rises when a warehouse is busy.
    "chemclaw_result_projection_failures_total": (
        "Stored payloads this release could not project into a record (a code gap, not an outage)."
    ),
    "chemclaw_audit_sink_failures_total": (
        "Audit records that could not be persisted (the trail is incomplete)."
    ),
    # The durable subsystem's counterpart to `chemclaw_connectors_unreachable_total`. It did not
    # exist, and a comment in `api/runner.py` asserted that the connector counter covered this —
    # it reads `tool.is_connected` over connector tools and never names Temporal, so a broker
    # outage produced no server-side signal at all at the shipped log level.
    # **Turns, not requests, and Temporal, not "a subsystem".** Both halves were briefly untrue: a
    # second increment sat in `api/middleware._subsystem_unavailable`, which fires per *HTTP
    # request* for any `SubsystemUnavailableError` — a family that includes `DocumentIndexError`,
    # i.e. a pgvector failure with no Temporal in it. One series carried two populations with two
    # different denominators, and `ChemclawDurableUnreachable` alerted on their sum while its
    # summary said "Temporal is not answering its health probe". The request-path population has
    # its own counter below; `tests/test_metric_declarations.py` holds this one to its one site.
    "chemclaw_durable_unreachable_total": (
        "Turns whose durable-subsystem health probe failed (Temporal did not answer)."
    ),
    # The request-path counterpart, and the sibling of `chemclaw_db_unavailable_total` — the same
    # shape for the same reason: one counter per shedding handler, counting the requests that
    # handler shed. Which subsystem was unavailable is in the `shedding` log line the handler
    # writes; it is not a label, because the value would be an exception class name and a bounded
    # label set that nobody enumerates is the cardinality trap `chemclaw_degraded_total` needed a
    # dedicated test to stay out of.
    "chemclaw_subsystem_unavailable_total": (
        "HTTP requests shed with 503 because a subsystem they needed was unavailable."
    ),
    # Non-zero means the provider reported usage this process could not parse, so those turns were
    # metered at zero. The budget guard meters what this reports, so a non-zero rate here means the
    # runaway-cost refusal is not binding — while every dashboard shows a deployment costing
    # nothing. Distinct from "no usage reported at all", which is legitimate and counts nothing.
    "chemclaw_usage_unreadable_total": (
        "Streamed usage contents that carried no readable token count (the turn metered zero)."
    ),
    # Non-zero means answers are being scored by the citation gate rather than the LLM judge. The
    # two measure different things (resolvability vs faithfulness), so this is not a slow path — it
    # is a weaker verdict, and without a counter a judge outage looked identical to a healthy
    # deployment on every dashboard.
    "chemclaw_verifier_degraded_total": (
        "Answers scored by the deterministic citation gate because the LLM judge was unavailable."
    ),
    # The review band's cost, made checkable rather than believed (D-2026-08-27): each extra judge
    # roll a verdict at the margin triggered is one increment, so band-rolls over answers-verified
    # is the measured fraction of turns actually paying the band.
    "chemclaw_verifier_band_rerolls_total": (
        "Extra judge rolls taken because a verdict landed inside the review band."
    ),
    # One row per protocol the condenser was handed, labelled by what happened to it:
    # `extracted` (its prose was read), `degraded` (the extraction failed or timed out, and the
    # row kept its recorded figures), `oversized` (too large for one call, refused by name and
    # never split). Labelled rather than three series because the three are one question — how
    # much of what a turn asked to condense actually got condensed — and a reader who cannot
    # divide them cannot answer it.
    "chemclaw_protocol_digests_total": (
        "Protocols handed to the condenser, by outcome (extracted / degraded / oversized)."
    ),
    "chemclaw_jobs_started_total": "Durable jobs launched by an agent tool.",
    # The job→session mailbox's failure signal. `notify_session_best_effort` swallows a failed
    # push-back by design (the science is the result; the notification is not), which made a
    # fleet-wide outage of the channel — a dead background queue, a full mailbox table —
    # invisible: every job finished, nobody was ever told. This is the only aggregate that says so.
    "chemclaw_pushback_dropped_total": (
        "Job push-back notifications that could not be recorded and were dropped."
    ),
    # The rejoin path's quiet failure. An identical job launch rejoins the running workflow, and
    # whether that rejoin is *announced* turns on one `describe()` call whose failure is a DEBUG
    # line — a broker that consistently refuses it silently reverts the announcement fix.
    "chemclaw_rejoin_describe_failed_total": (
        "Rejoined durable runs whose describe() failed, so the rejoin went unannounced."
    ),
    # The counter above counts *launches*, which on the most expensive thing this system does is the
    # least informative number available: a two-second xTB call and a six-hour DFT run increment it
    # identically. This is the consumption counterpart — accumulated seconds, so `rate()` reads as
    # "compute-seconds per second", the same shape as the token counters and the standard way spend
    # is expressed. A histogram would be the wrong instrument twice over: the shared bucket set tops
    # out at 300 s, which is noise for an hours-long search, and the question is how much was
    # consumed rather
    # than how the durations were distributed. Not node-hours — parallelism belongs to the launcher
    # and none reports it back yet — but runtime is the factor node-hours multiplies.
    "chemclaw_job_runtime_seconds_total": (
        "Wall-clock seconds accumulated by finished durable jobs, by connector."
    ),
    "chemclaw_notes_proposed_total": "Notes opened on a branch through the PR-gate.",
    # A note the indexer cannot parse is dropped from the graph so one bad file cannot block every
    # query — which is right, and was silent. `kg-validate` reports these in CI, over the
    # repository; nothing reported them over the tree a pod is actually serving, where a partial
    # sync or a truncated write leaves the deployment retrieving less than it should. A non-zero
    # rate here is a corpus problem, not a traffic problem, so it is a counter rather than a log
    # line alone.
    "chemclaw_notes_unparseable_total": (
        "Note files skipped by the indexer because they failed to parse; they are not retrievable."
    ),
    # The same defect wearing a different disguise, and the one that used to leave no trace at all:
    # two files claiming one note id. The indexer keeps the first in path order and drops the
    # second, so one of two curated notes is unreachable by every query — previously decided by
    # which directory sorted last, and reported nowhere. Like the counter above this is a corpus
    # problem rather than a traffic one, and it names the state an rsync that lands a renamed note
    # before removing the old one leaves behind.
    "chemclaw_notes_duplicate_id_total": (
        "Note files skipped by the indexer because another file already claimed their id; "
        "one of the two is not retrievable."
    ),
    # The counterpart to the line above, and the reason it could not stand alone: a best-effort
    # publish that fails is logged inside a workflow and swallowed, because the science is already
    # durable and a dead git remote must not fail a completed job. That is the right call about the
    # *job* and the wrong shape for the *knowledge* — with only a success counter, a total git
    # outage reads as "zero proposals", which is exactly what an idle system reads as. Two counters
    # make the difference visible and give the alert a ratio to fire on.
    "chemclaw_notes_publish_failures_total": (
        "Knowledge notes that could not be opened on a branch; the knowledge was lost."
    ),
    # A fan-out child that exhausted its retries and was dropped (`durable/orchestrator.py`).
    # Isolate-and-drop is the right policy — one poison input must not restart its siblings — but
    # until this counter existed the drop's only trace was a `workflow.logger.warning`, so the
    # parent completed *successfully* with a short list and every operator-visible signal said
    # healthy. Measured: a live fan-out returned two results from four inputs with nothing but log
    # lines to show for it.
    #
    # The failure this makes visible: the PR-gate's git credential expires, every
    # `PublishNoteWorkflow` child fails, and all three memory-synthesis jobs complete green
    # returning `[]` every night — `/schedules` showing `runs_total` climbing and no failures — for
    # as long as it takes someone to notice that nothing has been proposed in months.
    # `chemclaw_notes_publish_failures_total` above does not cover it: that one is incremented by
    # `publish_note_best_effort`, which the memory fan-out does not use.
    "chemclaw_fan_out_children_dropped_total": (
        "Fan-out children that failed their retries and were dropped; their work is missing from "
        "an otherwise successful parent."
    ),
    # The gate's outcomes, which the two counters above cannot express: they count submissions,
    # and the question an operator actually has is whether anything is being *reviewed*. A rising
    # `open` against a flat `merged` is a review queue nobody is working; `rejected` is the series
    # that had no record at all before, because a rejection is a deleted branch.
    "chemclaw_note_proposals_total": (
        "Note proposals by state — open on submission, merged/rejected on a human decision, "
        "failed when the submission never reached git."
    ),
    # A turn whose connectors did not come up still answers — from whatever tools remained. That is
    # the right behaviour and the reason it needs a number: a degraded answer is indistinguishable
    # from a good one in the transcript, and `open_reachable` returned the list to four callers that
    # all discarded it (REV-6). Counted per unreachable connector rather than per degraded turn, so
    # "one connector is dark" and "the fleet is dark" are different rates.
    "chemclaw_connectors_unreachable_total": (
        "Connectors that failed to come up when a turn or template step opened them; their tools "
        "were absent from that turn."
    ),
    "chemclaw_event_streams_rejected_total": (
        "Push-back event streams rejected with 429 at the per-user or per-process cap."
    ),
    # The push-back stream's own send timeout, and a separate series from
    # `chemclaw_turn_send_timeouts_total` because that declaration's comment forbids exactly this
    # merge: two populations in one counter is a denominator nobody can interpret. A turn stream cut
    # for a stalled reader and a push-back stream cut for one are different streams with different
    # lifetimes — the push-back stream is long-lived and holds a per-user slot, so a half-open
    # connection parks it invisibly until the kernel gives up on the socket.
    "chemclaw_event_stream_send_timeouts_total": (
        "Push-back event streams closed because a client stopped reading past the SSE send timeout."
    ),
    # A pooled checkout that times out is indistinguishable, from the route's point of view, from
    # an unreachable database — both arrive as `ConnectionError` and both are retryable. The load
    # run turned 16 of them into HTTP 500s because no route caught them, and the pool they came
    # from was not even exhausted: it never grew past 13 of 64 connections and opened zero new
    # ones. Counted separately from the admission shed so "the loop could not schedule a handoff"
    # is not read as "the LLM endpoint is full".
    # A token whose group memberships did not fit in it. Entra replaces `groups` with
    # `_claim_names` past roughly 150 memberships, and there is no fix at request time — resolving
    # the overage needs a Graph call, which D-089 does not permit. So the user with the *most*
    # access silently arrives with the *fewest* group-derived entitlements, and until this counter
    # existed the only trace was a WARNING line: a chemist quietly loses a gated document share and
    # nothing an operator watches moves. Counting it is what makes the condition alertable rather
    # than greppable, which is this repository's own standing rule about measurement.
    #
    # Unlabelled, deliberately: the interesting series is "is this happening at all", and a label
    # carrying the `oid` would key an unauthenticated exposition on user identity.
    "chemclaw_group_claim_overage_total": (
        "Validated tokens that carried a group-claim overage (`_claim_names`) instead of `groups`, "
        "so no group-derived entitlement could be read for that user."
    ),
    "chemclaw_db_unavailable_total": (
        "Requests shed with 503 because a pooled Postgres connection could not be obtained."
    ),
    # The upload cap's own shed, counted separately from the turn admission because the resource
    # is different: parse slots meter CPU in worker threads, permits meter LLM turns. A pod
    # refusing every upload and a pod being sent none look identical without this.
    "chemclaw_attachment_parses_shed_total": (
        "Uploads refused with 503 because every parse slot was still busy after "
        "`attachment_parse_queue_seconds`."
    ),
    # The two refusals that happen *before* a turn exists, and so were invisible to every counter
    # above: they are per-request, not per-turn. Unlabelled deliberately — a per-principal series
    # would key a metric on user identity, which `/metrics` is unauthenticated and must not carry
    # (D-152's allowlist). The rate is what an operator alerts on; who hit it is in the log.
    "chemclaw_requests_rate_limited_total": (
        "Requests refused with 429 by the per-principal request budget."
    ),
    "chemclaw_requests_too_large_total": (
        "Requests refused with 413 because the body exceeded service_max_request_bytes."
    ),
    # Same principle as the watermark counter above: the cross-process turn guard (D-121) is a
    # lease, so it holds only while its holder keeps refreshing. A refresh that fails is the guard
    # narrowing, and it must not be something only a log line knows.
    "chemclaw_turn_claim_refresh_failures_total": (
        "Failed refreshes of a running turn's session claim (D-121): the claim may lapse and "
        "another worker start a turn on the same session."
    ),
    # The counter above is the *warning*; this is the event it warns about, and until the
    # 2026-08-05 review nothing could see it. A refresh that matches no row raises nothing — the
    # takeover has simply already happened — so the failure counter stayed at zero through the one
    # outcome it exists to predict.
    "chemclaw_turn_claims_lost_total": (
        "Running turns whose session claim was taken over by another worker (D-121): the lease "
        "lapsed while the turn was still going, so two workers may have run on one session."
    ),
    # A live run looped `find_past_jobs` seven times in one turn, and the only trace was a turn
    # three times slower than the archived comparison. This is what a deployment alerts on when
    # the loop comes back — the refusal itself is invisible, since the turn still answers.
    "chemclaw_repeated_tool_calls_total": (
        "Tool calls refused because the turn had already made the identical one "
        "`max_identical_tool_calls` times, labelled by tool."
    ),
    # The budget guard (service.budget) already meters spend, but only to *refuse* a turn, and its
    # counters are per-process and unexported. This is the same number as an observable rate, so
    # "what is this deployment costing per hour" stops being a question only the provider's bill
    # can answer.
    "chemclaw_tokens_total": "Model tokens reported across all turns (prompt + completion).",
    # The same spend, split along the dimensions it is *priced* along (REV-10). One undifferentiated
    # total cannot answer "what is this costing", because input, output and cache-read carry
    # different prices — cache-read is roughly an order of magnitude cheaper than a fresh input
    # token, so a deployment that caches well and one that does not look identical in the total
    # while their bills do not. The provider already reports all four; nothing read past the sum.
    "chemclaw_input_tokens_total": "Prompt tokens sent to the model, excluding cache reads.",
    "chemclaw_output_tokens_total": "Completion tokens generated by the model.",
    "chemclaw_cache_read_tokens_total": (
        "Prompt tokens served from the provider's cache — priced well below a fresh input token, "
        "so this is the number that shows caching working."
    ),
    "chemclaw_cache_write_tokens_total": (
        "Prompt tokens written to the provider's cache — priced above a fresh input token, so a "
        "cache that is written and never read is a net loss this makes visible. Structurally 0 on "
        "the openai_compatible provider, which reports cache reads but has no cache-write concept: "
        "an honest zero here is not a fault (REV-9)."
    ),
    # Whether the context policy is running, and what it is buying. Two counters because they
    # answer two operator questions and neither answers the other: the first says the mechanism
    # fired at all, the second says whether the budget is set anywhere near the traffic. Both exist
    # because the defect they close was a compaction policy that *appeared* to run — three settings
    # with no reader, a config comment, and a sentence in the system prompt — for as long as nobody
    # had a number (`agent/compaction.py`). A counter is what makes "it is compacting" checkable
    # instead of believed.
    #
    # A model call that needed no reduction increments neither, so a flat zero means "never over
    # budget" and an absent series means "not wired" — the distinction the previous state of this
    # subsystem could not express.
    # **Outbound delivery, which shipped with no signal of any kind.** `chemclaw.deliver.registry`
    # held no
    # logger and no metric, `deliver()` swallowed every per-channel failure with a comment saying
    # the caller would log it, and the one caller discarded the return value — so "every digest was
    # dropped" and "every digest was delivered" produced identical observations. A seam built
    # because a project leader could not be reached on a Monday morning could fail totally and
    # silently, which is the same failure one layer in.
    #
    # Two counters rather than one, because a ratio is the question an operator actually has: a
    # channel that takes nothing while another takes everything is a broken webhook, and both
    # failing is an outage.
    "chemclaw_deliveries_total": ("Messages a delivery channel accepted, by channel."),
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
    # **The series the two above could not express, and the paragraph they used to end with was
    # wrong.** It said a flat zero on the compaction counter means "never over budget". Measured
    # through a compiled graph: a thread of 100,081 estimated tokens — over both triggers, ~224,000
    # billed — moved *neither* counter and emitted no event, because both edits ran and reclaimed
    # nothing. `ClearToolUsesEdit` had exactly `keep` candidates and the window edit cannot cut past
    # the newest group, so the one turn that is about to fail at the provider's context limit was
    # indistinguishable from a quiet one.
    #
    # So a flat zero on the two counters above means "never *reduced*", and this is what separates
    # the two readings of that.
    #
    # **What it is a leading indicator *of*, and this line has now been wrong in both directions.**
    # The comparison is the thread against `agent_context_token_budget` as
    # `context_budget.effective_trigger` converts it, and that conversion charges the request's own
    # prefix — instructions, the skills listing, every bound tool schema, measured at 43,175
    # estimated tokens on `default` on 2026-09-04. It used to charge it only where
    # `llm_context_window_tokens` was declared, and no deployment declares one, so this counter was
    # comparing a thread against a number the prefix had never met: measured through a compiled
    # graph, the edits left 90,030 estimated thread tokens beside that prefix, 137,301 went at a
    # 128k model, and this counter stayed **flat**. The line here then said so, which was the
    # correct reading of a broken arithmetic rather than of a broken counter.
    #
    # The prefix is charged unconditionally now, so a tick is a request the policy could not bring
    # inside the configured *request* budget — in every configuration, with or without a declared
    # window. Measured on the same fixture: the cut goes to 45,015 and the counter is silent
    # because the request now fits, and a thread the policy genuinely cannot cut that far ticks it
    # where the old arithmetic read clean — 0 -> 1 at the shipped budget with no window declared,
    # which `test_the_overrun_indicator_can_fire_at_the_shipped_budget_with_no_window` holds.
    # A flat zero is therefore evidence that every request stayed inside
    # `agent_context_token_budget`; whether that is the provider's real limit is what
    # `llm_context_window_tokens` still decides, and declaring it makes this the leading indicator
    # of a context-length failure rather than of a spend overrun.
    "chemclaw_context_unreducible_total": (
        "Model calls whose whole request — this call's prefix plus the thread — stayed over "
        "agent_context_token_budget after the context policy had reduced all it could. Where "
        "llm_context_window_tokens is declared the budget is additionally capped by what the model "
        "can hold, so a tick is the leading indicator of a context-length failure at the provider."
    ),
    # Counted rather than only logged because the *rate* is the signal: one truncated result is a
    # tool answering a broad question, and a tool that truncates on every call is one whose own
    # ceiling is set wrong for what a model can read.
    "chemclaw_tool_results_truncated_total": (
        "Tool results cut to agent_max_tool_result_chars before the model read them, by tool."
    ),
    # The counter for everything this codebase does *deliberately* and invisibly: catch, log a
    # warning, continue with less. Measured on `391b6ec^`: 41 such handlers across 34 modules, and
    # exactly 4 of them counted anything (`api/routes/turns.py`, `api/state.py`,
    # `durable/publish.py`, `kg/graph.py`) — so a preference store that had stopped writing, a cost
    # ledger losing every row and a redaction filter that never resolved its token names all read
    # from outside exactly like a healthy service. Each is individually right to swallow — the
    # alternative is failing a chemist's turn over telemetry — which is precisely why the swallow
    # has to leave a number behind.
    #
    # One counter with a `subsystem` label rather than one counter per site: the operator question
    # is "is anything degraded, and what", which is `sum by (subsystem)` over a single series
    # family, and a per-site counter would make that a union of a dozen metric names that has to be
    # edited every time a site is added. `agent/audit.py`'s dedicated
    # `chemclaw_audit_sink_failures_total` stays as it is — a lost audit record is a named
    # regulatory fact with its own alert, not a member of a general family.
    "chemclaw_degraded_total": (
        "Operations that failed and were continued past with reduced function, by subsystem."
    ),
    # --- the registry's own health -------------------------------------------------------------
    # Both of these count a failure *of the metrics surface*, which nothing could see before: a
    # bound gauge that raises, and a label set refused at the cardinality cap. Each used to leave
    # only a log line, and a log line about telemetry is the one nobody greps for.
    "chemclaw_gauge_read_failures_total": (
        "Gauge reads that raised and were omitted from a scrape, by metric — the reading is "
        "missing, and without this the omission is indistinguishable from an unbound gauge."
    ),
    "chemclaw_metric_series_dropped_total": (
        "Samples discarded because their metric had already reached the per-metric label-set cap; "
        "that metric is undercounting from this point on."
    ),
    # --- the HTTP surface ----------------------------------------------------------------------
    # There was no request-level metric of any kind: 23 routes and no way to answer "which route
    # is throwing 5xx", "what is p95 on /jobs", or "is this slowness the model or the database".
    # `route` is the *template*, never the raw path.
    "chemclaw_http_requests_total": "HTTP requests served, by route template and status class.",
    # Two refusals that happen before any handler and were therefore invisible to every counter
    # above, and one that happens inside `deps.py` and was invisible because it is deliberately a
    # 404. An authorization refusal returning 404-not-403 is right (it leaks no existence), and it
    # means the server-side record is the *only* place the distinction can survive — so a session
    # enumeration scan was indistinguishable from ordinary 404 traffic.
    "chemclaw_authz_refusals_total": (
        "Requests refused because the caller does not own the resource, by resource kind "
        "(answered 404 to avoid an existence leak, so this counter is the only trace)."
    ),
    "chemclaw_auth_failures_total": (
        "Requests refused at authentication, by reason (missing / invalid / provider_unavailable) "
        "— a client sending no header at all used to be indistinguishable from a healthy service."
    ),
    "chemclaw_request_validation_failures_total": (
        "Requests rejected with 422 by request-body validation, by route template. Nothing logged "
        "or counted these, so a client looping on a malformed body looked exactly like silence."
    ),
    # --- the model call ------------------------------------------------------------------------
    # The provider seam recorded nothing at all: a deployment retrying every call three times
    # inside the SDK looked identical to one retrying none, a provider rate-limiting us had no
    # counter distinct from the front door's own limiter, and `RunnableWithFallbacks` absorbing
    # 100% of traffic onto the fallback endpoint produced no log line and no metric.
    "chemclaw_model_calls_total": (
        "Model calls, by provider and outcome (ok / rate_limited / context_length / timeout / "
        "transport / error)."
    ),
    "chemclaw_model_fallbacks_total": (
        "Model calls served by the fallback endpoint after the primary raised, by provider — the "
        "signal that makes provider failover something an operator knows about rather than infers."
    ),
    # --- the tool chain ------------------------------------------------------------------------
    # A refusal and a crash were one `outcome='error'` and one identically-worded log line, so
    # "why did the agent not do the thing" required a LIKE scan of an unindexed free-text column.
    # Four outcomes, not three. `cancelled` is here because the alternative is under-counting
    # attempted calls: a turn abandoned mid-tool still made the call, and dropping it would make
    # this counter disagree with `audit_events`, which has recorded a `cancelled` outcome since
    # long before this metric existed.
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
    # --- the turn ------------------------------------------------------------------------------
    "chemclaw_turns_finished_total": (
        "Turns that ended, by outcome — the one series that separates `answered` from "
        "`loop_capped`, `empty_answer`, `errored`, `timed_out` and `abandoned`, which "
        "`turn_costs.completed` collapsed into a boolean."
    ),
    # --- the durable tier ----------------------------------------------------------------------
    # Measured on a live broker: a successful job emitted zero log lines and a failed job emitted
    # zero first-party lines and moved no metric. `chemclaw_jobs_started_total` had no counterpart
    # of any kind, so a connector whose every job failed was indistinguishable from an idle one.
    "chemclaw_jobs_finished_total": (
        "Durable jobs that ended, by connector and outcome (completed / failed) — the counterpart "
        "`chemclaw_jobs_started_total` never had."
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
    # --- the calculation cache -----------------------------------------------------------------
    # D-011 ("a persisted result is never recomputed") is the largest cost lever in the system and
    # `science/calc/store.py` has promised this counter to "the metrics layer (Phase 2b)" since it
    # was written. Until now the only way to see the cache working was DEBUG on a hot path.
    "chemclaw_calc_cache_total": (
        "Calculation-cache lookups, by outcome (hit / shared / miss) — `shared` is a concurrent "
        "miss on one key that `cached_compute` single-flighted onto another caller's computation."
    ),
    # The backend refusing for *capacity* rather than for bad data — the third category
    # `durable/publish.py` gained when a pod-full refusal stopped being classified as a permanent
    # error. It is counted separately from `chemclaw_degraded_total` deliberately: a busy backend is
    # ordinary operation, and folding it into the degradation signal would fire an outage alert on
    # a working system. It is the saturation signal for the calculation tier — one pod, four slots,
    # and a CREST search charged all four — so a rising rate here is the fleet asking for more calc
    # capacity, and it is the only place that asks.
    "chemclaw_calc_backend_at_capacity_total": (
        "Calculation-backend calls refused because every slot was busy, by tool. Retried with "
        "backoff rather than failed; a sustained rate means the backend is under-provisioned."
    ),
    # --- ingest and retrieval ------------------------------------------------------------------
    "chemclaw_ingest_records_total": (
        "Records seen by an ingest pass, by source and outcome (ingested / rejected / skipped)."
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
    "chemclaw_results_dead_lettered_total": (
        "Result publications retired to `failed` after exhausting their attempts. Distinct from "
        "`chemclaw_result_publish_failures_total`, which counts one row per *attempt*, so a "
        "permanent retirement was indistinguishable from a transient blip."
    ),
}

# Latency histograms. Two, not more: a turn is the unit a chemist waits on, and a tool call is the
# unit that explains a slow turn. Anything finer is what the trace pipeline is for.
# **Two sets, not one, and the single set was measurably wrong for both populations.** A turn and
# a tool call are different distributions — a turn is tens of seconds, a tool call is milliseconds
# to hours — and one shared set could only be a compromise that resolved neither.
#
# The old set topped out at 300 s while `service_turn_timeout_seconds` defaults to 600, so every
# turn between the two reported as 300 s: `histogram_quantile` cannot interpolate into `+Inf` and
# returns the highest finite boundary, which means p95 *saturated* precisely as turns got slow.
# Measured on the old set with samples at 450 s and 599 s, both landed in `+Inf`. The load test's
# p50 of 37 s at 50 users also sat in the 30-60 bucket with a 5x hole above it, so the busiest
# part of the range was the coarsest.
#
# `_TURN_BUCKETS` brackets the timeout on both sides, so a saturating deployment is visible as
# mass moving into the 600 bucket rather than as a quantile that stops moving.
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
# `_TOOL_BUCKETS` spans six orders of magnitude because the tool surface genuinely does: a
# `load_skill` is sub-millisecond, `inline_wait_seconds` is 20, and a graph step reaching the calc
# server is bounded by `calc_server_timeout_seconds` at 900.
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
# `_JOB_BUCKETS` is the same argument as `_TURN_BUCKETS` one tier up, and this histogram shipped
# making exactly the mistake that paragraph diagnoses: it was bound to `_TOOL_BUCKETS`, whose top
# finite boundary is **900 s**, while `connector_job_timeout_seconds` defaults to 18,000 and
# `xtb_job_timeout_seconds` to 15,000. `histogram_quantile` cannot interpolate into `+Inf` and
# returns the highest finite boundary, so every job over fifteen minutes landed in `+Inf` and the
# p95 pinned at exactly 900 s precisely as jobs got expensive — on the series whose own HELP text
# says it exists "so a p95 exists for the most expensive work in the system".
#
# Bracketed on both sides of the ceiling (14400 below, 21600 above) so a deployment saturating its
# own job timeout is visible as mass moving into the 18000 bucket rather than as a quantile that
# stops moving. The low end stays fine-grained because a re-run that hits the D-011 cache returns
# in seconds and belongs in a bucket of its own rather than pooled with a CREST search.
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
    18000.0,
    21600.0,
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
    "chemclaw_turn_duration_seconds": "Wall-clock duration of one streamed agent turn.",
    "chemclaw_tool_duration_seconds": "Wall-clock duration of one tool invocation.",
    "chemclaw_http_request_duration_seconds": (
        "Wall-clock duration of one HTTP request, by route template."
    ),
    "chemclaw_model_call_duration_seconds": "Wall-clock duration of one model call, by provider.",
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
}

# Per-histogram bucket boundaries. A histogram's buckets are part of its Prometheus identity, so
# this is a property of the declaration exactly as the HELP text is — see `_TURN_BUCKETS` above for
# why one shared set was wrong.
_HISTOGRAM_BUCKETS: dict[str, tuple[float, ...]] = {
    "chemclaw_turn_duration_seconds": _TURN_BUCKETS,
    "chemclaw_tool_duration_seconds": _TOOL_BUCKETS,
    "chemclaw_job_duration_seconds": _JOB_BUCKETS,
    "chemclaw_http_request_duration_seconds": _CALL_BUCKETS,
    "chemclaw_model_call_duration_seconds": _CALL_BUCKETS,
    "chemclaw_evidence_source_seconds": _CALL_BUCKETS,
    "chemclaw_embedding_duration_seconds": _CALL_BUCKETS,
    "chemclaw_db_query_duration_seconds": _CALL_BUCKETS,
    "chemclaw_sink_delivery_seconds": _CALL_BUCKETS,
}

# Histograms that carry labels, and the label names each accepts — the same declaration in both
# directions that `_COUNTER_LABELS` is, and enforced by the same code path.
#
# **`observe` took no labels at all until this existed**, which was the single most consequential
# limitation in this registry. `chemclaw_tool_duration_seconds` pooled an xTB call through the calc
# connector and a `read_attachment` into one distribution, so "why is this turn slow" could not be
# attributed to a tool — the one question the histogram's own docstring says it was added to
# answer. The same absence blocked per-route request latency, per-source retrieval latency and
# per-sink delivery latency, each of which is a separate finding elsewhere.
#
# Every label here is bounded by configuration or by a source literal, never by a caller's string,
# which is the same rule `_COUNTER_LABELS` documents at length. `route` is the FastAPI *route
# template* (`/sessions/{session_id}/messages`), enumerable from `app.routes` — never the raw path,
# which is attacker-controlled and is a cardinality bomb.
_HISTOGRAM_LABELS: dict[str, tuple[str, ...]] = {
    "chemclaw_tool_duration_seconds": ("tool",),
    "chemclaw_http_request_duration_seconds": ("route",),
    "chemclaw_model_call_duration_seconds": ("provider",),
    "chemclaw_evidence_source_seconds": ("source",),
    "chemclaw_db_query_duration_seconds": ("operation",),
    "chemclaw_job_duration_seconds": ("connector",),
    "chemclaw_sink_delivery_seconds": ("sink",),
}

# Bucket boundaries, in seconds. Not a `Settings` field on purpose: Prometheus treats the bucket
# set as part of a histogram's identity, so changing it per deployment breaks aggregation across
# pods and invalidates recorded history — it is a property of the metric's definition, like its
# HELP text, not a deployment knob. The range is chosen for this service's measured shape: a stub
# model puts a turn near 1 s, the load test's p50 at 50 users was 37 s, and the wall-clock turn
# timeout is 600 s, so the buckets have to span three orders of magnitude and still resolve the
# sub-second tool calls that dominate the count.
# Counters that carry labels, and the label names each accepts. A counter absent from this map is
# unlabelled and behaves exactly as before — pre-seeded to zero and rendered as one bare line.
#
# **Declared, not free-form** (REV-10). An undeclared label name raises exactly as an undeclared
# metric already does, because the failure mode of a label typo is not a crash but a second, silent
# time series that no dashboard queries and nobody notices.
#
# **Only `profile`, and deliberately not `model`.** The rule was written when per-model attribution
# was already emitted with richer labels than this registry could cheaply provide — the previous
# framework's chat-client instrumentation recorded `gen_ai.client.token.usage` with the request
# model, the response model, the provider and the token type. That instrumentation went with the
# framework and the LangChain stack ships no equivalent (`core/logging.py` records the measurement),
# so the axis is genuinely absent rather than emitted elsewhere. It is *still* not added here: the
# ledger `turn_costs` carries model attribution per turn (D-2026-08-01-spend-is-a-ledger-not-a-label
# decided exactly that question), and a second, lossier answer as a counter label would be the two
# systems to reconcile that this comment always warned about. What a *profile* costs is the gap this
# registry fills, because nothing else has ever heard of one.
_COUNTER_LABELS: dict[str, tuple[str, ...]] = {
    "chemclaw_tokens_total": ("profile",),
    "chemclaw_input_tokens_total": ("profile",),
    "chemclaw_output_tokens_total": ("profile",),
    "chemclaw_cache_read_tokens_total": ("profile",),
    "chemclaw_cache_write_tokens_total": ("profile",),
    # Four values, fixed by a CHECK constraint in `infra/sql/027_note_proposals.sql` — the only
    # label in this registry whose cardinality is bounded by the database rather than by trust.
    "chemclaw_note_proposals_total": ("state",),
    # Bounded by `CHEMCLAW_DELIVERY_CHANNELS` — a deployment's own list of channel folder names,
    # never a caller's string. Same rule as every label here.
    "chemclaw_deliveries_total": ("channel",),
    "chemclaw_delivery_failures_total": ("channel",),
    # Three values, fixed in `agent/condense.py`'s own `DigestSource` literal rather than by a
    # caller: `extracted`, `degraded`, `oversized`. Bounded by the code that emits it, which is the
    # same guarantee `state` above gets from a CHECK constraint.
    "chemclaw_protocol_digests_total": ("outcome",),
    # Bounded by configuration exactly as `profile` is: a connector is a bundle the chart enables,
    # never a name a caller supplies, and the shipped fleet is the set of `connector.yaml` files in
    # `connectors/` — not written down as a number here, because the number that was said six while
    # seven shipped.
    "chemclaw_job_runtime_seconds_total": ("connector",),
    # Two sources of conflict, different causes and different operator responses. `process` is a
    # same-process double-submit (impossible with the LRU's single-session cardinality guarantee,
    # but tracked for debugging). `durable` is a cross-replica race on the shared turn claim.
    "chemclaw_turns_conflict_total": ("scope",),
    # Bounded by the registered tool surface, which is configuration (the enabled connectors and
    # profile) rather than anything a caller can name.
    "chemclaw_repeated_tool_calls_total": ("tool",),
    # The same bound and for the same reason: a tool name here is one the registry served, never a
    # string a caller invented (`agent/tool_result_size.py` reads the request's tool name, which is
    # the one the graph dispatched).
    "chemclaw_tool_results_truncated_total": ("tool",),
    # A retriever's own `name`, and the bound is the same kind as `connector`: a source is a
    # registry entry a deployment activates, never a string a caller supplies. The shipped set is
    # the knowledge graph, the lexical and dense indexes, and the fingerprint store.
    "chemclaw_evidence_source_chunks_total": ("source",),
    # The same `source` label, on the counter that has to be read *against* the one above. Without
    # it the two series could not be joined at all: "graph contributed nothing this hour" and "some
    # source raised this hour" were two numbers with no way to decide whether they were the same
    # event, which is precisely the correlation an operator needs to tell a dark leg from a broken
    # one. Same bound, same reason — a retriever's registry name, never a caller's string.
    "chemclaw_evidence_source_failures_total": ("source",),
    # The decline counter completes the triple: chunks, failures, skips, one `source` label each,
    # so the three series join on the same key.
    "chemclaw_evidence_source_skips_total": ("source",),
    # The tightest bound of any label here: a subsystem name is a string literal at a `degraded()`
    # call site, so the whole value set is enumerable from the source and `tests/test_degraded.py`
    # enumerates it — across both call spellings (`degraded(...)` and `<module>.degraded(...)`) and
    # both argument forms, since its first version saw only the bare-name positional one and a
    # per-connector f-string label went past it silently. Nothing a request carries reaches here.
    "chemclaw_degraded_total": ("subsystem",),
    # Bounded by this module's own declarations: the label domain is `declared_metric_names()`.
    "chemclaw_gauge_read_failures_total": ("metric",),
    # Its sibling, and bounded the same way — the metric that hit the cardinality cap can only be
    # one of the names declared above. Unlabelled until 2026-09-04, which left an operator alerting
    # on it knowing that *something* was undercounting and nothing about what.
    "chemclaw_metric_series_dropped_total": ("metric",),
    # The route *template* from `request.scope["route"].path`, enumerable from `app.routes` — which
    # is the bound, and is why no count is written here: the one that was said 23 while `create_app`
    # registered 21. Never `request.url.path`, which is caller-controlled and unbounded, and which
    # is already the subject of a measured denial of service through the uvicorn access log.
    "chemclaw_http_requests_total": ("route", "status_class"),
    "chemclaw_request_validation_failures_total": ("route",),
    # Four literals in `api/deps.py`, four in `api/auth.py` — source-fixed, like `subsystem`.
    "chemclaw_authz_refusals_total": ("resource",),
    "chemclaw_auth_failures_total": ("reason",),
    # The provider name from the configured seam (`openai_compatible`, `anthropic`), not a model
    # id: per-model *spend* is `turn_costs`' question and stays there (D-2026-08-01), while
    # per-provider *health* is this one, and they are not the same question.
    "chemclaw_model_calls_total": ("provider", "outcome"),
    "chemclaw_model_fallbacks_total": ("provider",),
    "chemclaw_tool_calls_total": ("tool", "outcome"),
    "chemclaw_tool_refusals_total": ("reason",),
    "chemclaw_invalid_tool_calls_total": ("tool",),
    "chemclaw_turns_finished_total": ("outcome",),
    "chemclaw_jobs_finished_total": ("connector", "outcome"),
    "chemclaw_activity_failures_total": ("activity",),
    "chemclaw_calc_cache_total": ("outcome",),
    "chemclaw_calc_backend_at_capacity_total": ("tool",),
    "chemclaw_ingest_records_total": ("source", "outcome"),
    "chemclaw_evidence_source_kept_total": ("source",),
    "chemclaw_embedding_calls_total": ("outcome",),
    "chemclaw_db_query_failures_total": ("kind",),
}

# The most label-sets one counter may hold. A label *value* is not bounded by this module — it comes
# from configuration, and a future label could come from a provider response — so an unbounded map
# keyed on it is the same slow leak this codebase has already fixed three times (the budget
# tracker's per-user counters, the front door's live sessions, the note index). Past the cap the new
# series is refused and said so once, rather than being accepted quietly until the pod runs out of
# memory.
#
# **64 until the HTTP surface arrived, and the margin was one series.** Measured across the front
# door's 158 tests, `chemclaw_http_requests_total{route,status_class}` grew 35 series over 20 route
# templates plus `<unmatched>`, with no route producing more than three status classes — a worst
# case of 63 against a cap of 64. That is not a cardinality problem, it is a *sizing* one: the label
# domain is the route table, enumerable from `app.routes`, and it grows by one whenever somebody
# adds a route. 128 is sized against that table with room for it to double.
#
# Still generous for every other counter here: `profile` is a handful of names, so a *different*
# metric reaching this means something is generating values it should not — which is the case this
# cap exists for, and the reason it is raised rather than removed.
_MAX_SERIES_PER_COUNTER = 128

_GAUGES: dict[str, str] = {
    "chemclaw_turns_in_flight": "Turns currently streaming.",
    "chemclaw_egress_guard_armed": "1 when the in-process egress guard is installed, else 0.",
    "chemclaw_turn_capacity": "Configured maximum concurrent turns (the admission cap).",
    # The right-hand side of the only question the per-process cap cannot answer. `sum()` of the
    # gauge above across pods is what the fleet is *admitting* right now; this is what it was
    # declared to be allowed to admit. Config validation catches the product at deploy time, but it
    # cannot see a Deployment scaled by hand or an HPA edited in the cluster — an alert comparing
    # these two can. 0 when no ceiling is declared, which is what makes that alert self-disabling.
    "chemclaw_fleet_turn_ceiling": "Declared fleet-wide ceiling on concurrent turns (0 = none).",
    "chemclaw_live_sessions": "Sessions held in the front door's in-process LRU.",
    # **What the context budget is actually worth, measured instead of assumed.** The policy counts
    # with chars/4 and the provider bills its own tokenizer; the two disagree by content type, and
    # in the direction that matters — measured on this repository's payloads, the static prefix is
    # 1.04x and a connector JSON result 0.45x, so a thread believed to be at budget was ~2.2x it.
    # This is `billed / estimated`, smoothed over the calls this process has made, and it is the
    # number `agent/context_budget.effective_trigger` divides the configured budget by. 1.0 means
    # "not enough calls observed yet", which is also the state in which nothing is adjusted.
    "chemclaw_context_estimator_ratio": (
        "Billed input tokens divided by this system's own estimate of the same request (1.0 = "
        "not yet calibrated)."
    ),
    # Out-of-process capability can fail independently of the chat service, so its reachability
    # is a first-class signal rather than something to find in a log (`connectors.health`).
    "chemclaw_connectors_unhealthy": "Enabled connectors that could not be reached (0 = all up).",
    # The knowledge graph coming *in*, which had no signal at all: `chemclaw_notes_publish_failures
    # _total` covers a note failing to reach the PR-gate and nothing covered the corpus failing to
    # reach a pod. `deploy/knowledge-sync.sh`'s loop swallows a failed refresh on purpose, so the
    # pod serves a frozen graph and keeps citing it. Read from the volume by the process that
    # answers from it (`kg/graph.py::knowledge_sync_age_seconds`), so it needs no sidecar and no
    # kube-state-metrics.
    "chemclaw_knowledge_sync_age_seconds": (
        "Age of the newest note on this pod's knowledge tree, in seconds (-1 = the tree holds no "
        "note at all). Measures what this pod knows, not when the sync last ran."
    ),
    # Pool saturation (D-119). `requests_waiting` above zero is the signal that `pg_pool_max_size`
    # is too small for the offered load — the thing that used to show up as a connect timeout with
    # an idle database, which is unreadable from any other metric.
    "chemclaw_pg_pool_size": "Connections held across this process's Postgres pools.",
    "chemclaw_pg_pool_available": "Pooled connections currently idle and available.",
    "chemclaw_pg_pool_requests_waiting": "Callers blocked waiting for a pooled connection.",
    # The connection budget's two sides, the same pairing `chemclaw_turn_capacity` and
    # `chemclaw_fleet_turn_ceiling` make one subject over: `sum()` of the per-process ceiling is
    # what this deployment may open, and the declared number is what the server will serve. Config
    # validation refuses a bad product at startup but only for the shape the chart rendered, so a
    # `kubectl scale` or an in-cluster HPA edit is visible only by comparing these two. 0 when
    # undeclared, which is what makes the alert self-disabling.
    "chemclaw_pg_pool_max_size": "This process's configured maximum pooled connections.",
    "chemclaw_pg_fleet_max_connections": (
        "Declared fleet-wide ceiling on Postgres connections (0 = none)."
    ),
    # The calculation backend's admission budget (D-2026-08-27-a-per-worker-cap-is-not-a-backend-
    # ceiling), the third pair of this shape. Unlike the two above, the left-hand side is *live*
    # rather than configured, because the two kinds of process that dispatch there do not share a
    # cap: a `calc` worker is bounded by `worker_max_concurrent_activities`, while that bundle's own
    # MCP server pods dispatch straight from a tool call and are bounded by nothing local. Counting
    # the sessions actually held is the only number that covers both, and `sum()` of it across pods
    # is what `servers/calc` is being asked to serve right now.
    "chemclaw_calc_requests_in_flight": (
        "Calculation-backend sessions this process is currently holding open."
    ),
    "chemclaw_calc_backend_max_concurrent_requests": (
        "Declared ceiling on concurrent calculation-backend requests (0 = none)."
    ),
    # The event-stream cap's two sides, the same pairing `chemclaw_turns_in_flight` and
    # `chemclaw_turn_capacity` make for turns — and absent for exactly as long as the stream cap
    # has existed. Only *rejections* were counted, so "are we near the per-pod cap" was
    # unanswerable until the cap was already being hit.
    "chemclaw_event_streams_open": "Push-back event streams currently open on this process.",
    "chemclaw_event_stream_capacity": "Configured maximum concurrent push-back streams.",
    # In-flight durable work. `chemclaw_jobs_started_total` minus a completion counter would have
    # given this, except the two are booked in different processes — a job is launched by an agent
    # tool in the front door and executed by a worker — so a per-process subtraction is a number
    # nobody can take, and a fleet-wide one over raw counters is undone by any pod restart.
    #
    # **A deployment-wide reading, published identically by every worker**, so a dashboard takes
    # `max()` over the series and never `sum()`. It is read from the broker's own visibility count
    # (`durable/job_metrics.py`), because the per-process number this used to publish was not
    # measurable from inside a workflow: driven live, it stuck at 1 after a terminate, read 0 for a
    # RUNNING workflow after an eviction, and raised out of its own `finally` on worker shutdown.
    "chemclaw_jobs_in_flight": "Durable jobs open in this deployment (the same reading per pod).",
}

# Gauges that carry **one** label, read as a whole family from a single callable returning
# `{label value: reading}`.
#
# One label, not an arbitrary set, because three real callers wanted exactly one and a general
# mechanism for a case nobody has is the abstraction this repository's own rules refuse. The
# callers: an outbox backlog per sink, ingest lag per source, and connector health per connector —
# each of which was previously either a single summed number that answered nothing ("some sink is
# behind, which one?") or a `COUNT(*)` the code declined to do per scrape.
_GAUGE_FAMILIES: dict[str, str] = {
    "chemclaw_outbox_pending": "Result publications queued and not yet delivered, by sink.",
    # The number that separates "a backlog of five that turns over every second" from "a backlog
    # of five that has not moved since Tuesday". The partial index this reads
    # (`result_publications_pending`) already exists, and `min(enqueued_at)` over it is an
    # index-only scan on the leading edge — so the "a gauge would need a COUNT(*) on every scrape"
    # objection that argued against a gauge here does not apply to an age.
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
    # **The half of the static prefix the ratchet cannot see.** `tests/test_context_floor.py`
    # gates every in-process tool schema a turn binds, and an endpoint tool's schema arrives from a
    # running MCP server at handshake — so a connector's docstrings grow every turn's cost, forever,
    # with nothing in this repository able to fail. Measured at handshake, by connector, which is
    # the only place both the tool list and its origin are known. Its `sum()` plus the ratcheted
    # floor is what a turn actually pays before the chemist says anything.
    "chemclaw_connector_tool_schema_tokens": (
        "Estimated tokens of bound tool schema advertised by each connector at handshake."
    ),
    "chemclaw_connector_unhealthy": (
        "1 per enabled connector that could not be reached, by connector. The unlabelled "
        "`chemclaw_connectors_unhealthy` says how many; this says which, which is the half "
        "`open_reachable` had in hand and discarded."
    ),
}

_GAUGE_FAMILY_LABELS: dict[str, str] = {
    "chemclaw_outbox_pending": "sink",
    "chemclaw_outbox_oldest_pending_seconds": "sink",
    "chemclaw_outbox_dead_lettered": "sink",
    "chemclaw_ingest_cursor_lag_seconds": "source",
    "chemclaw_connector_unhealthy": "connector",
    "chemclaw_connector_tool_schema_tokens": "connector",
}


def declared_metric_names() -> frozenset[str]:
    """Every metric name this registry declares — counters, histograms and gauges together.

    Public because a metric name is a contract with things *outside* this process, and the only
    other place it is written down is prose: `docs/guides/runbook.md` tells an operator what to
    query, and an ADR tells them what to alert on. `make prose-validate` resolves those citations
    against this set (D-2026-08-08). Exposed as one function rather than three tables so a fourth
    kind of metric cannot be added without every reader of "what is declared" seeing it.
    """
    return (
        frozenset(_COUNTERS)
        | frozenset(_HISTOGRAMS)
        | frozenset(_GAUGES)
        | frozenset(_GAUGE_FAMILIES)
    )


def declared_histogram_names() -> frozenset[str]:
    """Just the histograms, for readers that must reason about their derived series.

    A histogram is the one metric kind whose *declared* name is not the name anybody queries:
    Prometheus derives `_bucket`, `_sum` and `_count` from it, and real PromQL — every
    `histogram_quantile` an operator will ever write — cites the derived name. A reader checking
    prose or a dashboard against `declared_metric_names()` therefore has to fold those three
    suffixes back, and folding them against every declared name would accept
    `chemclaw_turns_started_total_bucket` as well. Exposed as its own set so that fold can be
    exact rather than approximate.
    """
    return frozenset(_HISTOGRAMS)


class Metrics:
    """A tiny, thread-safe counter/gauge/histogram registry rendering Prometheus exposition text.

    Gauges are read through callables rather than stored, so a gauge can never drift from the
    structure it describes (the semaphore, the session map, the connection pools) — there is
    nothing to keep in sync. Counters and histograms are accumulated, since there is no live
    structure holding "how many turns have ever run".
    """

    def __init__(self) -> None:
        """Start with every declared counter at zero and no gauge sources bound."""
        self._lock = threading.Lock()
        self._counts: dict[str, float] = dict.fromkeys(_COUNTERS, 0.0)
        self._gauges: dict[str, Callable[[], float]] = {}
        self._gauge_families: dict[str, Callable[[], Mapping[str, float]]] = {}
        # Per histogram: one tally per bucket, plus a final overflow slot for samples past the
        # last boundary, plus the running sum. The *cumulative* counts the exposition format wants
        # are derived at render time, so recording a sample is one index and one increment.
        # Keyed by histogram, then by label set — `()` for an unlabelled one. An unlabelled
        # histogram is pre-seeded so it renders as an honest zero from the first scrape (it always
        # did); a labelled one is not, for the reason the labelled counters are not: an invented
        # zero series is indistinguishable from an observed one.
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
        # Labelled series, per counter, keyed by the sorted label pairs. Not pre-seeded: a series
        # exists once it has been observed, which is the Prometheus convention and the same rule
        # the gauge path states — an invented zero is indistinguishable from a real one.
        self._series: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._capped: set[str] = set()

    def increment(
        self, name: str, amount: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Add to a declared counter. An undeclared name or label is a programming error, so raises.

        The declaration is binding **in both directions**: a counter in `_COUNTER_LABELS` must be
        incremented *with* its labels, and one absent from it must be incremented *without* any.
        One rule rather than two, and it removes the case that has no good answer — a bare sample
        beside labelled ones, which a scraper reads as a further series rather than as their total,
        so the counter would silently double-count under any `sum()`.
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

        The cap used to leave nothing behind but one WARNING per metric per process lifetime, so
        "this metric is undercounting and has been for two days" was a log line nobody re-reads
        rather than a series anybody can alert on. It counts itself now — into a metric that is
        deliberately *not* itself capped, since its whole label domain is the declared metric
        names.

        **Written straight into `_series` rather than through `increment`, for two reasons that
        both have to hold.** The caller holds `self._lock`, which is a plain `Lock`, so re-entering
        the public method would deadlock; and the cap check lives in `increment`, so bypassing it
        is what makes "not itself capped" literally true rather than merely argued — this counter
        keeps recording after every other one has stopped. The cardinality that buys is bounded by
        `_COUNTERS` itself, which is a table in this file.
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

        Read on every scrape like an ordinary gauge, and guarded the same way — a source that
        raises omits its family and increments `chemclaw_gauge_read_failures_total` rather than
        failing the whole response.
        """
        if name not in _GAUGE_FAMILIES:
            raise KeyError(f"undeclared gauge family {name!r}")
        with self._lock:
            self._gauge_families[name] = source

    def observe(self, name: str, seconds: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one latency sample. An undeclared name or label is a programming error, so raises.

        The declaration binds **in both directions**, exactly as `increment`'s does: a histogram in
        `_HISTOGRAM_LABELS` must be observed *with* its labels, and one absent from it *without*
        any. The reason is the same and it is not symmetry for its own sake — a bare sample beside
        labelled ones is read by a scraper as a further series rather than as their total, so
        `histogram_quantile` over a `sum by (le)` would silently mix a per-label distribution with
        a duplicate of the whole.
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
        # `bisect_left` puts a sample exactly on a boundary in that boundary's bucket, which is
        # what Prometheus's `le` ("less than or equal") semantics mean. Past the last boundary it
        # lands in the overflow slot rendered as `le="+Inf"`.
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

        Summed rather than per-series on purpose: a caller asking for a counter's value wants the
        number the unlabelled counter used to report, and Prometheus aggregates the same way
        server-side. Reading one series is a query concern, not this registry's.
        """
        with self._lock:
            return self._counts[name] + sum(self._series.get(name, {}).values())

    def observations(self, name: str) -> tuple[int, float]:
        """A histogram's `(count, sum)` across every label set.

        Summed rather than per-series for the reason `value()` is: a caller asking for a
        histogram's totals wants the numbers the unlabelled histogram used to report, and
        Prometheus aggregates the same way server-side.
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
                lines.append(f"{name} {counts[name]:g}")
                continue
            # A labelled counter emits one line per observed series and never a bare one — the
            # bare sample cannot exist, because `increment` requires the declared labels. A
            # counter nothing has observed yet is therefore genuinely absent rather than zero,
            # which is the Prometheus convention and this module's own rule for gauges.
            for key, total in sorted(series.get(name, {}).items()):
                rendered = ",".join(f'{label}="{_escape(value)}"' for label, value in key)
                lines.append(f"{name}{{{rendered}}} {total:g}")
        for name, help_text in _GAUGES.items():
            source = gauges.get(name)
            if source is None:
                # A gauge whose source is not bound is omitted rather than reported as 0 — a
                # fabricated zero would be indistinguishable from a genuinely idle service.
                continue
            # **One failing source must not take the scrape down with it.** Until this guard
            # existed, `render()` invoked every bound callable bare, so a `pool.get_stats()` that
            # raised during a shutdown or a pool-registry race turned `/metrics` into an HTTP 500
            # — losing all of this process's metrics, from this pod, at exactly the moment the
            # incident they exist for was happening. Proven by binding a raising callable to
            # `chemclaw_pg_pool_size`: the whole response was lost.
            #
            # Omitting the one gauge is the same rule the branch above states for an unbound one,
            # applied to a source that cannot answer rather than one that is absent — and the
            # counter is what stops that omission being silent.
            try:
                reading = float(source())
            except Exception:
                self.increment("chemclaw_gauge_read_failures_total", labels={"metric": name})
                log.warning("gauge %s could not be read; it is absent from this scrape", name)
                continue
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name} {reading:g}",
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
                lines.append(f'{name}{{{label}="{_escape(str(value))}"}} {float(reading):g}')
        for name, help_text in _HISTOGRAMS.items():
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
            boundaries = _HISTOGRAM_BUCKETS[name]
            for key, buckets in sorted(histograms[name].items()):
                # The label pairs a bucket line carries, with `le` appended — `le` must sit inside
                # the same brace group as the rest, which is why this is built as a prefix rather
                # than by formatting two separate groups.
                declared = "".join(f'{label}="{_escape(value)}",' for label, value in key)
                suffix = "".join(f'{label}="{_escape(value)}"' for label, value in key)
                braced = f"{{{suffix}}}" if suffix else ""
                # Prometheus buckets are cumulative ("how many samples were <= le"), so the
                # per-bucket tallies are summed as they are emitted; the final `+Inf` bucket
                # equals the count.
                cumulative = 0.0
                for boundary, tally in zip(boundaries, buckets[:-1], strict=True):
                    cumulative += tally
                    lines.append(f'{name}_bucket{{{declared}le="{boundary:g}"}} {cumulative:g}')
                cumulative += buckets[-1]  # the overflow slot: samples past the last boundary
                lines += [
                    f'{name}_bucket{{{declared}le="+Inf"}} {cumulative:g}',
                    f"{name}_sum{braced} {histogram_sums[name][key]:g}",
                    f"{name}_count{braced} {cumulative:g}",
                ]
        return "\n".join(lines) + "\n"


# The process-wide registry. A module singleton for the same reason logging configuration is one:
# a scrape targets a process, and code deep in the call tree (the audit sink, a tool) must be able
# to count something without having a registry threaded down to it.
METRICS = Metrics()

# Exposition content type, per the Prometheus text format spec. Kept beside the renderer so the
# route and the format cannot disagree.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
