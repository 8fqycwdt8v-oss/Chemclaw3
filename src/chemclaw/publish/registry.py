"""Discovering result sinks, and building the ones a deployment enabled.

Mirrors `ingest/sources/registry.py` deliberately, down to the late binding: discovery reads
manifests and imports nothing, so the one fact needed in order to *skip* a sink — its name — is
available as data. A process that publishes nothing never imports a database client.

**Discovery is not enablement** (D-018). Every sink this repository ships is discovered; a
deployment publishes to the subset it names in `CHEMCLAW_RESULT_SINKS`, and that list is **empty by
default**. A system that began shipping every calculation to a destination on a default nobody
chose would be the exact failure this seam exists to make deliberate.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.manifest_io import read_manifest, resolve_driver, within_root
from chemclaw.publish.driver import ResultSink, SinkUnavailableError
from chemclaw.publish.manifest import ResultSinkManifest
from chemclaw.publish.record import ResultRecord

logger = logging.getLogger(__name__)

_MANIFEST = "sink.yaml"


class ResultSinkError(ChemclawError):
    """A sink could not be discovered, enabled or built."""


def _sink_dirs() -> list[Path]:
    """Every directory holding a `sink.yaml`, in discovery-path order.

    Earlier directories win a name collision — the precedence a `PATH` entry has — so a deployment
    can mount a folder with its own definition of a shipped sink and have it take effect without
    editing this repository.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for root in settings.result_sinks_dirs:
        base = Path(root)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (
                (child / _MANIFEST).is_file()
                and child.name not in seen
                and within_root(base, child)
            ):
                seen.add(child.name)
                found.append(child)
    return found


def _load(directory: Path) -> ResultSinkManifest:
    """Read and validate one manifest, rejecting a name that disagrees with its folder.

    The disagreement matters because the *folder* is what discovery finds while the *name* is what
    the enable list and every `result_publications.sink` row hold; letting them differ would make a
    sink enabled under one name and recorded under another.
    """
    path = directory / _MANIFEST
    raw = read_manifest(path, ResultSinkError)
    try:
        manifest = ResultSinkManifest.model_validate(raw)
    except ValidationError as exc:
        raise ResultSinkError(f"invalid result sink manifest {path}:\n{exc}") from exc
    if manifest.name != directory.name:
        raise ResultSinkError(
            f"result sink manifest {path} declares name {manifest.name!r} but lives in "
            f"directory {directory.name!r}; they must match"
        )
    return manifest


@cache
def discovered() -> dict[str, ResultSinkManifest]:
    """Every sink manifest on the discovery path, by name. Imports nothing."""
    return {directory.name: _load(directory) for directory in _sink_dirs()}


def enabled() -> list[ResultSinkManifest]:
    """The manifests this deployment publishes to, in the order it named them.

    An enabled name with no manifest is a startup error rather than a silent skip: a deployment
    that believes it is publishing and is not would look identical to one with nothing to publish,
    and that is the failure mode this whole subsystem is built to end.
    """
    available = discovered()
    manifests: list[ResultSinkManifest] = []
    for name in settings.result_sink_list:
        if name not in available:
            raise ResultSinkError(
                f"CHEMCLAW_RESULT_SINKS names {name!r}, which no manifest declares. "
                f"Discovered: {sorted(available) or 'none'}."
            )
        manifests.append(available[name])
    return manifests


def enabled_names() -> list[str]:
    """The enabled sink names — the cheap question, answered without building anything."""
    return [manifest.name for manifest in enabled()]


def publishing_enabled() -> bool:
    """Whether this deployment publishes results anywhere.

    Read by the enqueue path so that, with no sink configured, publishing costs one list lookup
    and not a database write — and by `planned_schedules`, so a deployment with nowhere to publish
    does not carry a Temporal Schedule that drains an always-empty queue.
    """
    return bool(settings.result_sink_list)


def _resolve(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it, or fail naming both halves of the reference.

    Typed as callable rather than `object` because this function's last act is to check that it is
    one — a caller that then has to re-narrow would be re-doing the check this already did.
    """
    resolved: Callable[..., Any] = resolve_driver(reference, ResultSinkError, "result sink driver")
    return resolved


class _BoundedSink:
    """A sink whose every call is bounded by `result_publish_timeout_seconds`.

    **The setting was documented as a per-`deliver` ceiling and enforced by nobody.** Its own
    declaration reads *"how long one `deliver` may take before the drain gives up on that batch and
    leaves its rows pending"*; what actually existed was the *activity's*
    `result_publish_timeout_seconds x len(sinks)` — one budget for the whole sequential loop. So a
    single hanging destination consumed the entire pass and every sink later in
    `CHEMCLAW_RESULT_SINKS` was never reached. Measured with `alpha` hanging and `beta` healthy
    over eight passes: `beta` was claimed **zero** times, its rows sat at `attempts=0` with no
    `last_error`, and nothing distinguished "starved" from "nothing to send" — while
    `durable/publish_results.py`'s module docstring gave two failure domains as the reason for the
    design. That is true of the *rows* and was false of the *pass*.

    **Here rather than in the drain loop, because the bound belongs to the seam.** Every sink this
    registry builds is bounded, so the guarantee does not depend on which caller drains — the
    backfill CLI and any later caller get it for free — and the activity's `x len(sinks)` budget
    becomes the honest sum of N per-sink budgets rather than one pool the first sink can drink.

    A timeout is a `SinkUnavailableError`: the destination did not answer, which is the retryable
    half of the contract, and it is what puts the reason into `result_publications.last_error`
    where an operator reads it. `aclose` is bounded too and *swallows* its timeout — a driver that
    will not let go of a connection must not also cost the next sink its pass, and the drain calls
    it from a `finally` that has nothing to do with delivery.
    """

    def __init__(self, name: str, sink: ResultSink, timeout_seconds: float) -> None:
        """Wrap `sink`, naming it for the errors and log lines this class raises."""
        self._name = name
        self._sink = sink
        self._timeout = timeout_seconds

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Deliver, giving up at the per-sink ceiling rather than holding the pass."""
        try:
            async with asyncio.timeout(self._timeout):
                await self._sink.deliver(records)
        except TimeoutError as exc:
            raise SinkUnavailableError(
                f"result sink {self._name!r} did not finish delivering {len(records)} row(s) "
                f"within result_publish_timeout_seconds ({self._timeout}s); the batch stays "
                "pending"
            ) from exc

    async def aclose(self) -> None:
        """Release the sink, bounded — a driver that will not close must not starve the next one."""
        try:
            async with asyncio.timeout(self._timeout):
                await self._sink.aclose()
        except TimeoutError:
            logger.warning(
                "result sink %r did not close within %ss; continuing to the next sink",
                self._name,
                self._timeout,
            )


def build(manifest: ResultSinkManifest) -> ResultSink:
    """Build the sink a manifest describes, bounded by the per-sink delivery ceiling.

    Deliberately uncached: a sink holds a connection, and a cached one would outlive a credential
    rotation. The drain builds per run, which is coarse enough that the construction cost does not
    matter and fine enough that a rotated secret takes effect on the next pass.

    What comes back is a `_BoundedSink` around the driver, never the driver itself — see that
    class for the starvation that made the wrapper the seam's job rather than a caller's.
    """
    factory = _resolve(manifest.driver)
    try:
        sink = factory(
            name=manifest.name,
            tenant_id=manifest.tenant_id or manifest.name,
            **manifest.config,
        )
    except TypeError as exc:
        # Re-framed so the message names the manifest and the driver rather than surfacing as an
        # opaque signature error from inside a vendor client — the same courtesy the data-source
        # seam extends for exactly this mistake.
        raise ResultSinkError(
            f"result sink {manifest.name!r}: driver {manifest.driver!r} does not accept the "
            f"config it was given ({sorted(manifest.config)}): {exc}"
        ) from exc
    if not isinstance(sink, ResultSink):
        raise ResultSinkError(
            f"result sink {manifest.name!r}: {manifest.driver!r} did not build a ResultSink "
            "(it must expose an async `deliver(records)`)"
        )
    # Checked *before* wrapping, so the error still names what the driver failed to be rather than
    # what this module wrapped it in.
    return _BoundedSink(manifest.name, sink, settings.result_publish_timeout_seconds)
