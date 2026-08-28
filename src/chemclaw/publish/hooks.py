"""The third publish hook: a composite that is neither cached nor a job.

**Why a third one.** Two hooks reach `enqueue_payload` today, and between them they were believed
to cover everything this system computes:

- a **primitive**, offered by `science/calc/store.py::publish_stored_result` on the cache-miss path,
  keyed by the `calc_type` the calculation server stamped;
- a **job composite**, offered by `ConnectorJobWorkflow._publish_result` in
  `durable/connector_job.py`, from the envelope a finished Temporal job returns, routed by the
  `payload_kind` that envelope carries
  (`D-2026-08-26-a-route-is-not-a-shape`).

There is a third kind and it reaches neither. `compute_thermochemistry` and `predict_logd` are
**tool** composites: assembled in-process from parts that are each separately keyed, returned inside
one conversation turn. A composite is not written to the calculation cache — its key would name its
own output, which is why it was never shipped as a primitive (D-011,
`D-2026-08-16-the-physics-leaves-the-cache-stays`) — and neither is a job, so no envelope names its
shape. Both have had a projector in `PAYLOAD_PROJECTORS` since the seam shipped, and nothing has
ever called it. `D-2026-08-27-a-composite-needs-a-hook-not-a-projector` is the decision.

**Where it hangs, and why that is not "a hook every tool must remember".** `audit_events.agent` is
the shape to avoid: a claim that something is recorded, with no producer, kept alive by a test that
calls the producer directly. So this is not called by a tool. It is called from the boundary every
tool result already crosses — `connectors/server.py`, which wraps each registered tool's function
once per bundle, at the same choke point `_sanitize_tool_errors` and `_bind_caller_per_tool_call`
already use and for the same stated reason ("patched once here, the one shared choke point every
connector's app is built through, rather than once per bundle"). A new tool is registered with
`@server.tool()` and is therefore already inside it; there is nothing for its author to remember.
The wrapper is on the tool's *function* rather than on `ToolManager.call_tool`, because by the time
a call reaches the manager the result has been through `convert_result` and is content blocks — a
tool result is not a model on the wire (`D-2026-08-26-a-tool-result-is-not-a-model-on-the-wire`),
and this hook needs the model to name its own shape.

**What decides whether a result is published, and why it is a declared set.** Most shapes crossing
that boundary are *already* published by the cache hook, under their own cache key — republishing
them here would mint a second record of one calculation under a second identity. So the hook
publishes only what `TOOL_COMPOSITES` names. That is an exclusion with a machine behind it, not a
list to be remembered: `tests/test_publish_reaches_the_hooks.py` derives the set — every projector
reachable from no stamped `calc_type` and no job envelope member — and fails if the declaration and
the derivation disagree. A new tool composite therefore fails the suite until it is declared, which
is the same discipline `_NOT_YET_PUBLISHED` and `_PRIMITIVES_NOT_PUBLISHED` already carry.

**Nothing here may fail a tool.** The polarity is `publish_stored_result`'s and
`publish_result_best_effort`'s: by the time this runs the science is computed and the model is
waiting for it, so a results store — or the local outbox, or this module — being unreachable is
counted and logged, never raised, and the tool returns exactly what it would have returned.
"""

import logging
from typing import Any

from pydantic import BaseModel

from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.publish.outbox import enqueue_payload
from chemclaw.publish.registry import publishing_enabled

logger = logging.getLogger(__name__)


# The result shapes that reach a results store through this hook and through no other.
#
# **Both are composites whose key would name their own output**, which is exactly why they are not
# cached and therefore why nothing else can offer them:
#
# - `ThermochemistryResult` — `compute_thermochemistry` relaxes, takes a Hessian, and re-optimizes
#   along any imaginary mode until the geometry settles. Its key would name the geometry the loop
#   settles on. It is also the only place a vibrational frequency exists in this system: the
#   `xtb.hess` row it is built from carries the matrix and no masses, so no projector over that row
#   can produce one (`_hessian`).
# - `LogdResult` — `predict_logd` is a cached remote pKa plus a local Crippen sum plus one
#   Henderson-Hasselbalch term, composed client-side. `connectors/calc/remote.py` records that the
#   calculation server refuses to key it, and `_CALC_TYPE_PROJECTORS` has no `logd` prefix because
#   logD has never had a cache row at all.
#
# Kept as a declaration rather than derived at runtime because the derivation needs the server's
# stamped key contract and the job envelope's member fields — two things a serving process has no
# reason to hold. The suite holds them, and asserts this set equals what they imply.
TOOL_COMPOSITES: frozenset[str] = frozenset({"ThermochemistryResult", "LogdResult"})


def _composite_ref(connector: str, tool: str, arguments: dict[str, Any]) -> str:
    """The identity of one tool composite: the request that produced it.

    A composite has no cache key — that is the definition of one here — so it has no
    content-addressed identity to borrow. The job hook answers the same question the same way and
    says so: `calc_ref` is the workflow id, "because a composite has no cache key: its identity is
    the run. That is also what makes it idempotent, since the workflow id is itself derived
    deterministically from the job and its arguments." This is that sentence without a workflow.

    So the same question asked twice is one record — the outbox's `ON CONFLICT DO NOTHING` on
    `(sink, calc_ref, schema_version)` collapses the repeat — while the same molecule asked at a
    second temperature is a second record, which is right: it is a second measurement.

    The arguments are the *validated* keyword arguments the tool body ran on, so a default the
    caller omitted and a default the caller passed explicitly derive one ref rather than two.
    """
    return f"{connector}.{tool}#{stable_hash(arguments)}"


async def publish_tool_result(
    *, connector: str, tool: str, arguments: dict[str, Any], result: Any
) -> int:
    """Offer one tool's own result to the external results store; never raise.

    Returns how many outbox rows were written, which a caller may ignore: by the time this runs the
    tool has already computed its answer, and a results store that cannot be queued to is strictly
    less important than returning it. Zero is the ordinary answer — no sink configured, or a result
    this hook does not publish — and is not a failure.

    Args:
        connector: The bundle serving the tool, which is the first half of the route recorded as
            `calc_type`. A route names where the work was dispatched and never what came back, so
            the shape is carried separately as `payload_kind`
            (`D-2026-08-26-a-route-is-not-a-shape`).
        tool: The tool's own name — the second half of that route.
        arguments: The validated keyword arguments the tool ran on. Hashed into the record's
            identity; never stored verbatim, because they are a request rather than a result.
        result: Whatever the tool returned. Anything that is not a pydantic model, or whose model
            is not named in `TOOL_COMPOSITES`, is left alone.
    """
    if not publishing_enabled() or not isinstance(result, BaseModel):
        return 0
    kind = type(result).__name__
    if kind not in TOOL_COMPOSITES:
        return 0
    try:
        return await enqueue_payload(
            calc_ref=_composite_ref(connector, tool, arguments),
            # A route, exactly as the job hook builds one. It identifies where this came from; the
            # `payload_kind` beside it is what identifies the shape.
            calc_type=f"{connector}.{tool}",
            payload=result.model_dump(mode="json"),
            payload_kind=kind,
            input_hash=stable_hash(arguments),
        )
    except Exception:
        # `enqueue_payload` promises not to raise and is tested for it; this is the guard for
        # everything *around* that promise — an argument dict that will not hash, a model whose
        # dump fails, a future edit here. A tool call must not fail because a record could not be
        # queued, and the counter is what keeps that from being silence.
        logger.exception("publish[tool]: could not queue %s from %s.%s", kind, connector, tool)
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0


__all__ = ["TOOL_COMPOSITES", "publish_tool_result"]
