"""Discover, enable and build delivery channels — the same five steps every seam here takes.

A folder holding a `channel.yaml`, found on a path list, enabled by name, driver resolved late,
and built per delivery, on `publish/registry.py`'s shape. Delivery is off until
`CHEMCLAW_DELIVERY_CHANNELS` names a channel: unlike a connector, a discovered channel sends
something out of the building, so finding a folder must not enable it.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.manifest_io import read_manifest, resolve_driver, within_root
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.deliver.driver import DeliveryDriver
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.message import Message

_MANIFEST = "channel.yaml"


class DeliveryChannelError(ChemclawError):
    """A channel could not be discovered, enabled or built."""


logger = logging.getLogger(__name__)


def _channel_dirs() -> list[Path]:
    """Every directory holding a `channel.yaml`, in discovery-path order.

    Earlier directories win a name collision, so a deployment can mount its own definition of a
    shipped channel.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for root in settings.delivery_channels_dirs:
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


def _load(directory: Path) -> DeliveryChannelManifest:
    """Read and validate one manifest, rejecting a name that disagrees with its folder.

    Read and parse errors are raised as `DeliveryChannelError`, so a caller such as
    `channel-validate` need not also catch `OSError` and `yaml.YAMLError`.
    """
    path = directory / _MANIFEST
    raw = read_manifest(path, DeliveryChannelError)
    try:
        manifest = DeliveryChannelManifest.model_validate(raw)
    except ValidationError as exc:
        raise DeliveryChannelError(f"invalid delivery channel manifest {path}:\n{exc}") from exc
    if manifest.name != directory.name:
        raise DeliveryChannelError(
            f"delivery channel in {directory} declares name {manifest.name!r}; the folder is the "
            f"key an operator enables, so it must be {directory.name!r}"
        )
    return manifest


def discovered() -> dict[str, DeliveryChannelManifest]:
    """Every channel on the discovery path, by name."""
    return {directory.name: _load(directory) for directory in _channel_dirs()}


def enabled() -> list[DeliveryChannelManifest]:
    """The channels this deployment named, in the order it named them.

    A name with no folder is an error rather than a skip: a misspelled channel means a deployment
    that believes it is delivering and is not.
    """
    available = discovered()
    chosen: list[DeliveryChannelManifest] = []
    for name in settings.delivery_channel_list:
        manifest = available.get(name)
        if manifest is None:
            raise DeliveryChannelError(
                f"CHEMCLAW_DELIVERY_CHANNELS names {name!r}, which is not on the discovery path "
                f"(found: {sorted(available) or 'nothing'})"
            )
        chosen.append(manifest)
    return chosen


def resolvable() -> tuple[list[DeliveryChannelManifest], list[str]]:
    """The channels that resolve, and the names that do not — for a caller that must still send.

    Unlike `enabled()`, one bad name does not take the healthy channels down with it; the caller
    reports the unresolved names through `degraded()` and still delivers to the rest.
    """
    available = discovered()
    chosen: list[DeliveryChannelManifest] = []
    unresolved: list[str] = []
    for name in settings.delivery_channel_list:
        manifest = available.get(name)
        if manifest is None:
            unresolved.append(name)
        else:
            chosen.append(manifest)
    return chosen, unresolved


def delivery_enabled() -> bool:
    """Whether anything would be delivered at all.

    Read before assembling a message, so a deployment with nowhere to send skips the render.
    """
    return bool(settings.delivery_channel_list)


def _resolve(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    resolved: Callable[..., Any] = resolve_driver(
        reference, DeliveryChannelError, "delivery driver"
    )
    return resolved


def build(manifest: DeliveryChannelManifest) -> DeliveryDriver:
    """Build the driver a manifest describes.

    Uncached: a driver may hold a credential, and a cached one would outlive a rotation.
    """
    factory = _resolve(manifest.driver)
    try:
        driver = factory(name=manifest.name, **manifest.config)
    except TypeError as exc:
        raise DeliveryChannelError(
            f"delivery channel {manifest.name!r} cannot build {manifest.driver!r} from its "
            f"`config:` block: {exc}. The driver's own signature is the schema."
        ) from exc
    if not isinstance(driver, DeliveryDriver):
        # A factory that built the wrong thing fails here, not at send time when a message is
        # dropped.
        raise DeliveryChannelError(
            f"delivery channel {manifest.name!r}: {manifest.driver!r} did not build a "
            "DeliveryDriver (it must expose an async `deliver(message)`)"
        )
    return driver


async def deliver(message: Message) -> list[str]:
    """Send one message on every enabled channel, and report which ones took it.

    Redacted once, here, rather than in each driver. A failing channel does not stop the others.
    The return value is what a caller advances a watermark on: "delivered" and "swallowed" are
    different facts. A channel that cannot be built is reported through `degraded()`; one that
    would not answer, on `chemclaw_delivery_failures_total`.
    """
    scrubbed = message.redacted()
    delivered: list[str] = []
    channels, unresolved = resolvable()
    if unresolved:
        # Counted rather than raised, so the channels that *do* resolve still receive this message.
        # See `resolvable` for the measurement this replaces.
        degraded(
            logger,
            "delivery_channel_config",
            "CHEMCLAW_DELIVERY_CHANNELS names %s, which is not on the discovery path; the other "
            "%d channel(s) still received this message",
            ", ".join(repr(name) for name in unresolved),
            len(channels),
            exc_info=False,
        )
    for manifest in channels:
        try:
            driver = build(manifest)
        except Exception as exc:
            # A channel that cannot be built is a configuration fault that fails every message, so
            # it goes to the alerted `degraded()` counter rather than the outage counter.
            degraded(
                logger,
                "delivery_channel_config",
                "delivery channel %s (%s) cannot be built, so it will deliver nothing until its "
                "configuration is fixed: %s",
                manifest.name,
                manifest.driver,
                exc,
                exc_info=False,
            )
            continue
        try:
            await driver.deliver(scrubbed)
        except Exception as exc:
            # Swallowed so one destination cannot block the others, but logged and counted.
            logger.warning(
                "deliver.channel_failed: %s (%s): %s", manifest.name, manifest.driver, exc
            )
            METRICS.increment("chemclaw_delivery_failures_total", labels={"channel": manifest.name})
            continue
        delivered.append(manifest.name)
        METRICS.increment("chemclaw_deliveries_total", labels={"channel": manifest.name})
    return delivered
