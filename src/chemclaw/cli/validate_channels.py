"""Validate the delivery-channel manifests — `make channel-validate`.

Four checks pydantic cannot make from a manifest alone:

1. an **enabled** channel that no manifest declares;
2. a **driver** that cannot be imported or is not callable;
3. a **config block** the driver's signature will not accept (the callable is the schema);
4. a **cleartext destination** under the enforced posture.

Rules 2-4 run over every *discovered* manifest, not the enabled set: `CHEMCLAW_DELIVERY_CHANNELS`
is empty in CI, and a channel broken while disabled is one nobody can enable. Rule 4 lives here
because the driver's own refusal happens inside the per-channel `try` of `registry.deliver`, where
it reads as a delivery outage rather than a configuration fault.

Connects to nothing: reachability is a deployment fact.
"""

import argparse
import inspect
import logging
import sys
from urllib.parse import urlsplit

import yaml

from chemclaw.core.config import settings
from chemclaw.core.connect import ENV_SUFFIX, check_env_name
from chemclaw.core.logging import configure_logging
from chemclaw.deliver.driver import plaintext_channel_refusal
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
    # A `*_env` key holds the NAME of an environment variable, never the value; a pasted token here
    # would be committed, so this failure is a disclosure rather than an outage.
    for key, value in manifest.config.items():
        if not key.endswith(ENV_SUFFIX):
            continue
        try:
            check_env_name(key, str(value or ""), error=DeliveryChannelError)
        except DeliveryChannelError as exc:
            problems.append(f"{manifest.name}: {exc}")
    return problems


def _config_strings(value: object, depth: int = 3) -> list[str]:
    """Every string a driver could read a destination out of, to a bounded depth.

    `config:` is free-form, so a destination may be nested (`urls: [a, b]`,
    `endpoints: {primary: …}`, `targets: [{url: …}]`). `depth` counts container hops below the
    `config` dict; a plain string is always returned, and the guard only stops a container.
    `depth=3` reaches strings inside a list of per-target dicts; anything deeper is outside what
    rule 4 claims to see.
    """
    if isinstance(value, str):
        return [value]
    if depth <= 0:
        return []
    if isinstance(value, list):
        return [found for item in value for found in _config_strings(item, depth - 1)]
    if isinstance(value, dict):
        return [found for item in value.values() for found in _config_strings(item, depth - 1)]
    return []


def _posture_problems(manifest: DeliveryChannelManifest) -> list[str]:
    """A destination the enforced posture forbids (rule 4).

    Every `http`/`https` value in `config:` is asked, whatever its key, since drivers name their
    destination freely. Only those schemes are asked: hostless values such as file paths are not
    destinations, and must not depend on `PG_LOOPBACK_HOSTS` containing `''`.

    Asked with `enforced=True` unconditionally, since `settings.entra_required` is off in CI: a
    manifest
    that will be refused once enforcement is on is broken today. The rule itself is
    `deliver.driver.plaintext_channel_refusal`, one definition for construction and validation.
    """
    token_env = str(manifest.config.get("token_env", "") or "")
    urls = [
        found
        for found in _config_strings(manifest.config)
        if urlsplit(found).scheme in ("http", "https")
    ]
    reasons = (
        plaintext_channel_refusal(manifest.name, url, token_env, enforced=True) for url in urls
    )
    return [reason for reason in reasons if reason]


def problems() -> list[str]:
    """Every finding across every discovered channel, plus rule 1 over the enabled set.

    Zero discovered manifests is itself a finding: a gate iterating nothing cannot fail.
    """
    try:
        manifests = discovered()
    except (DeliveryChannelError, OSError, yaml.YAMLError) as exc:
        # One problem line rather than a traceback. `OSError`/`yaml.YAMLError` are caught too
        # because
        # `deliver.registry._load` does not wrap them.
        return [f"cannot read a delivery channel manifest: {exc}"]

    if not manifests:
        # Zero discovered manifests leaves rules 2-4 checking nothing, so a typo in the `PATH`-style
        # `CHEMCLAW_DELIVERY_CHANNELS_DIR` would silently disable the plaintext refusal.
        return [
            f"no delivery channels discovered under {settings.delivery_channels_dir!r} — no "
            "driver, no config block and no destination posture would be checked, and this gate "
            "would have checked nothing"
        ]
    found = _enabled_problems(manifests)
    for manifest in manifests.values():
        found.extend(_driver_problems(manifest))
        found.extend(_posture_problems(manifest))
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
