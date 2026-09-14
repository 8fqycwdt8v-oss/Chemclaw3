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
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from chemclaw.core.config import PG_LOOPBACK_HOSTS, settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import register_secret_env
from chemclaw.deliver.message import Attachment, Message


@runtime_checkable
class DeliveryDriver(Protocol):
    """Get one message to its recipient, or raise.

    Runtime-checkable for the reason `publish.driver.ResultSink` is: `registry.build` calls a
    factory a *manifest* named, and the only thing standing between a mistyped `driver:` and a
    message silently going nowhere is an `isinstance` at the moment it is built. A structural check
    is all that is available — the protocol is one method — and one method is what the seam needs.
    """

    async def deliver(self, message: Message) -> None:
        """Send `message`. Raises on any failure; the caller decides whether that is fatal."""
        ...


def _refuse_impossible_directory(name: str, directory: Path) -> None:
    """Raise when `directory` can never *be* a directory — a permanent configuration fault.

    **This is where the config-versus-outage split had a hole.** `registry.deliver` separates a
    channel that cannot be *built* (`degraded(subsystem="delivery_channel_config")`, an alert) from
    a destination that would not answer (`chemclaw_delivery_failures_total`, a graph an operator
    skims) — and the file driver's constructor touched no filesystem, so `build()` succeeded for
    *any* string. A mistyped `directory:` or a path whose parent is a regular file surfaced later,
    at `mkdir` time inside `deliver()`, on the outage counter: a fault that will be identical on
    every message for the life of the process, reported as the share having a bad afternoon.

    **Only the provable half is claimed.** A path that does not exist yet is not a
    misconfiguration — creating it is what a first delivery to a fresh mount does — so this
    *creates* it and says nothing. What it refuses is the two cases no remount fixes: something
    that is not a directory already sits there, or a component of the path is a regular file
    (`NotADirectoryError`). Every other `OSError` — a permission, a read-only mount, a share that
    is not mounted yet — is deliberately left to the outage path, because a validator cannot tell
    those from a destination that is temporarily down, and guessing wrong here moves a real outage
    onto the alert that means "a human must edit a manifest".

    Raised as `ValueError` for the reason the plaintext refusal is: `registry.deliver` builds inside
    a `try` and routes anything raised there to the config counter.
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
        # Ambiguous, and deliberately not claimed. A permission or an unmounted share may be gone
        # tomorrow; `deliver()` will try the same `mkdir` and report the failure as the outage it
        # may well be.
        return


def message_id(message: Message) -> str:
    """The one handle a receiver can dedupe a message on, derived from its content.

    **The file driver already computed this and did not share it**, which is what made the two
    shipped channels disagree about a property neither states. The file channel is idempotent
    because this hash is its filename — three deliveries of one message leave one file, measured.
    The webhook channel POSTs `recipient`/`subject`/`body`/`kind` and nothing else: `correlation_id`
    is excluded on purpose (its own docstring says it is "never rendered to the recipient", and it
    is the key joining this fleet's deliveries to the audit trail), which is right for the *content*
    and left the payload with no field a receiver could key on. Measured: three `deliver()` calls of
    one message put one file on the share and **three** POSTs on the wire.

    `deliver_message_activity` runs under `BAD_DATA_RETRY`, so a worker death after the POST
    landed re-runs the activity and re-POSTs — at-least-once by construction, which is correct
    for delivery and is exactly why the receiver needs a key. A duplicated digest is a nuisance;
    the same driver now really does carry the `job-result`, `report` and `awaiting` kinds, where
    a duplicate is a duplicated ticket — this paragraph said "declared seam" while those three
    had no producer at all.

    **`correlation_id` is in the *key* and stays out of the *payload*, and conflating those two
    questions cost a real message.** The paragraph above is right that the id must not be rendered
    to a recipient; it does not follow that an idempotency key may not be derived from it. Keyed on
    content alone, two genuinely distinct runs with identical content collapse: measured, two
    `job-result` messages from different runs of the same job for the same chemist produced the
    identical id `d1b783a371b64708`, so a compliant receiver drops the second **by design** and the
    share overwrites it. `subject` is `f"{connector}:{job} finished"` and `body` is a summary
    derived from the inputs, so a re-run of one job is byte-identical by construction — and
    `job-result` is the one kind with no natural discriminator in its body, where digest, report
    and awaiting carry note ids, a `note_ref` and a deadline.

    Folding it in preserves the property the key exists for, because a *retry* of one delivery
    carries the same correlation id by construction: the workflow builds the message once and
    Temporal re-runs the activity with that same input. Same delivery, same key; different run,
    different key. A producer that passes no correlation id is unchanged.

    **`kind` is in the hash, and the file driver's hash did not have it** — the file driver spelled
    the kind as the filename's *prefix* instead. Folding it in changes every share filename once,
    on the first re-delivery after this release: the old file stays and the new one lands beside
    it. That is the smaller cost. The alternative — a three-field id for the share and a four-field
    one for the webhook — would give two messages differing only in `kind` the same
    `Idempotency-Key`, which is a receiver dropping a real `job-result` because a digest with the
    same body had already arrived.
    """
    return stable_hash(
        {
            "to": message.recipient,
            "subject": message.subject,
            "body": message.body,
            "kind": message.kind,
            "correlation": message.correlation_id,
            # **The attachment's identity, not its bytes.** A key that hashed the content would
            # make a re-delivery of the same report a *different* message the moment the draft was
            # regenerated with one word changed, which is the opposite of what an idempotency key
            # is for; a key that ignored attachments entirely would give a message and the same
            # message carrying a run sheet one id, so a receiver drops the one that has the file.
            "attachments": [(one.filename, one.media_type) for one in message.attachments],
        }
    )


def _write_atomically(path: Path, content: str | bytes) -> None:
    """Put `content` at `path` in one step, so a concurrent reader never sees half of it.

    `Path.write_text` truncates and *then* writes, and readers of a delivery share hold no lock —
    measured on a ~520 kB digest re-delivered 60 times with a reader stat-ing the file, **12%** of
    1,883 observations saw a short file. Re-delivery overwrites the same path by design (the
    filename is a content hash, which is what makes this channel idempotent), so the window is not
    rare: it opens on every retry.

    `flush` + `fsync` before the rename, because on an SMB/CIFS mount — the deployment this driver
    is written for — a crash after the rename could otherwise leave a zero-length file that
    `deliver()` has already counted on `chemclaw_deliveries_total`. The temp file is in the same
    directory because `os.replace` is atomic only within one filesystem.

    **The same technique as `kg/git_writer._replace_atomically`, deliberately copied rather than
    imported.** That helper is private to a module in another layer (`kg/`), and
    `tests/test_layering.py` polices the direction of imports between them; the shared thing here is
    a four-line stdlib idiom, not an abstraction. If a third caller appears, the idiom belongs in
    `core/`.
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

    **No client, no credential, no egress**, which is the same argument
    `chemclaw.ingest.documents` makes in the other direction: a share that is *mounted* rather than
    called needs none of the three, and a site that already mounts one for reading can be delivered
    to over the same mount. It is also what makes this seam testable end to end without a network.

    One file per message, named by a hash of the message rather than by a timestamp, so a retried
    activity overwrites its own file instead of leaving two copies of one digest.

    **The destination is judged at construction**, not on the first write — see
    `_refuse_impossible_directory` for which half of "bad path" is claimed and why the rest is not.
    """

    def __init__(self, name: str, directory: str, suffix: str = ".md") -> None:
        """Bind the channel's name and the directory it writes into, refusing an impossible path."""
        self.name = name
        self.directory = Path(directory)
        self.suffix = suffix
        _refuse_impossible_directory(name, self.directory)

    async def deliver(self, message: Message) -> None:
        """Write the message, creating the directory if the mount allows it.

        The `mkdir` stays here as well as in the constructor, and is not the redundancy it looks
        like: the constructor's runs when the driver is *built*, and a share can be unmounted or a
        directory removed between that and any later message. What moved to construction is the
        *verdict* — a path that can never be a directory — not the creation.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = message_id(message)
        stamp = datetime.now(UTC).isoformat()
        attached = [
            _attachment_path(self.directory, message.kind, identity, one)
            for one in message.attachments
        ]
        # **The attachments land before the message names them.** A chemist watching the share
        # opens the `.md` the moment it appears; a message listing a file that is not there yet
        # reads as a delivery that lost it. Same ordering rule, and the same reason, as
        # `kg/record.py` writing a note's dependencies before the note that cites them.
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

    Prefixed with the message's own id so two deliveries carrying `run-sheet.csv` do not overwrite
    each other — the message file is content-addressed for exactly that reason, and an attachment
    named only by its filename would undo it for the half a chemist actually opens. `Attachment`'s
    pattern is what makes joining these two safe: no separator can reach here.
    """
    return directory / f"{kind}-{identity}-{attachment.filename}"


def plaintext_channel_refusal(name: str, url: str, token_env: str = "", *, enforced: bool) -> str:
    """Why `url` may not be delivered to under the enforced posture, or `""` when it may.

    The same floor `publish/drivers/http._refuse_plaintext_sink` applies to a result sink, on the
    same terms and for a stronger reason: a delivery carries *more* human-readable content than a
    sink record does — a chemist's standing query, note ids, an escalation body — and when
    `token_env` is set every POST carries a bearer credential too. The sibling seam decided this and
    this one shipped without it.

    Loopback dev is exempt, exactly as the sink and as `require_pg_tls` and the Temporal-mTLS guard.

    **`enforced` is a parameter rather than a read of `settings.entra_required`, and that is the
    whole difference between a gate and a claim.** The first version read the setting here, which
    made this function answer `""` for every channel whenever enforcement was off — and enforcement
    is off by default and off in CI, where the *validator* runs. So `make channel-validate`'s rule 4
    could only ever fire on a deployment that had already flipped the setting on, which is precisely
    the deployment it exists to catch *before*. That is the same CI-blindness rules 2 and 3 already
    work around by iterating the discovered set rather than the enabled one, and this rule shipped
    without the equivalent. The validator now passes `enforced=True` unconditionally — it asks
    "would this channel be legal under the posture this deployment is heading for?", which is a
    question about a manifest and not about today's environment — while the construction site passes
    `settings.entra_required`, because there the question really is about today's environment.

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

    Kept at the construction site as well as in the validator: the validator is a gate an operator
    runs, and a driver built by a path that skipped it must still not open a cleartext destination.

    Here — and only here — the posture is this process's own: refusing to *open* a destination is a
    statement about what this deployment is doing right now, so it reads `settings.entra_required`.
    The validator asks the other question and passes `enforced=True`; see the docstring above.
    """
    reason = plaintext_channel_refusal(name, url, token_env, enforced=settings.entra_required)
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
        # Attachments included, base64 as `AttachmentBytes` serialises them — this seam's whole
        # point is that a receiver gets the artefact and not a pointer to it.
        payload = message.model_dump(
            include={"recipient", "subject", "body", "kind", "attachments"}
        )
        # **At-least-once is the right contract, and this is the handle that makes it survivable.**
        # See `message_id` for the measurement. Sent as a field *and* as `Idempotency-Key`, because
        # a chat or ticketing host reads the header and a site's own receiver reads the body, and
        # neither could dedupe before: the file channel next door was idempotent for the same
        # message through the same registry call while this one was not.
        identity = message_id(message)
        payload["message_id"] = identity
        headers["Idempotency-Key"] = identity
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            # Never inherit an ambient proxy — the same flag, and the same reason, every other
            # *httpx* client in this tree that reaches a real dependency carries. Not every client:
            # `api/auth.py`'s `PyJWKClient` fetches the tenant key set through
            # `urllib.request.urlopen`, which has no such flag and follows `HTTP_PROXY` (measured);
            # it is a `BACKLOG.md` row rather than a silent exception to this sentence. The httpx
            # set is (`connectors/registry.py`,
            # `core/mcp_session.py`, `core/embeddings.py`, `connectors/health.py`,
            # `agent/llm_provider.py`, `publish/drivers/http.py`). That list was *aspirational*
            # about its last two until 2026-09-05: both LLM seams carried the flag only on a
            # private-CA branch no shipped configuration takes, so this comment described a fleet
            # posture two of its six members did not have
            # (`D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` made it true).
            # This one was the exception and
            # is the worst place for it: the payload is human-readable message content and the
            # request carries `Authorization: Bearer`. Measured with a recording listener installed
            # as `HTTP_PROXY`, the proxy received the whole POST — body and bearer — and the
            # configured destination received nothing. The destination is stated in the manifest
            # and reached through the chart's `networkPolicy.egressDestinations`; a pod-level
            # `HTTPS_PROXY` is not a second opinion about where a digest goes.
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
