"""Discovering result sinks, and building the ones a deployment enabled.

Mirrors `ingest/sources/registry.py`: discovery reads manifests and imports nothing, so a process
that publishes nothing never imports a database client. Discovery is not enablement (D-018): a
deployment publishes only to the sinks named in `CHEMCLAW_RESULT_SINKS`, empty by default.
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

    Earlier directories win a name collision, like `PATH`, so a deployment can override a shipped
    sink by mounting a folder.
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

    Discovery finds the folder while the enable list and `result_publications.sink` hold the name;
    they must agree.
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

    An enabled name with no manifest is a startup error: silently not publishing would look
    identical
    to having nothing to publish.
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

    Lets the enqueue path skip the database and `planned_schedules` omit the drain schedule when no
    sink is configured.
    """
    return bool(settings.result_sink_list)


def unpublishable_reason() -> str | None:
    """Why this deployment cannot republish anything, or `None` when a sink is enabled.

    Both the `republish_calculations` launcher's `unavailable_reason` and its guard's message, so
    the
    two cannot disagree.
    """
    if publishing_enabled():
        return None
    return (
        "no result sink is enabled (CHEMCLAW_RESULT_SINKS is empty), so a republish would scan "
        "the whole stored corpus and queue nothing. Enable a sink first."
    )


def _resolve(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    resolved: Callable[..., Any] = resolve_driver(reference, ResultSinkError, "result sink driver")
    return resolved


class _BoundedSink:
    """A sink whose every call is bounded by `result_publish_timeout_seconds`.

    Without a per-sink bound, one hanging destination would consume the whole drain pass and starve
    every later sink. Bounding here, at the seam, gives every caller the guarantee. A timeout is a
    `SinkUnavailableError` (retryable, recorded in `last_error`). `aclose` is bounded too and
    swallows its timeout, so a driver that will not let go cannot cost the next sink its pass.
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

    Uncached: a sink holds a connection, and building per drain run lets a rotated credential take
    effect on the next pass. Always returns a `_BoundedSink`, never the bare driver.
    """
    factory = _resolve(manifest.driver)
    try:
        sink = factory(
            name=manifest.name,
            tenant_id=manifest.tenant_id or manifest.name,
            **manifest.config,
        )
    except TypeError as exc:
        # Re-framed to name the manifest and the driver rather than an opaque signature error.
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
