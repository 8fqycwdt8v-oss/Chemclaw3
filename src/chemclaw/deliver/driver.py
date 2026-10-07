"""What a delivery channel has to be, and the two this release ships.

One method. A driver takes a `Message` and gets it to a person; it returns nothing and raises on
failure, so Temporal's activity retry is the durability rather than a second retry loop here.
Neither shipped driver holds a vendor client: a site that wants Exchange, Teams or Slack names a
`module:callable` in a manifest, so no vendor's shape becomes the seam's shape.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from chemclaw.core.config import PG_LOOPBACK_HOSTS, settings
from chemclaw.core.http import default_ssl_context
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import register_secret_env
from chemclaw.deliver.message import Attachment, Message


@runtime_checkable
class DeliveryDriver(Protocol):
    """Get one message to its recipient, or raise.

    Runtime-checkable so `registry.build` can `isinstance`-check a factory a manifest named.

    A driver is called **at least once** per message: a retried delivery activity re-walks every
    channel, including those that already took the message, and there is no per-channel delivery
    record. So every driver must make a redelivery identifiable at the destination — carry
    `message_id(message)` where the destination keys on it, or be idempotent by construction (the
    file driver is both: the id is its filename).
    """

    async def deliver(self, message: Message) -> None:
        """Send `message`. Raises on any failure; the caller decides whether that is fatal.

        Called at least once per message — see the class docstring for what that requires.
        """
        ...


def _refuse_impossible_directory(name: str, directory: Path) -> None:
    """Raise when `directory` can never *be* a directory — a permanent configuration fault.

    A missing path is created. What is refused is what no remount fixes: a non-directory already
    sits there, or a path component is a regular file. Every other `OSError` (permission,
    read-only or unmounted share) is left to the outage path in `deliver()`, since it cannot be
    told from a temporary outage. Raised as `ValueError` so `registry.deliver` routes it to the
    configuration counter rather than the outage counter.
    """
    if directory.exists() and not directory.is_dir():
        raise ValueError(
            f"delivery channel {name!r} writes into {str(directory)!r}, which exists and is not a "
            "directory. No message can ever be written there; fix the channel's `directory:`."
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except (NotADirectoryError, FileExistsError) as exc:
        raise ValueError(
            f"delivery channel {name!r} writes into {str(directory)!r}, which cannot be a "
            f"directory: {exc}. A component of that path is a regular file, so no message can ever "
            "be written there; fix the channel's `directory:`."
        ) from exc
    except OSError:
        # Ambiguous: may be transient, so `deliver()` reports it as an outage.
        return


def message_id(message: Message) -> str:
    """The one handle a receiver can dedupe a message on, derived from its content.

    Delivery is at-least-once, so the receiver needs a key. The hash covers `kind`, the content and
    `correlation_id`: a retry of one delivery carries the same correlation id (the workflow builds
    the message once), while two runs with byte-identical content stay distinct. `correlation_id`
    is in the key but never rendered into the payload.
    """
    return stable_hash(
        {
            "to": message.recipient,
            "subject": message.subject,
            "body": message.body,
            "kind": message.kind,
            "correlation": message.correlation_id,
            # The attachment's identity, not its bytes: a regenerated draft is still the same
            # message, while a message with and without a file are not.
            "attachments": [(one.filename, one.media_type) for one in message.attachments],
        }
    )


def _write_atomically(path: Path, content: str | bytes) -> None:
    """Put `content` at `path` in one step, so a concurrent reader never sees half of it.

    Re-delivery overwrites the same path by design, so a truncate-then-write would expose short
    files to readers of the share. `flush` + `fsync` before the rename so a crash on an SMB/CIFS
    mount cannot leave a zero-length file; the temp file sits in the same directory because
    `os.replace` is atomic only within one filesystem.
    """
    binary = isinstance(content, bytes)
    with tempfile.NamedTemporaryFile(
        "wb" if binary else "w",
        encoding=None if binary else "utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class FileDeliveryDriver:
    """Write each message as a file into a directory — a mounted share, in practice.

    No client, no credential, no egress. One file per message, named by a hash of the message, so
    a retried activity overwrites its own file. The destination is judged at construction (see
    `_refuse_impossible_directory`).
    """

    def __init__(self, name: str, directory: str, suffix: str = ".md") -> None:
        """Bind the channel's name and the directory it writes into, refusing an impossible path."""
        self.name = name
        self.directory = Path(directory)
        self.suffix = suffix
        _refuse_impossible_directory(name, self.directory)

    async def deliver(self, message: Message) -> None:
        """Write the message, creating the directory if the mount allows it.

        The `mkdir` repeats here because a share can be unmounted or a directory removed after the
        driver was built.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = message_id(message)
        stamp = datetime.now(UTC).isoformat()
        attached = [
            _attachment_path(self.directory, message.kind, identity, one)
            for one in message.attachments
        ]
        # Attachments land before the message that names them, so a chemist opening the `.md` the
        # moment it appears never sees a missing file.
        for path, attachment in zip(attached, message.attachments, strict=True):
            _write_atomically(path, attachment.content)
        listing = "".join(f"File: {path.name}\n" for path in attached)
        _write_atomically(
            self.directory / f"{message.kind}-{identity}{self.suffix}",
            f"# {message.subject}\n\nTo: {message.recipient}\nWhen: {stamp}\n{listing}\n"
            f"{message.body}\n",
        )


def _attachment_path(directory: Path, kind: str, identity: str, attachment: Attachment) -> Path:
    """Where one attachment sits beside its message on the share.

    Prefixed with the message's id so two deliveries carrying the same filename do not overwrite
    each other. `Attachment`'s filename pattern guarantees no separator reaches here.
    """
    return directory / f"{kind}-{identity}-{attachment.filename}"


def plaintext_channel_refusal(name: str, url: str, token_env: str = "", *, enforced: bool) -> str:
    """Why `url` may not be delivered to under the enforced posture, or `""` when it may.

    The same plaintext floor a result sink has: a delivery carries human-readable content and,
    with `token_env` set, a bearer credential. Loopback dev is exempt.

    `enforced` is a parameter so the validator can pass `True` unconditionally (would this manifest
    be legal under the target posture?) while the construction site passes
    `settings.entra_required`. The reason is returned rather than raised so
    `cli/validate_channels.py` can refuse a manifest before anything is delivered; a raise inside
    `registry.deliver` would only be a swallowed per-message drop.
    """
    if not enforced:
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

    Kept at construction as well as in the validator, so a driver built by a path that skipped the
    validator still never opens a cleartext destination. Reads this process's own posture.
    """
    reason = plaintext_channel_refusal(name, url, token_env, enforced=settings.entra_required)
    if reason:
        raise ValueError(reason)


class WebhookDeliveryDriver:
    """POST each message as JSON to one URL — the shape a chat or ticketing integration takes.

    The credential is read from the environment by name, never from the manifest, and registered
    with the log redaction inventory at the read site. This driver makes an outbound call: it is
    opt-in via `CHEMCLAW_DELIVERY_CHANNELS`, and its host needs an entry in the chart's
    `networkPolicy.egressDestinations`.
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

        Sends the recipient's view only, so `correlation_id` never reaches a third-party host.
        """
        headers = {"Content-Type": "application/json"}
        token = os.environ.get(self.token_env, "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Attachments included, base64 as `AttachmentBytes` serialises them — this seam's whole
        # point is that a receiver gets the artefact and not a pointer to it.
        payload = message.model_dump(
            include={"recipient", "subject", "body", "kind", "attachments"}
        )
        # Sent as a field and as `Idempotency-Key`: a chat or ticketing host reads the header, a
        # site's own receiver reads the body.
        identity = message_id(message)
        payload["message_id"] = identity
        headers["Idempotency-Key"] = identity
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            # One process-wide trust store: the driver is rebuilt per delivery (so it cannot outlive
            # a credential rotation), and parsing the CA bundle per client is blocking CPU on the
            # event loop.
            verify=default_ssl_context(),
            # Never inherit an ambient proxy: the request carries message content and a bearer
            # token, and the destination is the one the manifest states.
            trust_env=False,
        ) as client:
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
