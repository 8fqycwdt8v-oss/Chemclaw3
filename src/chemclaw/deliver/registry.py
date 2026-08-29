"""Discover, enable and build delivery channels — the same five steps every seam here takes.

A folder holding a `channel.yaml`, found on a path list, enabled by name, driver resolved late, and
built per delivery. Written to `publish/registry.py`'s shape deliberately: an operator who has
attached a data source or a result sink already knows how to attach a channel, and a fourth
mechanism to learn would be the cost of a seam that bought nothing.

**Delivery is off until a deployment names a channel.** `CHEMCLAW_DELIVERY_CHANNELS` is empty by
default, which is not the "discovery is enablement" default the connector registry takes — and the
asymmetry is deliberate. A discovered connector serves a tool; a discovered channel *sends
something out of the building*, and turning that on by finding a folder is the shape
`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` was written about.
"""

import importlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics import METRICS
from chemclaw.deliver.driver import DeliveryDriver
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.message import Message

_MANIFEST = "channel.yaml"


class DeliveryChannelError(ChemclawError):
    """A channel could not be discovered, enabled or built."""


logger = logging.getLogger(__name__)


def _channel_dirs() -> list[Path]:
    """Every directory holding a `channel.yaml`, in discovery-path order.

    Earlier directories win a name collision — the precedence a `PATH` entry has — so a deployment
    can mount its own definition of a shipped channel without editing this repository.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for root in settings.delivery_channels_dirs:
        base = Path(root)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (child / _MANIFEST).is_file() and child.name not in seen:
                seen.add(child.name)
                found.append(child)
    return found


def _load(directory: Path) -> DeliveryChannelManifest:
    """Read and validate one manifest, rejecting a name that disagrees with its folder."""
    raw = yaml.safe_load((directory / _MANIFEST).read_text(encoding="utf-8")) or {}
    manifest = DeliveryChannelManifest.model_validate(raw)
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

    A name with no folder is an error rather than a skip: an operator who spelled a channel wrong
    means to be delivering and is not, which is the one failure a delivery seam must be loud about.
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


def delivery_enabled() -> bool:
    """Whether anything would be delivered at all.

    Read before assembling a message, so a deployment with nowhere to send costs one list lookup
    rather than a render — the same shape `publishing_enabled` has for the same reason.
    """
    return bool(settings.delivery_channel_list)


def _resolve(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise DeliveryChannelError(
            f"cannot import {module_name!r} for delivery driver {reference!r}: {exc}. A driver's "
            "client package is installed only where that channel is actually used."
        ) from exc
    driver = getattr(module, attribute, None)
    if driver is None:
        raise DeliveryChannelError(
            f"{module_name!r} has no attribute {attribute!r} (from {reference!r})"
        )
    if not callable(driver):
        raise DeliveryChannelError(f"{reference!r} is not callable")
    resolved: Callable[..., Any] = driver
    return resolved


def build(manifest: DeliveryChannelManifest) -> DeliveryDriver:
    """Build the driver a manifest describes.

    Uncached, for the reason the sink registry gives: a driver may hold a credential, and a cached
    one would outlive a rotation.
    """
    factory = _resolve(manifest.driver)
    try:
        driver = factory(name=manifest.name, **manifest.config)
    except TypeError as exc:
        raise DeliveryChannelError(
            f"delivery channel {manifest.name!r} cannot build {manifest.driver!r} from its "
            f"`config:` block: {exc}. The driver's own signature is the schema."
        ) from exc
    return driver  # type: ignore[no-any-return]


async def deliver(message: Message) -> list[str]:
    """Send one message on every enabled channel, and report which ones took it.

    **Redacted once, here, rather than in each driver.** A scrub every driver has to remember is a
    scrub the next driver forgets, and the one that forgets is the one that sends outside the
    cluster.

    **A failing channel does not stop the others**, the reject-and-continue discipline the ELN sync
    and the digest already use: one broken webhook must not cost every other recipient their
    message. The return value is what a caller advances a watermark on — "delivered" and
    "swallowed" are different facts, and `durable/digest.py` is the caller that must not conflate
    them.
    """
    scrubbed = message.redacted()
    delivered: list[str] = []
    for manifest in enabled():
        try:
            driver = build(manifest)
            await driver.deliver(scrubbed)
        except Exception as exc:
            # **Swallowed for the other channels' sake, and never silently.** Re-raising would make
            # a message undeliverable to everyone because it was undeliverable to one. But the
            # comment here used to say "logged by the caller with its own context" and there was no
            # such caller: nothing in this package held a logger or a metric, and the one caller
            # discarded the return value — so every digest being dropped and every digest being
            # delivered were the same observation from outside. That is the failure this seam was
            # built to end, one layer further in.
            logger.warning(
                "deliver.channel_failed: %s (%s): %s", manifest.name, manifest.driver, exc
            )
            METRICS.increment("chemclaw_delivery_failures_total", labels={"channel": manifest.name})
            continue
        delivered.append(manifest.name)
        METRICS.increment("chemclaw_deliveries_total", labels={"channel": manifest.name})
    return delivered
