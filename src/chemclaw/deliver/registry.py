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

    The read and the parse are wrapped for the same reason `publish/registry._load` wraps them: an
    unreadable or malformed `channel.yaml` is a *manifest* problem, and a caller that catches
    `DeliveryChannelError` — `channel-validate` is one — should not have to also catch `OSError`
    and `yaml.YAMLError` to report it. It did have to: wave 4 of the 2026-09 re-review drove a
    malformed manifest through the validator and got a raw traceback where its sibling reported a
    line. The CLI compensated; the asymmetry belonged here.
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
    resolved: Callable[..., Any] = resolve_driver(
        reference, DeliveryChannelError, "delivery driver"
    )
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
    if not isinstance(driver, DeliveryDriver):
        # The check `publish/registry.build` already made for a sink, and this seam needed more
        # than that one: a channel is the surface whose whole job is to leave the building, so a
        # factory that built the wrong thing must fail here rather than at send time, when the
        # message that was supposed to reach a person is the thing being dropped.
        raise DeliveryChannelError(
            f"delivery channel {manifest.name!r}: {manifest.driver!r} did not build a "
            "DeliveryDriver (it must expose an async `deliver(message)`)"
        )
    return driver


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

    **A channel that cannot be built and a channel that would not answer are different facts**, and
    they are reported on different series — the first through `degraded()`, the second on
    `chemclaw_delivery_failures_total`. Both continue to the next channel; only one of them will
    still be true tomorrow.
    """
    scrubbed = message.redacted()
    delivered: list[str] = []
    for manifest in enabled():
        try:
            driver = build(manifest)
        except Exception as exc:
            # **A channel that cannot be *built* is a configuration fault, not a destination
            # outage, and sharing one counter with the outage hid it.** A bad `config:` block, an
            # unimportable driver, or a destination this deployment's posture forbids will fail
            # identically on every message for the life of the process — while
            # `chemclaw_delivery_failures_total` is the series an operator reads as "the webhook
            # host is having a bad afternoon". `degraded()` is this repository's chokepoint for
            # exactly that distinction: it counts `chemclaw_degraded_total{subsystem}`, which is
            # alerted rather than skimmed. `make channel-validate` is where such a channel should
            # have been caught; this is the second line, for the deployment that did not run it.
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
