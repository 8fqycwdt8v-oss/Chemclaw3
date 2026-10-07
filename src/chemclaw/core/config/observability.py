"""Logging, the tool-audit trail, OpenTelemetry export and the egress guard.

One domain section of the composed `Settings`; the package `__init__.py` flattens the sections and
owns the env prefix, `.env` loading and cross-section validators.
"""

import logging
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class ObservabilitySettings(BaseSettings):
    """The process-wide "what happened" knobs.

    Verbosity, the audit-record shape and the off-by-default OTel pipeline, applied once per process
    by `chemclaw.core.logging.configure_logging` at each entrypoint.
    """

    # The format carries the timestamp, level, and logger name every diagnosis needs.
    log_level: str = "INFO"
    # The default format carries `correlation_id`/`actor`/`session_id` (stamped by `ContextFilter`)
    # so a line can be joined to a turn without `log_json`.
    log_format: str = (
        "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s/%(session_id)s]: %(message)s"
    )
    # One JSON object per line instead of the `%`-format string. Off in code, on in the chart: a
    # terminal wants the string, a log stack wants to parse.
    log_json: bool = False
    # Tool-audit trail (agents.audit): each tool call is logged once (name, args, outcome, latency);
    # arguments are truncated to this many characters.
    agent_audit_max_arg_chars: int = Field(default=200, ge=0)
    # Distinct numeric values one tool result may put on a `ToolResultEvent`; a ceiling an order of
    # magnitude above real results, so a table dump cannot flood the browser stream.
    stream_max_result_numbers: int = Field(default=512, ge=0)
    # Largest tool result (UTF-8 bytes) written to the tool-result store (`api/tool_results.py`,
    # `GET /sessions/{id}/tool-results/{ref}`), including the full text of a result the model saw
    # cut. Over the cap it is not stored (empty `result_ref`, never trimmed) and the producer logs
    # it. Derived as four bytes per character times the largest first-party per-tool ceiling;
    # `tests/test_full_tool_results.py` pins that. 0 disables storing.
    stream_max_result_bytes: int = Field(default=1_048_576, ge=0)
    # Largest tool result (UTF-8 bytes) that rides on its `ToolResultEvent` as `result_inline`,
    # saving small results a fetch. Kept far below `stream_max_result_bytes` so large sweeps never
    # ride the stream. 0 disables.
    stream_inline_result_bytes: int = Field(default=4096, ge=0)
    # Git SHA stamped onto every audit record so a result ties to the code that produced it. Set by
    # the image build (`CHEMCLAW_REVISION` build arg in `deploy/Containerfile`); "unknown" means a
    # local build. `tests/test_deploy_chart.py` pins the wiring.
    deployment_revision: str = "unknown"
    # OpenTelemetry span export (traces only). `chemclaw.core.logging.configure_telemetry` builds
    # the provider; the exporter reads `OTEL_EXPORTER_OTLP_*`. Needs the SDK and OTLP extras. Token
    # spend is in `chemclaw_*_tokens_total` and `turn_costs`.
    #
    # `otel_include_sensitive_data` decides whether prompts and completions go on `otel_llm_spans`
    # spans; False hides all content (`core/tracing.py`). Setting it True sends chemists' questions
    # and answers to the collector, a data-egress decision.
    otel_enabled: bool = False
    otel_include_sensitive_data: bool = False
    # A span per model call via OpenInference's LangChain instrumentation, carrying token counts,
    # model name and provider, over plain OTLP. Any OTLP backend works (Arize Phoenix included).
    otel_llm_spans: bool = False
    # Whether Chemclaw leaves LangSmith's own tracing switches alone; off, so it does not. LangSmith
    # is declined (third-party content storage) but enables itself from ambient environment, so
    # `chemclaw.core.egress.pin_langsmith_egress` applies the decision at import. True does not
    # enable tracing; it hands `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` back to the environment.
    langsmith_tracing_allowed: bool = False
    # The in-process egress guard (`chemclaw.core.netguard`), armed at config import: a connect or
    # DNS lookup outside the derived allowlist (gateway, Postgres, Temporal, connectors, IdP,
    # `egress_allow`) is refused, logged and counted. It also governs the `LD_PRELOAD` layer
    # (`chemclaw.core.netguard_preload`, armed by `deploy/entrypoint.sh`) that catches child
    # processes and compiled extensions; `false` disables both. Static binaries and raw syscalls
    # remain the NetworkPolicy's.
    egress_guard_enabled: bool = True
    # Extra hosts the guard permits, comma-separated; each a reviewed exception. Hosts named only in
    # a manifest (`datasource.yaml`/`sink.yaml` `connection:` blocks, delivery channels) must be
    # listed here, since the allowlist derives from settings alone. A git note remote is derived
    # automatically (`netguard._push_hosts`, with `ssh -G` for aliases) unless git uses a custom ssh
    # command.
    egress_allow: str = ""
    # Bound on `ssh -G <alias>` (`netguard._ssh_hostname`) at config import; no network, just ssh
    # config. On timeout the alias itself is allowed.
    egress_ssh_resolve_timeout_seconds: float = Field(default=5.0, gt=0)
    # OTLP collector endpoint, bridged into `OTEL_EXPORTER_OTLP_ENDPOINT` when set; empty in dev.
    otel_endpoint: str = ""
    # Where a worker serves `/healthz`, `/readyz` and `/metrics` (`chemclaw.core.worker_http`);
    # separate from the front door's `service_port`. 0 disables, for two workers on one dev machine
    # only; the chart always sets it.
    worker_metrics_host: str = "0.0.0.0"
    worker_metrics_port: int = Field(default=9000, ge=0)
    # Where the Temporal SDK's own Prometheus exposition is served in every process with a Temporal
    # client. Separate from `chemclaw_*` because the SDK owns those names and labels. These series
    # (pollers, task slots, schedule-to-start latency) are what distinguish a saturated worker from
    # an idle one. 0 disables, the default.
    temporal_metrics_host: str = "0.0.0.0"
    temporal_metrics_port: int = Field(default=0, ge=0)
    # How long an in-flight activity may finish after a stop signal before the worker cancels it
    # (`durable/serve.py`); long activities fall back to their retries. The chart's
    # `terminationGracePeriodSeconds` must exceed it (`tests/test_deploy_chart.py`).
    worker_graceful_shutdown_seconds: float = Field(default=120.0, gt=0)
    # How long `kg/graph.py::knowledge_sync_age_seconds` reuses one stat scan of the knowledge tree,
    # which otherwise runs on every scrape inside the `/metrics` event loop. Only the newest mtime
    # is cached and the age is recomputed each scrape, so staleness errs toward alerting. Matches
    # the knowledge-sync sidecar's cadence. 0 scans every scrape.
    knowledge_age_scan_ttl_seconds: float = Field(default=300.0, ge=0.0)

    @field_validator("log_level")
    @classmethod
    def _log_level_is_one_logging_accepts(cls, value: str) -> str:
        """Refuse a level name `logging` will not take, here rather than at the first log line.

        Resolved through `logging.getLevelNamesMapping()` (so stdlib aliases like `WARN` work) and
        upper-cased, as `configure_logging` does.
        """
        level = value.strip().upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError(
                f"log_level={value!r} is not a level `logging` accepts, so this deployment would "
                "construct and then die at its first log line. Use one of "
                f"{sorted(logging.getLevelNamesMapping())} (CHEMCLAW_LOG_LEVEL)"
            )
        return level

    @model_validator(mode="after")
    def _the_two_metrics_ports_are_not_the_same_port(self) -> Self:
        """Two expositions on one port is a worker that reports Temporal as unreachable.

        Both are bound by the same process, so equal ports make the second bind fail; refuse it at
        startup naming both settings. 0 disables either and is not a collision.
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
