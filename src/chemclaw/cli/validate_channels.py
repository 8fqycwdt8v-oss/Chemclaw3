"""Validate the delivery-channel manifests — `make channel-validate`.

Four checks pydantic cannot make from a manifest alone, in the shape `validate_sinks` established.
Rule 1 is a property of the enabled set; rules 2 to 4 run over every **discovered** manifest,
because a channel that is broken while disabled is a channel nobody can enable:

1. an **enabled** channel that no manifest declares — a deployment believing it delivers and not
   doing so is indistinguishable from one with nothing to deliver;
2. a **driver** that cannot be imported or is not callable;
3. a **config block** the driver's signature will not accept — the same "the callable is the
   schema" rule the other three seams apply, checked by binding rather than by a second model;
4. a **cleartext destination** under the enforced posture.

Rules 2 and 3 run over *discovered* rather than enabled manifests for the reason `validate_sinks`
records: `CHEMCLAW_DELIVERY_CHANNELS` is empty in CI, so iterating the enabled set would resolve
zero drivers and bind zero config blocks — a gate that could only ever fail on rule 1, which is
empty too.

**Rule 4 needed the same workaround one layer in, and shipped without it.** It asks the posture
question with `enforced=True` rather than letting `plaintext_channel_refusal` read
`settings.entra_required`, which is `False` by default and set by nothing that invokes this gate —
so the rule written to stop a plaintext channel merging silently was itself inert in CI. A gate
that only fires once the setting it guards is already on is not a gate.

**Rule 4 is here because it had nowhere else to be.** `deliver.driver` refuses a non-loopback
`http://` channel under `entra_required`, and that raise happens inside driver construction — which
`registry.deliver` performs inside the per-channel `try` that exists so one broken channel does not
cost every other recipient their message. Measured, the refusal therefore never refused: the
channel stayed enabled, every delivery returned `[]`, and the single WARNING per message read as
the destination being down. A posture violation is a *configuration* fault, so it belongs in the
gate an operator runs before delivering, and this is that gate.


Deliberately does **not** connect to anything. A channel's reachability is a deployment fact, and
this runs in CI with no webhook host and no mounted share in sight.
"""

import argparse
import inspect
import logging
import sys
from urllib.parse import urlsplit

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


def _config_strings(value: object, depth: int = 2) -> list[str]:
    """Every string a driver could read a destination out of, to a bounded depth.

    The `config:` block is free-form by design — the driver's own signature is the schema — so a
    destination is not always a top-level string. A site's driver may take `urls: [a, b]` for a
    fan-out, or `endpoints: {primary: …, fallback: …}`; the first version of rule 4 looked only at
    top-level `str` values, so both of those shapes passed a check written to catch exactly them.

    Bounded rather than fully recursive on purpose. This is not a config-schema validator: it walks
    the three shapes a destination is realistically written in — a bare string, a list, and one
    level of nesting inside either — and stops. A driver that buries its URL deeper than that is
    outside what this rule claims to see, which is better stated here than believed.
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

    Every value in the `config:` block that *is* a URL is asked, rather than a key named `url`. The
    block is free-form by design — the driver's own signature is the schema — so a site's driver may
    call its destination `endpoint`, `webhook_url` or `hook`, and a check that only knew one
    spelling would pass every channel it was written to catch.

    **The scheme test is what makes that safe, and leaving it out only worked by accident.**
    `plaintext_channel_refusal` exempts a loopback host, and `PG_LOOPBACK_HOSTS` contains `''` — so
    a value with no host at all (`/var/chemclaw/outbox`, `.md`) was already answered `""`, and the
    shipped `share` channel passed for a reason that has nothing to do with delivery: that empty
    string is there so a Postgres DSN with no host reads as local. Depending on it would mean a
    change to a Postgres constant silently refusing every file channel as a cleartext destination.
    So this asks only about `http`/`https` values, and says so.

    **`enforced=True` unconditionally**, which is the same workaround rules 2 and 3 already take one
    step further out. Those two iterate *discovered* rather than enabled manifests because
    `CHEMCLAW_DELIVERY_CHANNELS` is empty in CI; this one had the identical blindness one layer in,
    because `plaintext_channel_refusal` read `settings.entra_required` — `False` by default, `False`
    in CI, and set by nothing in `.github/workflows/ci.yml`, the chart or the runbook that invokes
    this gate. So the rule written to stop an enabled plaintext channel merging silently merged
    silently itself. A validator must ask the question the deployment is heading for, not the one
    its own ambient config already answers: a manifest that will be refused the day enforcement is
    turned on is a broken manifest today.

    The rule itself comes from `deliver.driver` rather than a second copy here: one definition,
    asked at construction *and* at validation, differing only in who supplies the posture.
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
    """Every finding across every discovered channel, plus rule 1 over the enabled set."""
    manifests = discovered()
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
