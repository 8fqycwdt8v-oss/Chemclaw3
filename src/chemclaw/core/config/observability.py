"""Logging, the tool-audit trail, and OpenTelemetry export.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class ObservabilitySettings(BaseSettings):
    """Logging, the tool-audit trail, and OpenTelemetry export.

    Grouped because these are the process-wide "what happened" knobs: one config-driven switch
    for verbosity so an admin can raise it to DEBUG for troubleshooting without touching code,
    the audit-record shape, and the (off-by-default) OTel pipeline. Applied once per process by
    `chemclaw.core.logging.configure_logging`, called at each worker's entrypoint.
    """

    # The format carries the timestamp, level, and logger name every diagnosis needs.
    log_level: str = "INFO"
    # The three identifiers are in the default format, not only in the JSON one. `ContextFilter`
    # stamps `correlation_id`/`actor`/`session_id` onto every record that reaches a swept handler,
    # and until this line carried them the only way to see them was `log_json`, which is set in the
    # chart and nowhere else — so `make chat`, `make connectors`, a hand-started worker, CI and
    # every local reproduction ran with the join key invisible. The earlier reasoning here ("two
    # formats to keep in step is how one of them goes stale") is right about the risk and had the
    # cost backwards: the format a developer actually reads was the one with no way to join a line
    # to a turn.
    log_format: str = (
        "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s/%(session_id)s]: %(message)s"
    )
    # One JSON object per line instead of the `%`-format string above. Off in code and on in the
    # chart, the same split `budget_enabled` uses: a developer reading a terminal wants the string,
    # and a cluster log stack wants to parse rather than guess. The `%`-format is left as the
    # default *shape* rather than widened with the three ids, because `log_json` supersedes it and
    # two formats to keep in step is how one of them goes stale.
    log_json: bool = False
    # The tool-audit trail (agents.audit): every agent tool call is logged once (name, args,
    # outcome, latency) by one tool-call middleware. Arguments are truncated to this many
    # characters so a large payload (a full optimization problem, an observation list) cannot
    # flood the log; raise it when a fuller argument record is needed for an audit.
    agent_audit_max_arg_chars: int = Field(default=200, ge=0)
    # How many distinct numeric values one tool result may put on a `ToolResultEvent`. A ceiling
    # rather than a budget: the largest real result measured holds 49 (a full electronic-properties
    # calculation — every atom charge and bond order), so the default is an order of magnitude
    # clear of normal traffic and exists only so a pathological result (a thousand-row table dump)
    # cannot put megabytes on a browser's event stream. Here rather than a literal in
    # `api/runner_trace.py` because it is a threshold on the wire, and an operator whose UI is
    # choking on a chatty connector must be able to lower it without a release (2026-08-05 review).
    stream_max_result_numbers: int = Field(default=512, ge=0)
    # How large one tool result may be, in UTF-8 bytes, and still be written to the tool-result
    # store (`api/tool_results.py`) for a surface to fetch through
    # `GET /sessions/{id}/tool-results/{ref}`. A result over the cap is **not stored** and its
    # `ToolResultEvent.result_ref` stays empty — the honest "not stored" — and the producer logs
    # what it refused, for the reason stated one field up: a silent truncation reads as
    # completeness, which is what `_capped_numbers` exists to avoid.
    #
    # 128 KiB against a largest-measured real result of ~20,000 characters (a 40-chunk evidence
    # sweep), so it is far out of reach of normal traffic and exists only so a pathological result
    # cannot put megabytes per call into Postgres. 0 disables storing entirely — one knob rather
    # than a cap plus an on/off flag, because "store nothing" is the cap at its floor and two
    # settings would be two ways to say one thing.
    stream_max_result_bytes: int = Field(default=131072, ge=0)
    # How large one tool result may be, in UTF-8 bytes, and still ride along on its own
    # `ToolResultEvent` as `result_inline` instead of costing a surface a second round trip.
    #
    # The preview/ref split is a rule about *large* results, and it was being applied to every
    # result: a 300-byte ICH limit and a two-field pKa each paid a fetch to be rendered as anything
    # but prose, for a payload several times smaller than the preview's own 200-character budget.
    # Under this cap the text is on the event; over it, the field is empty and the ref is the only
    # way to the result, exactly as before.
    #
    # 4 KiB, which is two orders of magnitude below `stream_max_result_bytes` and deliberately so:
    # this must never become the path by which a 20,000-character evidence sweep reaches a browser
    # on every turn. It is a shortcut for the small ones, and the number is what keeps it that.
    # 0 disables it — the same one-knob rule as the cap above, where "never inline" is the floor.
    stream_inline_result_bytes: int = Field(default=4096, ge=0)
    # The deployment's code/prompt/skill revision stamped onto every audit record (AG-14): the
    # Git SHA the running pod was built from, so a past agent result ties to the exact version that
    # produced it (reproducibility). The image build sets it — `deploy/Containerfile` takes a
    # `CHEMCLAW_REVISION` build arg and exports it under this name, and the image workflow passes
    # the commit SHA. That sentence used to be here as a claim about a build that did not exist:
    # nothing set it anywhere, so every deployment recorded the literal "unknown" while AG-14 read
    # as met (REV-17). "unknown" is now what a local `docker build` honestly reports, not what
    # production does. `tests/test_deploy_chart.py` pins the wiring; the image job runs the built
    # image and compares the value, because only that can prove it arrived.
    deployment_revision: str = "unknown"
    # OpenTelemetry *span* export (off by default). When enabled,
    # `chemclaw.core.logging.configure_telemetry` builds the tracer provider itself and lets the
    # exporter read the standard `OTEL_EXPORTER_OTLP_*` environment variables. Requires the
    # OpenTelemetry SDK + OTLP exporter extras.
    #
    # **Traces only, and that is a change rather than a simplification.** The bootstrap used to be
    # one call into the agent framework, which also installed a `MeterProvider` and recorded the
    # `gen_ai.client.token.usage` histogram — per-model token attribution that no replacement in
    # this stack emits. It is gone; `chemclaw_*_tokens_total` and the `turn_costs` table are where
    # spend is answered now (`docs/guides/runbook.md` says so where an operator looks).
    #
    # `otel_include_sensitive_data` **has a consumer again**, and it is the same consumer it always
    # had: whether prompts and completions are attached to spans. It lost the last one with the
    # agent framework and spent a phase as a knob `configure_telemetry` warned about;
    # `otel_llm_spans` below asks the identical question, so it gets this flag back rather than a
    # second one beside it. False (the default) builds an OpenInference `TraceConfig` with every
    # hide flag set, so a model-call span carries identifiers and counts and nothing else — the rule
    # `core/tracing.py` states. It still governs nothing when `otel_llm_spans` is off, and
    # `configure_telemetry` still says so out loud in that case.
    #
    # **Setting it True is a decision about data leaving the pod**, not a debugging convenience: it
    # puts a chemist's question and the model's answer on a span that travels to the collector, and
    # `SECURITY.md`'s note about the audit trail applies to the collector's store in the same words.
    otel_enabled: bool = False
    otel_include_sensitive_data: bool = False
    # A span per *model call*, through OpenInference's LangChain instrumentation. Off by default
    # like every other observability switch.
    #
    # **What it buys.** `core/tracing.py` opens two spans — the turn and the tool call — so the
    # model call between them is invisible, and `chemclaw_*_tokens_total` carries `profile` rather
    # than model (D-152, deliberately). Both were named regressions once the agent framework left
    # with `gen_ai.client.token.usage`. The instrumentation emits one span per model call carrying
    # `llm.token_count.prompt`/`.completion`/`.total`, `llm.model_name` and `llm.provider`, plus the
    # chain and tool spans around them, over plain OTLP — so the collector this chart already points
    # at is the whole delivery mechanism.
    #
    # **Arize Phoenix reads these conventions natively and is what this was measured against**, but
    # nothing here depends on it: any OTLP backend receives the same spans, and the package is
    # Apache-2.0 while Phoenix's server is Elastic License 2.0. The licence question is about the
    # container an operator runs, not about this tree.
    otel_llm_spans: bool = False
    # Whether Chemclaw leaves LangSmith's own tracing switches alone. Off, so it does not.
    #
    # Beside `otel_llm_spans` because it answers the neighbouring question — where model-call
    # telemetry is allowed to go — and gives the opposite answer for a different destination.
    # OTLP spans go to a collector this deployment runs; LangSmith tracing posts prompts and
    # completions to api.smith.langchain.com, which D-2026-08-11 declined for the production path
    # (proprietary, no OSS self-host, third-party content storage that four merged decisions
    # forbid).
    #
    # `langsmith` enables itself from ambient environment and is in the closure regardless of that
    # decision — a hard requirement of `langchain-core`, pulled again by `deepagents`. So the
    # decision has to be *applied*, which `chemclaw.core.egress.pin_langsmith_egress` does at
    # import of this package. Until then it was applied only by the Helm chart, leaving every
    # other way of starting a process (`make chat`, `make connectors`, a hand-started worker, CI,
    # `docker run`) governed by whatever the environment happened to hold.
    #
    # **True does not turn tracing on.** It stops Chemclaw overriding `LANGSMITH_TRACING` /
    # `LANGCHAIN_TRACING_V2`, handing the choice back to the operator's own environment — for a
    # developer deliberately debugging against their own LangSmith project. Setting it in a
    # deployment is a decision about chemists' questions leaving the pod for a third party, on the
    # same footing as `otel_include_sensitive_data` and with less control over where they land.
    langsmith_tracing_allowed: bool = False
    # The in-process egress guard (`chemclaw.core.netguard`), armed at config import. It patches the
    # socket entry points so a connect or DNS lookup to a host outside the derived allowlist —
    # the LLM gateway, Postgres, Temporal, the connector endpoints, the IdP, and whatever
    # `egress_allow` names — is refused, logged at ERROR and counted. Defence in depth behind the
    # NetworkPolicy for the invariant that only LLM traffic (and declared infrastructure) leaves the
    # estate: a library fetching model weights, a usage ping, a DNS licence check is caught here,
    # though a static scan cannot see it. On by default; `false` is the loud, stated opt-out for
    # a deployment that has an equivalent network control and wants the process out of the way. It
    # cannot cover a child process, a `ctypes` call into libc, or a compiled extension's own
    # syscalls — the NetworkPolicy is the layer that does.
    egress_guard_enabled: bool = True
    # Extra hosts the guard permits, comma-separated, on top of the destinations derived from the
    # other settings. Empty by default and empty in the shipped chart; each entry is a deliberate,
    # reviewed exception (a mirror, a licence server) in the same spirit as `MCP_EGRESS_ALLOW`.
    egress_allow: str = ""
    # The OTLP collector endpoint (plan F6-T5). Bridged into `OTEL_EXPORTER_OTLP_ENDPOINT` when
    # set, so the exporter's own precedence still applies; empty in dev (no collector). Config, so
    # the in-cluster collector address is one value like every other endpoint.
    otel_endpoint: str = ""
    # Where a *worker* process serves `/healthz`, `/readyz` and `/metrics`
    # (`chemclaw.core.worker_http`). The front door has `service_port`; every other process had no
    # HTTP surface at all, which is why its metrics were uncollected and its liveness was a comment
    # rather than a probe. Separate from `service_port` because these are different processes in
    # different pods, and a worker binding the chat port would read as one.
    #
    # 0 disables the surface. That is for two workers on one developer machine — the second cannot
    # bind — and never for a deployment: the chart sets the port on every worker Deployment and a
    # test pins that it does.
    worker_metrics_host: str = "0.0.0.0"
    worker_metrics_port: int = Field(default=9000, ge=0)
    # Where the **Temporal SDK's own** Prometheus exposition is served, in every process that opens
    # a Temporal client. Beside `worker_metrics_port` because it is the same question — which port
    # does a PodMonitor scrape — and giving the two different answers is how one of them gets left
    # out of the chart.
    #
    # A second endpoint rather than a merge into `chemclaw_*`, because these are the SDK's series
    # and not this registry's: the SDK owns their names, their labels and their cardinality, and
    # `core/metrics.py` is deliberately strict about all three. `Client.connect` takes a `runtime=`
    # and nothing in `src/` passed one, so **none of them existed**: no `temporal_num_pollers`, no
    # `temporal_worker_task_slots_available` / `_used`, no `activity_schedule_to_start_latency`, no
    # `activity_execution_failed`, no sticky-cache size or miss rate. Verified live: building the
    # runtime below exposed nine series immediately and the failure/latency families under load.
    #
    # The one that decides a deployment: `worker_max_concurrent_activities` is a pod's throughput
    # ceiling, and a CREST search holds a slot for hours — so a `connector-calc` worker with every
    # slot taken and a growing schedule-to-start queue looked exactly like an idle one.
    #
    # 0 disables, the same shape and for the same reason `worker_metrics_port` uses: two workers on
    # one developer machine cannot both bind. Off by default, because a process that binds a port
    # nobody asked it to is a surprise in every environment that is not a cluster.
    temporal_metrics_host: str = "0.0.0.0"
    temporal_metrics_port: int = Field(default=0, ge=0)
    # How long an in-flight Temporal activity gets to finish after a stop signal before the worker
    # cancels it (`durable/serve.py`). Bounded on both sides and neither bound is arbitrary: below
    # it, a drain that cancels everything is a hard kill with extra steps; above it, a node drain is
    # held open by work Temporal would happily retry. 120 s finishes a short activity — a note
    # re-index, a digest, an ELN page — and abandons a long one to the retry that already exists
    # for it. The chart's `terminationGracePeriodSeconds` must sit above this, or the kubelet
    # SIGKILLs through the drain and the setting buys nothing; `tests/test_deploy_chart.py` pins
    # that ordering.
    worker_graceful_shutdown_seconds: float = Field(default=120.0, gt=0)
    # How long `kg/graph.py::knowledge_sync_age_seconds` may reuse one stat scan of the knowledge
    # tree. That gauge is a *live* callback, so it ran an O(notes) `rglob` + `stat` sweep on every
    # scrape, synchronously, inside the `async def` that serves `/metrics` — measured, `render()`
    # went from 0.128 ms on an empty tree to 102.6 ms at 10k notes, and that is the whole front
    # door's event loop stalled for the duration, not one request's latency.
    #
    # It is a scan budget rather than a metric-staleness budget, and the difference is the reason a
    # number this large is safe: what gets cached is the newest note's **mtime**, while the age is
    # recomputed from `time.time()` on every scrape. So a corpus that stopped arriving keeps
    # reporting an age that grows in real time however long the cache lives — the failure this gauge
    # exists for cannot be cached away. The only thing the window delays is noticing a corpus got
    # *newer*, which makes a reading at most this many seconds too old: it errs toward alerting and
    # never toward silence.
    #
    # 300 s because it has to sit comfortably above the scrape interval to do anything at all — at
    # a 30 s scrape a 15 s window would miss on every single scrape and buy nothing — and because
    # `ChemclawKnowledgeCorpusStale` is a `for: 15m` rule against a threshold stated in hours, so
    # five minutes of pessimism is inside its noise. 0 restores the scan-every-scrape behaviour.
    knowledge_age_scan_ttl_seconds: float = Field(default=300.0, ge=0.0)

    @model_validator(mode="after")
    def _the_two_metrics_ports_are_not_the_same_port(self) -> Self:
        """Two expositions on one port is a worker that reports Temporal as unreachable.

        Both are served by the *same process*: `worker_http` binds `worker_metrics_port` for the
        `chemclaw_*` registry, and the Temporal SDK's own Rust exporter binds
        `temporal_metrics_port`. Setting them equal is not a duplicate scrape — the second bind
        fails. Measured on 2026-08-28 with 127.0.0.1:9111 already held: `Runtime(...)` raised
        `ValueError: Failed starting Prometheus exporter: Address already in use`, and because that
        construction happens inside `connect_options()`, `core/temporal_client.py::connect()`
        replaced it with "the durable execution backend (Temporal) is unreachable … This is an
        infrastructure outage" — for a broker that was up and answering.

        `telemetry_runtime()` now degrades rather than raising, so this is no longer the difference
        between a working process and a broken one; it is still a deployment stating a thing it
        cannot have, and the answer to "why are there no SDK metrics" should be a startup error
        naming both settings rather than a counter somebody has to think to look at. 0 means
        "disabled" for both and is therefore not a collision.
        """
        if (
            self.temporal_metrics_port
            and self.temporal_metrics_port == self.worker_metrics_port
            and self.temporal_metrics_host == self.worker_metrics_host
        ):
            raise ValueError(
                f"temporal_metrics_port={self.temporal_metrics_port} is also "
                "worker_metrics_port, on the same host: one process serves both expositions, so "
                "the second bind fails. Give the SDK's exposition a port of its own, or set "
                "temporal_metrics_port=0 to switch it off."
            )
        return self
