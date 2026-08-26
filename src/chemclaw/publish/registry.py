"""Discovering result sinks, and building the ones a deployment enabled.

Mirrors `ingest/sources/registry.py` deliberately, down to the late binding: discovery reads
manifests and imports nothing, so the one fact needed in order to *skip* a sink — its name — is
available as data. A process that publishes nothing never imports a database client.

**Discovery is not enablement** (D-018). Every sink this repository ships is discovered; a
deployment publishes to the subset it names in `CHEMCLAW_RESULT_SINKS`, and that list is **empty by
default**. A system that began shipping every calculation to a destination on a default nobody
chose would be the exact failure this seam exists to make deliberate.
"""

import importlib
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.publish.driver import ResultSink
from chemclaw.publish.manifest import ResultSinkManifest

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
            if (child / _MANIFEST).is_file() and child.name not in seen:
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
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ResultSinkError(f"cannot read result sink manifest {path}: {exc}") from exc
    manifest = ResultSinkManifest.model_validate(raw)
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
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ResultSinkError(
            f"cannot import {module_name!r} for result sink driver {reference!r}: {exc}. "
            "A driver's client package is installed only where that sink is actually written to."
        ) from exc
    driver = getattr(module, attribute, None)
    if driver is None:
        raise ResultSinkError(
            f"{module_name!r} has no attribute {attribute!r} (from {reference!r})"
        )
    if not callable(driver):
        raise ResultSinkError(f"{reference!r} is not callable")
    resolved: Callable[..., Any] = driver
    return resolved


def build(manifest: ResultSinkManifest) -> ResultSink:
    """Build the sink a manifest describes.

    Deliberately uncached: a sink holds a connection, and a cached one would outlive a credential
    rotation. The drain builds per run, which is coarse enough that the construction cost does not
    matter and fine enough that a rotated secret takes effect on the next pass.
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
    return sink
