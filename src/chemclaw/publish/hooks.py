"""The third publish hook: a composite that is neither cached nor a job.

Primitives are published from the cache-miss path and job composites from the Temporal envelope.
Tool composites (`compute_thermochemistry`, `predict_logd`) are assembled in-process within a
turn and reach neither, since a composite's key would name its own output
(`D-2026-08-27-a-composite-needs-a-hook-not-a-projector`).

The hook hangs on `connectors/server.py`'s per-tool wrapper, the choke point every registered
tool already passes through, so no tool author has to remember it. It wraps the tool *function*
because past it the result is content blocks, not a model that can name its shape.

Only `TOOL_COMPOSITES` are published (everything else is published under its own cache key);
`tests/test_publish_reaches_the_hooks.py` derives that set and fails if they disagree. Nothing
here may fail a tool: errors are counted and logged, never raised.
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
# Both are composites whose key would name their own output, so they are never cached:
# `ThermochemistryResult` (an optimise/Hessian loop; also the only source of vibrational
# frequencies) and `LogdResult` (remote pKa plus local Crippen, composed client-side). Declared
# rather than derived at runtime; the suite asserts the declaration matches the derivation.
TOOL_COMPOSITES: frozenset[str] = frozenset({"ThermochemistryResult", "LogdResult"})


# Keys that are a *presentation* of the result rather than part of it, dropped before the identity
# is taken. One entry, and it is not a general escape hatch — see `_composite_ref`.
_PRESENTATIONAL: frozenset[str] = frozenset({"modes"})


def _composite_ref(connector: str, tool: str, payload: dict[str, Any]) -> str:
    """The identity of one tool composite: the route it came from, plus what it produced.

    A composite has no cache key, so it is content-addressed on its *result*, not its request:
    sentinel defaults make two requests for one measurement, and a calculator change makes one
    request yield two measurements (and the outbox keeps the first forever). The result restates the
    parameters actually used and changes when the science does, so the same question is one record
    and a genuinely different result is a second.

    `_PRESENTATIONAL` fields are dropped before hashing: `modes` is truncated by the caller's
    `top_bands` and carries no physics the rest of the payload lacks (`mode_count` and every
    thermodynamic quantity survive). The route stays readable in front of the hash.
    """
    identity = {key: value for key, value in payload.items() if key not in _PRESENTATIONAL}
    return f"{connector}.{tool}#{stable_hash(identity)}"


async def publish_tool_result(
    *, connector: str, tool: str, arguments: dict[str, Any], result: Any
) -> int:
    """Offer one tool's own result to the external results store; never raise.

    Returns how many outbox rows were written; zero (no sink, or a shape this hook does not publish)
    is not a failure.

    Args:
        connector: The bundle serving the tool, the first half of the `calc_type` route. The shape
            is carried separately as `payload_kind`.
        tool: The tool's own name, the second half of that route.
        arguments: The validated keyword arguments the tool ran on, hashed into `input_hash`;
            never stored verbatim.
        result: Whatever the tool returned. Anything not a pydantic model named in
            `TOOL_COMPOSITES` is left alone.
    """
    if not publishing_enabled() or not isinstance(result, BaseModel):
        return 0
    kind = type(result).__name__
    if kind not in TOOL_COMPOSITES:
        return 0
    try:
        payload = result.model_dump(mode="json")
        return await enqueue_payload(
            calc_ref=_composite_ref(connector, tool, payload),
            # A route, exactly as the job hook builds one. It identifies where this came from; the
            # `payload_kind` beside it is what identifies the shape.
            calc_type=f"{connector}.{tool}",
            payload=payload,
            payload_kind=kind,
            # What was asked for (`calc_ref` is what came back): the raw validated arguments, so an
            # unstated default reads as unstated.
            input_hash=stable_hash(arguments),
        )
    except Exception:
        # Guards everything around `enqueue_payload`'s own no-raise promise; the counter keeps a
        # failure from being silent.
        logger.exception("publish[tool]: could not queue %s from %s.%s", kind, connector, tool)
        record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
        return 0


__all__ = ["TOOL_COMPOSITES", "publish_tool_result"]
