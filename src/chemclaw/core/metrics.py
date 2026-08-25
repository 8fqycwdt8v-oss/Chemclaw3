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
    # The wall-clock timeout's sibling, and the reason it needed one: a turn stopped by the
    # harness loop's iteration cap used to return normally and emit nothing, so the runaway guard
    # firing was invisible to everything outside the process. A rising rate here is an agent that
    # keeps planning more work than a turn can close — a prompt or skill problem, not an outage.
    "chemclaw_turn_loop_caps_total": "Turns stopped by the harness loop's iteration cap.",
    # A plan that could not be read at all, as distinct from a session proposing none. Alertable
    # because it is the one state in which a one-shot approval is left unspent (`agent/plan_gate.py`
    # says what that would cost if it passed silently as "no plan").
    "chemclaw_plan_unreadable_total": "Turns whose plan could not be read to spend its approval.",
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
    "chemclaw_jobs_started_total": "Durable jobs launched by an agent tool.",
    # The counter above counts *launches*, which on the most expensive thing this system does is the
    # least informative number available: a two-second xTB call and a six-hour DFT run increment it
    # identically. This is the consumption counterpart — accumulated seconds, so `rate()` reads as
    # "compute-seconds per second", the same shape as the token counters and the standard way spend
    # is expressed. A histogram would be the wrong instrument twice over: the shared bucket set tops
    # out at 300 s, which is noise for HPC work, and the question is how much was consumed rather
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
    "chemclaw_context_compactions_total": (
        "Model calls whose message list was reduced to stay inside the context token budget."
    ),
    "chemclaw_context_reclaimed_tokens_total": (
        "Estimated prompt tokens reclaimed by context compaction (char/4 estimate, not billed "
        "tokens — the billed figure is chemclaw_input_tokens_total)."
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
}

# Latency histograms. Two, not more: a turn is the unit a chemist waits on, and a tool call is the
# unit that explains a slow turn. Anything finer is what the trace pipeline is for.
_HISTOGRAMS: dict[str, str] = {
    "chemclaw_turn_duration_seconds": "Wall-clock duration of one streamed agent turn.",
    "chemclaw_tool_duration_seconds": "Wall-clock duration of one tool invocation.",
}

# Bucket boundaries, in seconds. Not a `Settings` field on purpose: Prometheus treats the bucket
# set as part of a histogram's identity, so changing it per deployment breaks aggregation across
# pods and invalidates recorded history — it is a property of the metric's definition, like its
# HELP text, not a deployment knob. The range is chosen for this service's measured shape: a stub
# model puts a turn near 1 s, the load test's p50 at 50 users was 37 s, and the wall-clock turn
# timeout is 600 s, so the buckets have to span three orders of magnitude and still resolve the
# sub-second tool calls that dominate the count.
_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)

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
    # Bounded by configuration exactly as `profile` is: a connector is a bundle the chart enables,
    # never a name a caller supplies, and the whole shipped fleet is six.
    "chemclaw_job_runtime_seconds_total": ("connector",),
    # Two sources of conflict, different causes and different operator responses. `process` is a
    # same-process double-submit (impossible with the LRU's single-session cardinality guarantee,
    # but tracked for debugging). `durable` is a cross-replica race on the shared turn claim.
    "chemclaw_turns_conflict_total": ("scope",),
    # Bounded by the registered tool surface, which is configuration (the enabled connectors and
    # profile) rather than anything a caller can name.
    "chemclaw_repeated_tool_calls_total": ("tool",),
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
    # The tightest bound of any label here: a subsystem name is a string literal at a `degraded()`
    # call site, so the whole value set is enumerable from the source and `tests/test_degraded.py`
    # enumerates it — across both call spellings (`degraded(...)` and `<module>.degraded(...)`) and
    # both argument forms, since its first version saw only the bare-name positional one and a
    # per-connector f-string label went past it silently. Nothing a request carries reaches here.
    "chemclaw_degraded_total": ("subsystem",),
}

# The most label-sets one counter may hold. A label *value* is not bounded by this module — it comes
# from configuration, and a future label could come from a provider response — so an unbounded map
# keyed on it is the same slow leak this codebase has already fixed three times (the budget
# tracker's per-user counters, the front door's live sessions, the note index). Past the cap the new
# series is refused and said so once, rather than being accepted quietly until the pod runs out of
# memory. Generous: `profile` is a handful of names, so reaching this means something is wrong.
_MAX_SERIES_PER_COUNTER = 64

_GAUGES: dict[str, str] = {
    "chemclaw_turns_in_flight": "Turns currently streaming.",
    "chemclaw_turn_capacity": "Configured maximum concurrent turns (the admission cap).",
    # The right-hand side of the only question the per-process cap cannot answer. `sum()` of the
    # gauge above across pods is what the fleet is *admitting* right now; this is what it was
    # declared to be allowed to admit. Config validation catches the product at deploy time, but it
    # cannot see a Deployment scaled by hand or an HPA edited in the cluster — an alert comparing
    # these two can. 0 when no ceiling is declared, which is what makes that alert self-disabling.
    "chemclaw_fleet_turn_ceiling": "Declared fleet-wide ceiling on concurrent turns (0 = none).",
    "chemclaw_live_sessions": "Sessions held in the front door's in-process LRU.",
    # Out-of-process capability can fail independently of the chat service, so its reachability
    # is a first-class signal rather than something to find in a log (`connectors.health`).
    "chemclaw_connectors_unhealthy": "Enabled connectors that could not be reached (0 = all up).",
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
}


def declared_metric_names() -> frozenset[str]:
    """Every metric name this registry declares — counters, histograms and gauges together.

    Public because a metric name is a contract with things *outside* this process, and the only
    other place it is written down is prose: `docs/guides/runbook.md` tells an operator what to
    query, and an ADR tells them what to alert on. `make prose-validate` resolves those citations
    against this set (D-2026-08-08). Exposed as one function rather than three tables so a fourth
    kind of metric cannot be added without every reader of "what is declared" seeing it.
    """
    return frozenset(_COUNTERS) | frozenset(_HISTOGRAMS) | frozenset(_GAUGES)


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
        # Per histogram: one tally per bucket, plus a final overflow slot for samples past the
        # last boundary, plus the running sum. The *cumulative* counts the exposition format wants
        # are derived at render time, so recording a sample is one index and one increment.
        self._histograms: dict[str, list[float]] = {
            name: [0.0] * (len(_BUCKETS) + 1) for name in _HISTOGRAMS
        }
        self._histogram_sums: dict[str, float] = dict.fromkeys(_HISTOGRAMS, 0.0)
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
                if name not in self._capped:
                    self._capped.add(name)
                    log.warning(
                        "counter %s reached %d label sets; further series are dropped. A label "
                        "value here is meant to be low-cardinality (a profile name), so this "
                        "means something is generating values it should not.",
                        name,
                        _MAX_SERIES_PER_COUNTER,
                    )
                return
            series[key] = series.get(key, 0.0) + amount

    def bind_gauge(self, name: str, source: Callable[[], float]) -> None:
        """Bind a gauge to a live source; reading it always reflects current state."""
        if name not in _GAUGES:
            raise KeyError(f"undeclared gauge {name!r}")
        with self._lock:
            self._gauges[name] = source

    def observe(self, name: str, seconds: float) -> None:
        """Record one latency sample. An undeclared name is a programming error, so it raises."""
        if name not in _HISTOGRAMS:
            raise KeyError(f"undeclared histogram {name!r}")
        # `bisect_left` puts a sample exactly on a boundary in that boundary's bucket, which is
        # what Prometheus's `le` ("less than or equal") semantics mean. Past the last boundary it
        # lands in the overflow slot rendered as `le="+Inf"`.
        index = bisect_left(_BUCKETS, seconds)
        with self._lock:
            self._histograms[name][index] += 1.0
            self._histogram_sums[name] += seconds

    def value(self, name: str) -> float:
        """A counter's total across every label set (tests assert on this, not on the text).

        Summed rather than per-series on purpose: a caller asking for a counter's value wants the
        number the unlabelled counter used to report, and Prometheus aggregates the same way
        server-side. Reading one series is a query concern, not this registry's.
        """
        with self._lock:
            return self._counts[name] + sum(self._series.get(name, {}).values())

    def observations(self, name: str) -> tuple[int, float]:
        """A histogram's `(count, sum)` — what tests assert on instead of parsing the text."""
        with self._lock:
            return int(sum(self._histograms[name])), self._histogram_sums[name]

    def render(self) -> str:
        """Render the Prometheus text exposition format (one HELP/TYPE/value block per metric)."""
        with self._lock:
            counts = dict(self._counts)
            gauges = dict(self._gauges)
            histograms = {name: list(values) for name, values in self._histograms.items()}
            histogram_sums = dict(self._histogram_sums)
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
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name} {float(source()):g}",
            ]
        for name, help_text in _HISTOGRAMS.items():
            buckets = histograms[name]
            lines += [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
            # Prometheus buckets are cumulative ("how many samples were <= le"), so the per-bucket
            # tallies are summed as they are emitted; the final `+Inf` bucket equals the count.
            cumulative = 0.0
            for boundary, tally in zip(_BUCKETS, buckets[:-1], strict=True):
                cumulative += tally
                lines.append(f'{name}_bucket{{le="{boundary:g}"}} {cumulative:g}')
            cumulative += buckets[-1]  # the overflow slot: samples past the last boundary
            lines += [
                f'{name}_bucket{{le="+Inf"}} {cumulative:g}',
                f"{name}_sum {histogram_sums[name]:g}",
                f"{name}_count {cumulative:g}",
            ]
        return "\n".join(lines) + "\n"


# The process-wide registry. A module singleton for the same reason logging configuration is one:
# a scrape targets a process, and code deep in the call tree (the audit sink, a tool) must be able
# to count something without having a registry threaded down to it.
METRICS = Metrics()

# Exposition content type, per the Prometheus text format spec. Kept beside the renderer so the
# route and the format cannot disagree.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
