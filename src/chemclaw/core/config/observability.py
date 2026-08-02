"""Logging, the GxP tool-audit trail, and OpenTelemetry export.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class ObservabilitySettings(BaseSettings):
    """Logging, the GxP tool-audit trail, and OpenTelemetry export.

    Grouped because these are the process-wide "what happened" knobs: one config-driven switch
    for verbosity so an admin can raise it to DEBUG for troubleshooting without touching code,
    the audit-record shape, and the (off-by-default) OTel pipeline. Applied once per process by
    `chemclaw.core.logging.configure_logging`, called at each worker's entrypoint.
    """

    # The format carries the timestamp, level, and logger name every diagnosis needs.
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    # One JSON object per line instead of the `%`-format string above. Off in code and on in the
    # chart, the same split `budget_enabled` uses: a developer reading a terminal wants the string,
    # and a cluster log stack wants to parse rather than guess. The `%`-format is left as the
    # default *shape* rather than widened with the three ids, because `log_json` supersedes it and
    # two formats to keep in step is how one of them goes stale.
    log_json: bool = False
    # GxP tool-audit trail (agents.audit): every agent tool call is logged once (name, args,
    # outcome, latency) by one MAF function middleware. Arguments are truncated to this many
    # characters so a large payload (a full optimization problem, an observation list) cannot
    # flood the log; raise it when a fuller argument record is needed for an audit.
    agent_audit_max_arg_chars: int = Field(default=200, ge=0)
    # The deployment's code/prompt/skill revision stamped onto every audit record (AG-14): the
    # Git SHA the running pod was built from, so a past agent result ties to the exact version that
    # produced it (GxP reproducibility). The image build sets it — `deploy/Containerfile` takes a
    # `CHEMCLAW_REVISION` build arg and exports it under this name, and the image workflow passes
    # the commit SHA. That sentence used to be here as a claim about a build that did not exist:
    # nothing set it anywhere, so every deployment recorded the literal "unknown" while AG-14 read
    # as met (REV-17). "unknown" is now what a local `docker build` honestly reports, not what
    # production does. `tests/test_deploy_chart.py` pins the wiring; the image job runs the built
    # image and compares the value, because only that can prove it arrived.
    deployment_revision: str = "unknown"
    # OpenTelemetry export (off by default). When enabled,
    # `chemclaw.logging.configure_telemetry` calls MAF's `configure_otel_providers`, which reads
    # the standard `OTEL_EXPORTER_OTLP_*` environment variables for the collector endpoint.
    # Requires the OpenTelemetry SDK + OTLP exporter extras to be installed;
    # `enable_sensitive_data` controls whether prompts/results are attached to spans (keep off
    # unless a trusted collector needs them).
    otel_enabled: bool = False
    otel_include_sensitive_data: bool = False
    # The OTLP collector endpoint (plan F6-T5). Exported as `OTEL_EXPORTER_OTLP_ENDPOINT` for
    # MAF's `configure_otel_providers` when set; empty in dev (no collector). Config, so the
    # in-cluster collector address is one value like every other endpoint.
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
    # How long an in-flight Temporal activity gets to finish after a stop signal before the worker
    # cancels it (`durable/serve.py`). Bounded on both sides and neither bound is arbitrary: below
    # it, a drain that cancels everything is a hard kill with extra steps; above it, a node drain is
    # held open by work Temporal would happily retry. 120 s finishes a short activity — a note
    # re-index, a digest, an ELN page — and abandons a long one to the retry that already exists
    # for it. The chart's `terminationGracePeriodSeconds` must sit above this, or the kubelet
    # SIGKILLs through the drain and the setting buys nothing; `tests/test_deploy_chart.py` pins
    # that ordering.
    worker_graceful_shutdown_seconds: float = Field(default=120.0, gt=0)
