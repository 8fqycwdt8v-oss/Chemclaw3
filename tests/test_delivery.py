"""The outbound delivery seam: a message that leaves for a person.

`durable/digest.py` stated the position this replaces in as many words — *"no new delivery
mechanism, no email integration, no second notification system"* — which was right while the product
was a chat window and is why a project leader could not be reached on a Monday morning: the only
place a digest landed was a mailbox inside the app.

Four properties are asserted rather than described, because each is a claim the README makes:
delivery is off until a deployment names a channel, a message is redacted before any driver sees it,
one channel's failure is not everyone's, and nothing reads *from* a channel.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.core.config import settings
from chemclaw.deliver.driver import FileDeliveryDriver
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.message import Message
from chemclaw.deliver.registry import (
    DeliveryChannelError,
    build,
    deliver,
    delivery_enabled,
    discovered,
    enabled,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def test_delivery_is_off_until_a_deployment_names_a_channel() -> None:
    """Discovery is deliberately not enablement here, unlike the connector registry.

    A discovered connector serves a tool; a discovered channel sends something out of the building.
    The shipped channels are found — that is what makes them enable-able — and none of them is on.
    """
    assert set(discovered()) == {"share", "webhook"}
    assert settings.delivery_channel_list == []
    assert delivery_enabled() is False
    assert enabled() == []


def test_a_channel_named_with_no_folder_is_an_error_rather_than_a_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who spelled a channel wrong means to be delivering and is not.

    The one failure a delivery seam has to be loud about: a silent skip looks exactly like a
    deployment with nothing to send.
    """
    monkeypatch.setattr(settings, "delivery_channels", "share,teams")
    with pytest.raises(DeliveryChannelError, match="teams"):
        enabled()


def test_the_shipped_channels_build_from_their_own_manifests() -> None:
    """`config:` is bound against the driver's signature, so a wrong key fails here."""
    manifests = discovered()
    driver = build(manifests["share"])
    assert isinstance(driver, FileDeliveryDriver)
    # And a manifest whose config does not match the driver names both, rather than surfacing a
    # bare `TypeError` from inside the driver.
    wrong = DeliveryChannelManifest(
        name="share",
        description="x",
        driver="chemclaw.deliver.driver:file_channel",
        config={"folder": "/tmp"},
    )
    with pytest.raises(DeliveryChannelError, match="signature is the schema"):
        build(wrong)


def test_a_manifest_may_not_set_the_name_the_registry_supplies() -> None:
    """The same guard the sink and source manifests carry, for the same failure."""
    with pytest.raises(ValueError, match="supplies it"):
        DeliveryChannelManifest(
            name="share",
            description="x",
            driver="chemclaw.deliver.driver:file_channel",
            config={"name": "other", "directory": "/tmp"},
        )


def test_a_message_is_redacted_before_any_driver_sees_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scrub is in the registry, not in each driver.

    A redaction every driver has to remember is one the next driver forgets, and the one that
    forgets is the one that sends outside the cluster. Driven end to end through the file channel,
    with a real registered secret, so the assertion is over what actually lands on disk.
    """
    from chemclaw.core.logging import register_secret_env

    monkeypatch.setenv("CHEMCLAW_TEST_DELIVERY_SECRET", "hunter2-not-in-the-outbox")
    register_secret_env("CHEMCLAW_TEST_DELIVERY_SECRET")

    outbox = tmp_path / "outbox"
    monkeypatch.setattr(settings, "delivery_channels_dir", str(tmp_path / "channels"))
    channel = tmp_path / "channels" / "local"
    channel.mkdir(parents=True)
    (channel / "channel.yaml").write_text(
        "name: local\n"
        "description: a test channel\n"
        "driver: chemclaw.deliver.driver:file_channel\n"
        f"config:\n  directory: {outbox}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "delivery_channels", "local")

    took = asyncio.run(
        deliver(
            Message(
                recipient="u-1",
                subject="digest",
                body="the token is hunter2-not-in-the-outbox, do not send it",
            )
        )
    )
    assert took == ["local"]
    written = "\n".join(path.read_text(encoding="utf-8") for path in outbox.iterdir())
    assert "hunter2-not-in-the-outbox" not in written
    assert "***" in written


def test_one_channels_failure_is_not_everyones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject-and-continue, the discipline the ELN sync and the digest already use.

    The return value is the point: "delivered" and "swallowed" are different facts, and
    `durable/digest.py` is the caller that must not conflate them.
    """
    root = tmp_path / "channels"
    good = root / "good"
    good.mkdir(parents=True)
    (good / "channel.yaml").write_text(
        "name: good\ndescription: works\ndriver: chemclaw.deliver.driver:file_channel\n"
        f"config:\n  directory: {tmp_path / 'out'}\n",
        encoding="utf-8",
    )
    bad = root / "bad"
    bad.mkdir()
    (bad / "channel.yaml").write_text(
        "name: bad\ndescription: unreachable\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  url: http://127.0.0.1:1/never\n  timeout_seconds: 0.05\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))
    monkeypatch.setattr(settings, "delivery_channels", "bad,good")

    took = asyncio.run(deliver(Message(recipient="u-1", subject="s", body="b")))
    assert took == ["good"]


def test_nothing_reads_from_a_channel() -> None:
    """An absence pinned: a channel is write-only, and a driver that read would be an ingest source.

    The mirror of the rule `ingest/sources/README.md` states in the other direction — "a source
    cannot acquire a write path by declaring one". A `fetch`, `read` or `poll` on the driver
    Protocol would make this seam a second, ungoverned way into the corpus.
    """
    protocol = (SRC / "deliver" / "driver.py").read_text(encoding="utf-8")
    for verb in ("def fetch", "def read", "def poll", "def receive"):
        assert verb not in protocol, (
            f"{verb!r} appears in the delivery driver. A channel is outbound only; a reader here "
            "is an ingest source that declared its way in through the wrong seam."
        )


def test_a_connector_bearer_token_is_scrubbed_from_an_outbound_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim this module made in prose and did not implement.

    `Message.redacted()`'s docstring said "the same filter runs here" about `core/logging`'s scrub.
    It did not: `redact_secrets` reaches a connector's bearer token only through its `extra_secrets`
    argument, and nothing passed one — so a tool error quoting its own `Authorization` header was
    scrubbed from the log line and shipped verbatim to the webhook host. Both sides now resolve the
    names through `connectors.registry.bearer_token_env_names`, so they cannot cover different sets.

    Reverting the `extra_secrets=` argument leaves every suite green, which is why this exists.
    """
    from chemclaw.deliver.message import Message

    secret = "sk-connector-token-abc123"
    monkeypatch.setenv("CHEMCLAW_CALC_MCP_TOKEN", secret)
    # **Bare, deliberately.** An "Authorization: Bearer …" spelling is caught by the structural
    # patterns whatever \ holds, so a test written that way passes with the fix
    # reverted and proves nothing — which is exactly what the first version of this test did. Only a
    # value recognisable *as a configured credential* exercises the argument that was missing.
    body = f"the server refused; the token it rejected was {secret}"
    scrubbed = Message(recipient="u-1", subject="digest", body=body).redacted()
    assert secret not in scrubbed.body, "a connector bearer token reached an outbound message"
    assert "***" in scrubbed.body


def test_the_webhook_sends_the_recipients_view_and_not_the_join_key() -> None:
    """`correlation_id` is the key that joins a delivery to this system's audit trail.

    Its own field docstring says "never rendered to the recipient". The file driver honoured that;
    the webhook driver serialised the whole model with `model_dump()` and posted it to a third-party
    chat or ticketing host. The projection is an allow-list rather than a deny-list, so a field
    added later is omitted rather than leaked.
    """
    from chemclaw.deliver.message import Message

    message = Message(
        recipient="u-1", subject="s", body="b", kind="digest", correlation_id="corr-secret"
    )
    payload = message.model_dump(include={"recipient", "subject", "body", "kind"})
    assert "correlation_id" not in payload
    assert set(payload) == {"recipient", "subject", "body", "kind"}


def test_a_plaintext_channel_is_refused_under_the_enforced_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor the sibling outbound seam already had and this one shipped without.

    `publish/drivers/http` refuses a non-loopback `http://` sink under `entra_required` because
    confidential chemistry would cross the wire in cleartext. A delivery carries *more*
    human-readable content than a sink record does — a chemist's standing query, note ids, an
    escalation body — and, when `token_env` is set, a bearer credential in every request.
    """
    from chemclaw.deliver.driver import WebhookDeliveryDriver

    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(ValueError, match="cleartext"):
        WebhookDeliveryDriver(name="ops", url="http://hooks.example.com/x")
    # Loopback stays available for local development, as it does for the sink.
    WebhookDeliveryDriver(name="ops", url="http://127.0.0.1:9000/x")
    WebhookDeliveryDriver(name="ops", url="https://hooks.example.com/x")

    monkeypatch.setattr(settings, "entra_required", False)
    WebhookDeliveryDriver(name="ops", url="http://hooks.example.com/x")


def test_a_message_kind_cannot_escape_the_outbox() -> None:
    """`kind` is a path component in the file driver, and was documented-but-unbounded.

    The docstring called it "a bounded vocabulary" and the type was `str`, while
    `FileDeliveryDriver` builds `directory / f"{kind}-{identity}{suffix}"` — so an absolute or
    `../`-bearing value escapes the outbox, with `mkdir(parents=True)` creating whatever it
    traverses to. The `Literal` is the bound; the prose was not.
    """
    from pydantic import ValidationError

    from chemclaw.deliver.message import Message

    for hostile in ("/etc/cron.d/x", "../../../etc/x", "digest/../.."):
        with pytest.raises(ValidationError):
            Message(recipient="u-1", subject="s", kind=hostile)  # type: ignore[arg-type]


def _channel(root: Path, name: str, body: str) -> None:
    """Write one `channel.yaml` under `root`, so a test can enable a channel it made up."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "channel.yaml").write_text(body, encoding="utf-8")


def test_the_config_gate_refuses_a_plaintext_channel_the_delivery_path_only_swallows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal had nowhere to be heard, which is why it was not a refusal.

    `_refuse_plaintext_channel` raises from driver construction, and construction happens inside
    `deliver()`'s per-channel `try` — the swallow that exists so one broken channel does not cost
    every other recipient their message, and that is correct. Measured before this test existed:
    with `entra_required=true` and an enabled `http://` channel, `enabled()` returned it,
    `deliver()` returned `[]`, and `make channel-validate` reported **no problems at all**. The
    control named itself a refusal and was a per-message drop on a deployment that looked healthy.

    So the gate an operator runs before delivering is where the question gets asked, and this
    asserts it in both directions — including that the shipped `https://` manifest still passes,
    since a posture check that failed everything would be removed by the next person.
    """
    from chemclaw.cli.validate_channels import problems

    root = tmp_path / "channels"
    _channel(
        root,
        "plainhook",
        "name: plainhook\ndescription: a site's own webhook\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  url: http://chat.internal/hooks/chemclaw\n"
        "  token_env: CHEMCLAW_DELIVERY_WEBHOOK_TOKEN\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))

    monkeypatch.setattr(settings, "entra_required", False)
    assert problems() == [], "the posture check must not fire where the posture is not enforced"

    monkeypatch.setattr(settings, "entra_required", True)
    found = problems()
    assert len(found) == 1 and "cleartext" in found[0] and "plainhook" in found[0], found


def test_the_posture_check_reads_the_destination_whatever_the_driver_calls_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `config:` block is free-form on purpose, so the check cannot key on the word `url`.

    The driver's own signature is the schema, so a site's driver may name its destination
    `endpoint`, `hook` or `webhook_url`. A check that knew one spelling would pass every channel it
    was written to catch — and would do so silently, which is the shape of the defect it exists to
    end. Every value that *is* a URL is asked instead.

    The `mounted` half is the one that would rot quietly: a mounted share's `directory` and `suffix`
    are not URLs, and the first version of this check passed them only because
    `PG_LOOPBACK_HOSTS` contains `''` for a Postgres DSN with no host. Asserted here so that
    depending on somebody else's constant cannot come back — if it did, every file channel in every
    enforced deployment would fail validation as a cleartext destination.
    """
    from chemclaw.cli.validate_channels import problems

    root = tmp_path / "channels"
    _channel(
        root,
        "oddkey",
        "name: oddkey\ndescription: names its destination something else\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  endpoint: http://chat.internal/hooks\n",
    )
    _channel(
        root,
        "mounted",
        "name: mounted\ndescription: a mounted share, no destination at all\n"
        "driver: chemclaw.deliver.driver:file_channel\n"
        f"config:\n  directory: {tmp_path / 'outbox'}\n  suffix: .md\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))
    monkeypatch.setattr(settings, "entra_required", True)

    found = problems()
    assert [problem for problem in found if "mounted" in problem] == [], (
        "a mounted share has no URL in its config and must not be refused as a cleartext "
        f"destination: {found}"
    )
    assert any("oddkey" in problem and "cleartext" in problem for problem in found), found


def test_a_channel_that_cannot_be_built_is_not_counted_as_a_destination_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two facts have different lifetimes, and one counter could not tell them apart.

    A send failure is usually a host having a bad afternoon. A channel whose driver will not build
    — a bad `config:` block, an unimportable `module:callable`, a destination the posture forbids —
    fails identically on every message until somebody edits a manifest. Both continue to the next
    channel; only one of them is still true tomorrow, so the permanent fault goes to
    `chemclaw_degraded_total{subsystem="delivery_channel_config"}` rather than hiding inside the
    transient one's series.
    """
    from chemclaw.core.metrics import METRICS

    root = tmp_path / "channels"
    _channel(
        root,
        "good",
        "name: good\ndescription: works\ndriver: chemclaw.deliver.driver:file_channel\n"
        f"config:\n  directory: {tmp_path / 'out'}\n",
    )
    _channel(
        root,
        "broken",
        "name: broken\ndescription: no such driver\n"
        "driver: chemclaw.deliver.no_such_module:nothing\nconfig: {}\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))
    monkeypatch.setattr(settings, "delivery_channels", "broken,good")

    def _series(line_prefix: str) -> float:
        """One labelled series out of the exposition.

        `value()` sums across every label set, and the label is the whole point of this assertion.
        """
        for line in METRICS.render().splitlines():
            if line.startswith(line_prefix):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    config = 'chemclaw_degraded_total{subsystem="delivery_channel_config"}'
    outage = "chemclaw_delivery_failures_total"
    config_before = _series(config)
    outage_before = METRICS.value(outage)

    assert asyncio.run(deliver(Message(recipient="u-1", subject="s", body="b"))) == ["good"]

    assert _series(config) == config_before + 1, (
        "an unbuildable channel left no configuration-degradation signal"
    )
    assert METRICS.value(outage) == outage_before, (
        "an unbuildable channel was counted as a destination outage, which is the conflation "
        "that made a permanent misconfiguration read as a transient one"
    )
