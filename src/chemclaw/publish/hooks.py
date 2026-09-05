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
from chemclaw.publish.composites import record_composite
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


# Keys that are a *presentation* of the result rather than part of it, dropped before the identity
# is taken. One entry, and it is not a general escape hatch — see `_composite_ref`.
_PRESENTATIONAL: frozenset[str] = frozenset({"modes"})


def _composite_ref(connector: str, tool: str, payload: dict[str, Any]) -> str:
    """The identity of one tool composite: the route it came from, plus what it produced.

    A composite has no cache key — that is the definition of one here — so it has no key to
    borrow. **It is content-addressed on its own result instead of on its request**, and the two
    are not interchangeable; hashing the request was wrong in both directions and both were
    measured (`D-2026-08-27-a-composite-needs-a-hook-not-a-projector`'s hook, audited after it
    shipped):

    - *Two requests, one measurement.* Both tool composites take a **sentinel** default — `ph=None`
      resolved to `settings.logd_default_ph`, `temperature_k=0.0` to `settings.xtb_thermo_
      temperature_k` — so the caller who omits the parameter and the caller who passes the value it
      resolves to send different arguments and get the identical answer. On the request hash that
      is two rows for one measurement. The result restates the parameter it actually used
      (`LogdResult.ph`, `ThermochemistryResult.temperature_k`), so on the payload it is one.
    - *One request, two measurements.* The outbox's identity is `(sink, calc_ref, schema_version)`
      and a delivered row is kept forever, so a ref that does not move when the science moves makes
      the **first** computation permanent: re-running the same question after a calculator or epoch
      change queued a genuinely different result and `ON CONFLICT DO NOTHING` dropped it. The two
      older hooks do not have this problem because their refs carry a version — the cache key's
      `calc_version` and epoch-folded `params_hash`, and the job's workflow id. A composite has no
      version of its own to carry: its parts each have one, they are not visible at this seam, and
      `publish` may not import `science` to reach them (`tests/test_layering.py`). What *is*
      visible is that the numbers came out different, which is the same fact one step later.

    So the same question asked twice is one record — the payload is deterministic for both shapes
    (a `structure_id` is a content address, and neither model carries a timing or a timestamp) —
    while the same molecule at a second temperature, or the same temperature after the calculator
    moved, is a second record. Both are second measurements.

    **What the payload is hashed *without*, and why that is not a hole in the above.** Moving the
    identity onto the result put a presentational argument inside it. `compute_thermochemistry`
    takes `top_bands`, which does nothing to the physics: it truncates `modes` to
    `result.strongest_bands(limit)` on the way out, for a caller's context budget. Reproduced: the
    same molecule at the same temperature with `top_bands=200` and with the default produced
    identical `structure_id`, `gibbs_free_energy_hartree` and `temperature_k`, and two different
    permanent `calc_ref`s — one physical measurement, two rows kept forever, which is exactly the
    "two requests, one measurement" defect above coming back in the other seam.

    So `modes` is dropped before the hash rather than canonicalised. Canonicalising would mean
    reconstructing the untruncated list, and the truncation is lossy in one direction only — a
    smaller `top_bands` is a subset of a larger one, and this seam holds neither the full set nor
    the limit that produced the subset, so there is nothing here to reconstruct it from. Dropping
    it costs no physics: `mode_count` is the honest count of the full set and survives truncation by
    its own contract, `imaginary_frequencies_cm` and `lowest_wavenumbers_cm` are stated as always
    coming from the *full* set for the same reason, and every thermodynamic quantity — the
    electronic energy, the ZPE, the enthalpy, the entropy, the Gibbs free energy — is in the payload
    unchanged. A genuinely different spectrum is a different Hessian, and a different Hessian moves
    all of those. `_PRESENTATIONAL` is one key rather than a policy: a field belongs there only when
    a caller's argument decides it and no measurement does.

    The route stays in front of the hash rather than being folded into it, so a `calc_ref` still
    says where it came from when it is read by a person.
    """
    identity = {key: value for key, value in payload.items() if key not in _PRESENTATIONAL}
    return f"{connector}.{tool}#{stable_hash(identity)}"


async def publish_tool_result(
    *, connector: str, tool: str, arguments: dict[str, Any], result: Any
) -> int:
    """Offer one tool's own result to the external results store; never raise.

    Returns how many outbox rows were written, which a caller may ignore: by the time this runs the
    tool has already computed its answer, and a results store that cannot be queued to is strictly
    less important than returning it. Zero is the ordinary answer — no sink configured, or a result
    this hook does not publish — and is not a failure.

    **The recovery path, stated because until now there was none.** A declared composite is written
    to `result_composites` *before* it is queued, and `publish/backfill.backfill_composites` walks
    that table. So a composite whose enqueue failed, or one computed before a results store was
    attached, is re-queued by the same backfill that recovers the calculation cache and the job
    record — the property the other two published shapes always had and this one did not.

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
    if not isinstance(result, BaseModel):
        return 0
    kind = type(result).__name__
    if kind not in TOOL_COMPOSITES:
        return 0
    try:
        payload = result.model_dump(mode="json")
        calc_ref = _composite_ref(connector, tool, payload)
        # **The local record first, and it is written whether or not a sink is enabled.** It is the
        # only durable copy this shape has: a primitive is recovered from `calculation_results` and
        # a job composite from `job_records`, and a tool composite is in neither by construction —
        # so before this the enqueue below was the sole copy, and `enqueue_payload` swallows every
        # failure by design. See `publish/composites.py` for what that costs and why it is paid.
        await record_composite(
            calc_ref=calc_ref,
            calc_type=f"{connector}.{tool}",
            payload_kind=kind,
            input_hash=stable_hash(arguments),
            payload=payload,
        )
        if not publishing_enabled():
            return 0
        return await enqueue_payload(
            calc_ref=calc_ref,
            # A route, exactly as the job hook builds one. It identifies where this came from; the
            # `payload_kind` beside it is what identifies the shape.
            calc_type=f"{connector}.{tool}",
            payload=payload,
            payload_kind=kind,
            # The request, which is a different fact from the identity above: `input_hash` is what
            # was asked for and `calc_ref` is what came back. It is deliberately still the raw
            # validated arguments — an unstated default reads as unstated here, which is what the
            # caller actually sent.
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
