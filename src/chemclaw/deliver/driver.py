"""What a delivery channel has to be, and the two this release ships.

One method. A driver takes a `Message` and gets it to a person; it returns nothing and raises on
failure, so Temporal's activity retry is the durability rather than a second retry loop here.

**Both shipped drivers are deliberately unglamorous**, and neither holds a mail client. The seam is
the deliverable: a site that wants Exchange, Teams or Slack writes a `module:callable` and names it
in a manifest, exactly as it would for a warehouse ELN or a result store. Shipping a credentialed
client for one vendor would make that vendor's shape the seam's shape, which is the mistake
`D-2026-08-26-the-driver-s-signature-is-the-schema` records for the Snowflake driver that never had
a tenant.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from chemclaw.core.config import PG_LOOPBACK_HOSTS, settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import register_secret_env
from chemclaw.deliver.message import Message


class DeliveryDriver(Protocol):
    """Get one message to its recipient, or raise."""

    async def deliver(self, message: Message) -> None:
        """Send `message`. Raises on any failure; the caller decides whether that is fatal."""
        ...


class FileDeliveryDriver:
    """Write each message as a file into a directory — a mounted share, in practice.

    **No client, no credential, no egress**, which is the same argument
    `chemclaw.ingest.documents` makes in the other direction: a share that is *mounted* rather than
    called needs none of the three, and a site that already mounts one for reading can be delivered
    to over the same mount. It is also what makes this seam testable end to end without a network.

    One file per message, named by a hash of the message rather than by a timestamp, so a retried
    activity overwrites its own file instead of leaving two copies of one digest.
    """

    def __init__(self, name: str, directory: str, suffix: str = ".md") -> None:
        """Bind the channel's name and the directory it writes into."""
        self.name = name
        self.directory = Path(directory)
        self.suffix = suffix

    async def deliver(self, message: Message) -> None:
        """Write the message, creating the directory if the mount allows it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = stable_hash(
            {"to": message.recipient, "subject": message.subject, "body": message.body}
        )
        path = self.directory / f"{message.kind}-{identity}{self.suffix}"
        stamp = datetime.now(UTC).isoformat()
        path.write_text(
            f"# {message.subject}\n\nTo: {message.recipient}\nWhen: {stamp}\n\n{message.body}\n",
            encoding="utf-8",
        )


def plaintext_channel_refusal(name: str, url: str, token_env: str = "") -> str:
    """Why `url` may not be delivered to under the enforced posture, or `""` when it may.

    The same floor `publish/drivers/http._refuse_plaintext_sink` applies to a result sink, on the
    same terms and for a stronger reason: a delivery carries *more* human-readable content than a
    sink record does — a chemist's standing query, note ids, an escalation body — and when
    `token_env` is set every POST carries a bearer credential too. The sibling seam decided this and
    this one shipped without it.

    Only under `entra_required`, with loopback dev exempt, exactly as the sink and as
    `require_pg_tls` and the Temporal-mTLS guard.

    **The reason is returned rather than raised, because the raise happens in the wrong place to be
    a refusal.** A driver is constructed inside `registry.deliver`'s per-channel `try`, which
    swallows so that one broken channel does not cost every other recipient their message — and
    that swallow is correct. Measured: with `entra_required=true` and an enabled `http://` channel,
    `enabled()` returns it, `deliver()` returns `[]`, and the only trace is one WARNING per message,
    which reads exactly like the destination being down. So the control that named itself a refusal
    was a per-message drop on a deployment that looked healthy. Returning the reason lets
    `cli/validate_channels.py` ask the same question of a *manifest* — before anything is
    delivered, which is where "refuse" can mean refuse — from this one definition.
    """
    if not settings.entra_required:
        return ""
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.hostname or "").lower() in PG_LOOPBACK_HOSTS:
        return ""
    carried = (
        "every POST carries a bearer credential and the message body"
        if token_env
        else "the message body"
    )
    return (
        f"entra_required=true with a non-loopback http delivery channel {name!r} at {url!r}: "
        f"{carried} would cross the wire in cleartext. Use an https:// url, or bind a loopback "
        "address for local dev."
    )


def _refuse_plaintext_channel(name: str, url: str, token_env: str) -> None:
    """Raise `plaintext_channel_refusal`'s reason, when there is one.

    Kept at the construction site as well as in the validator: the validator is a gate an operator
    runs, and a driver built by a path that skipped it must still not open a cleartext destination.
    """
    reason = plaintext_channel_refusal(name, url, token_env)
    if reason:
        raise ValueError(reason)


class WebhookDeliveryDriver:
    """POST each message as JSON to one URL — the shape a chat or ticketing integration takes.

    The credential is read from the environment by name, never from the manifest, and the name is
    registered with the log redaction inventory at construction so its value cannot reach a log
    line. That registration is at the *read* site deliberately, which is what
    `core.logging.register_secret_env` asks for: a registration one module away from the use drifts
    from it.

    **This driver makes an outbound call, which is why the seam is opt-in and named.** Nothing here
    is enabled unless `CHEMCLAW_DELIVERY_CHANNELS` says so, and a deployment enabling it owes the
    chart's `networkPolicy.egressDestinations` an entry — a channel whose host the policy drops
    fails every delivery with a timeout that reads as the destination being down.
    """

    def __init__(self, name: str, url: str, token_env: str = "", timeout_seconds: float = 10.0):
        """Bind the destination and the environment variable its bearer token lives under."""
        _refuse_plaintext_channel(name, url, token_env)
        self.name = name
        self.url = url
        self.token_env = token_env
        self.timeout_seconds = timeout_seconds
        register_secret_env(token_env)

    async def deliver(self, message: Message) -> None:
        """POST the message, raising for any non-2xx response.

        **The recipient's view, not the model's.** `model_dump()` serialised the whole object,
        which sent `correlation_id` to a third-party chat or ticketing host — the field whose own
        docstring says "never rendered to the recipient", and the key that joins this fleet's
        deliveries to the audit trail. The file driver honoured that and this one did not, which is
        the asymmetry a shared projection removes.
        """
        headers = {"Content-Type": "application/json"}
        token = os.environ.get(self.token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = message.model_dump(include={"recipient", "subject", "body", "kind"})
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.url, content=json.dumps(payload), headers=headers)
            response.raise_for_status()


def file_channel(name: str, directory: str, suffix: str = ".md") -> DeliveryDriver:
    """Build a `FileDeliveryDriver` — the `module:callable` a manifest names."""
    return FileDeliveryDriver(name=name, directory=directory, suffix=suffix)


def webhook_channel(
    name: str, url: str, token_env: str = "", timeout_seconds: float = 10.0
) -> DeliveryDriver:
    """Build a `WebhookDeliveryDriver` — the `module:callable` a manifest names."""
    return WebhookDeliveryDriver(
        name=name, url=url, token_env=token_env, timeout_seconds=timeout_seconds
    )
