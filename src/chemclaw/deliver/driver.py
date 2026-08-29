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

import httpx

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
        self.name = name
        self.url = url
        self.token_env = token_env
        self.timeout_seconds = timeout_seconds
        register_secret_env(token_env)

    async def deliver(self, message: Message) -> None:
        """POST the message, raising for any non-2xx response."""
        headers = {"Content-Type": "application/json"}
        token = os.environ.get(self.token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.url, content=json.dumps(message.model_dump()), headers=headers
            )
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
