"""Validate the delivery-channel manifests — `make channel-validate`.

Three checks pydantic cannot make from a manifest alone, in the shape `validate_sinks` established.
Rule 1 is a property of the enabled set; rules 2 and 3 run over every **discovered** manifest,
because a channel that is broken while disabled is a channel nobody can enable:

1. an **enabled** channel that no manifest declares — a deployment believing it delivers and not
   doing so is indistinguishable from one with nothing to deliver;
2. a **driver** that cannot be imported or is not callable;
3. a **config block** the driver's signature will not accept — the same "the callable is the
   schema" rule the other three seams apply, checked by binding rather than by a second model.

Rules 2 and 3 run over *discovered* rather than enabled manifests for the reason `validate_sinks`
records: `CHEMCLAW_DELIVERY_CHANNELS` is empty in CI, so iterating the enabled set would resolve
zero drivers and bind zero config blocks — a gate that could only ever fail on rule 1, which is
empty too.

Deliberately does **not** connect to anything. A channel's reachability is a deployment fact, and
this runs in CI with no webhook host and no mounted share in sight.
"""

import argparse
import inspect
import logging
import sys

from chemclaw.core.config import settings
from chemclaw.core.connect import ENV_SUFFIX, check_env_name
from chemclaw.core.logging import configure_logging
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.registry import DeliveryChannelError, _resolve, discovered

logger = logging.getLogger(__name__)


def _enabled_problems(manifests: dict[str, DeliveryChannelManifest]) -> list[str]:
    """An enabled name with no manifest (rule 1)."""
    return [
        f"CHEMCLAW_DELIVERY_CHANNELS names {name!r}, which no manifest declares "
        f"(discovered: {sorted(manifests) or 'none'})"
        for name in settings.delivery_channel_list
        if name not in manifests
    ]


def _driver_problems(manifest: DeliveryChannelManifest) -> list[str]:
    """A driver that will not resolve, or will not take its config (rules 2 and 3)."""
    try:
        driver = _resolve(manifest.driver)
    except DeliveryChannelError as exc:
        return [f"{manifest.name}: {exc}"]

    problems: list[str] = []
    supplied = {"name": manifest.name, **manifest.config}
    try:
        # Bound rather than called: constructing a driver may open a client, and this check must
        # run against no destination at all.
        inspect.signature(driver).bind(**supplied)
    except TypeError as exc:
        problems.append(
            f"{manifest.name}: driver {manifest.driver!r} does not accept its config "
            f"({sorted(manifest.config)}): {exc}"
        )
    # A `*_env` key holds the NAME of an environment variable, never the value. The realistic
    # mistake is a pasted token, which would then be committed in a manifest — so this is the one
    # check here whose failure is a disclosure rather than an outage.
    for key, value in manifest.config.items():
        if not key.endswith(ENV_SUFFIX):
            continue
        try:
            check_env_name(key, str(value or ""), error=DeliveryChannelError)
        except DeliveryChannelError as exc:
            problems.append(f"{manifest.name}: {exc}")
    return problems


def problems() -> list[str]:
    """Every finding across every discovered channel, plus rule 1 over the enabled set."""
    manifests = discovered()
    found = _enabled_problems(manifests)
    for manifest in manifests.values():
        found.extend(_driver_problems(manifest))
    return found


def main(argv: list[str] | None = None) -> int:
    """Report every problem, or confirm the manifests are sound."""
    parser = argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_channels", description=__doc__
    )
    parser.parse_args(argv)
    configure_logging()

    found = problems()
    for problem in found:
        sys.stderr.write(f"delivery channel: {problem}\n")
    if found:
        return 1
    logger.info(
        "delivery channels: %d discovered, %d enabled",
        len(discovered()),
        len(settings.delivery_channel_list),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
