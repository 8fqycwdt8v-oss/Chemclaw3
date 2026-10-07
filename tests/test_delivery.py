"""The outbound delivery seam: a message that leaves for a person.

Four properties are asserted: delivery is off until a deployment names a channel, a message is
redacted before any driver sees it, one channel's failure is not everyone's, and nothing reads
*from* a channel.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.deliver.driver import FileDeliveryDriver
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.message import Attachment, Message
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

    A redaction every driver must remember is one the next driver forgets. Driven end to end through
    the file channel with a real registered secret, asserting on what lands on disk.
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

    The mirror of `ingest/sources/README.md`'s rule; a `fetch`, `read` or `poll` on the driver
    Protocol would be an ungoverned way into the corpus.
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
    """A connector bearer token is scrubbed from an outbound message.

    `redact_secrets` reaches a connector's token only through `extra_secrets`, so both the log
    filter and `Message.redacted()` resolve the names through
    `connectors.registry.bearer_token_env_names` and cannot cover different sets.
    """
    from chemclaw.deliver.message import Message

    secret = "sk-connector-token-abc123"
    monkeypatch.setenv("CHEMCLAW_CALC_MCP_TOKEN", secret)
    # Bare, deliberately: an "Authorization: Bearer …" spelling is caught by the structural patterns
    # anyway, so only a value recognisable as a configured credential exercises `extra_secrets`.
    body = f"the server refused; the token it rejected was {secret}"
    scrubbed = Message(recipient="u-1", subject="digest", body=body).redacted()
    assert secret not in scrubbed.body, "a connector bearer token reached an outbound message"
    assert "***" in scrubbed.body


def test_the_webhook_sends_the_recipients_view_and_not_the_join_key() -> None:
    """The webhook sends the recipient's view, not `correlation_id`, the audit-trail join key.

    The projection is an allow-list, so a field added later is omitted rather than leaked. Asserted
    on the captured request body, which is where the guarantee is.
    """
    message = Message(
        recipient="u-1", subject="s", body="b", kind="digest", correlation_id="corr-secret"
    )
    payload = _post_and_capture(message)[1]
    assert "correlation_id" not in payload
    assert set(payload) == {"recipient", "subject", "body", "kind", "message_id", "attachments"}


def test_the_webhook_never_follows_an_ambient_proxy() -> None:
    """A pod's `HTTP_PROXY` must not silently reroute a delivery — with its body and its bearer.

    Every client here that reaches a real dependency sets `trust_env=False`; this one carries
    message content and `Authorization: Bearer`. Driven through a real socket: a listener installed
    as the proxy answers `200` and the configured host is unroutable, so any delivery that completes
    went through the proxy.
    """
    import os
    import socket
    import threading

    from chemclaw.deliver.driver import WebhookDeliveryDriver

    received: list[bytes] = []
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _accept() -> None:
        """Record whatever a proxy-following client sends, then answer so it does not hang."""
        try:
            conn, _ = listener.accept()
            conn.settimeout(2.0)
            received.append(conn.recv(4096))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            conn.close()
        except OSError:  # closed by the main thread when nothing connected
            pass

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    old_proxy = os.environ.get("HTTP_PROXY")
    os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
    os.environ["CHEMCLAW_TEST_DELIVERY_TOKEN"] = "s3cr3t-bearer-value"
    try:
        driver = WebhookDeliveryDriver(
            name="probe",
            # `.invalid` is reserved by RFC 2606 and never resolves, so a delivery that reaches
            # anything at all reached the proxy.
            url="http://hook.chemclaw.invalid/deliver",
            token_env="CHEMCLAW_TEST_DELIVERY_TOKEN",
            timeout_seconds=2.0,
        )
        message = Message(recipient="u-1", subject="s", body="CONFIDENTIAL BODY", kind="digest")
        try:
            asyncio.run(driver.deliver(message))
        except Exception:
            # Failing to reach an unroutable host is the pass path; the assertion below is
            # about where the bytes went, not about whether the delivery succeeded.
            pass
    finally:
        listener.close()
        thread.join(timeout=3.0)
        os.environ.pop("CHEMCLAW_TEST_DELIVERY_TOKEN", None)
        if old_proxy is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = old_proxy

    assert received == [], (
        "an ambient HTTP_PROXY received the delivery — body and bearer included:\n"
        + received[0].decode("utf-8", "replace")
    )


def test_a_plaintext_channel_is_refused_under_the_enforced_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plaintext channel is refused under the enforced posture.

    As `publish/drivers/http` refuses a non-loopback `http://` sink under `entra_required`; a
    delivery carries human-readable content and possibly a bearer in every request.
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
    """A message kind cannot escape the outbox.

    `FileDeliveryDriver` builds `directory / f"{kind}-{identity}{suffix}"`, so an absolute or `../`
    value would escape (with `mkdir(parents=True)`); the `Literal` type is the bound.
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
    """The config gate refuses a plaintext channel that the delivery path would only swallow.

    Driver construction raises inside `deliver()`'s per-channel `try`, which correctly swallows, so
    at delivery the refusal is a silent per-message drop. `make channel-validate` is where it must
    be heard, both directions (the shipped `https://` manifest passes), and *whatever this
    environment's posture*: a validator asks about the manifest, and a channel refused once
    enforcement is on is broken today. Construction still reads the setting; see the next test.
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

    for enforced in (False, True):
        monkeypatch.setattr(settings, "entra_required", enforced)
        found = problems()
        assert len(found) == 1 and "cleartext" in found[0] and "plainhook" in found[0], (
            f"with entra_required={enforced} the gate reported {found!r}. The rule must not depend "
            "on the setting it exists to catch a violation of being already switched on — that is "
            "a gate that passes in CI and fails in production"
        )


def test_a_driver_built_outside_the_gate_still_refuses_a_cleartext_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction asks about *this* deployment; the validator always asks under enforcement.

    `plaintext_channel_refusal` takes the posture as a parameter. Building a driver passes
    `settings.entra_required`, so local development can still use an `http://` channel. Both
    directions, so making the validator unconditional cannot make construction unconditional too.
    """
    from chemclaw.deliver.driver import webhook_channel

    monkeypatch.setattr(settings, "entra_required", False)
    assert webhook_channel(name="dev", url="http://chat.internal/hooks/chemclaw") is not None, (
        "construction must follow this deployment's own posture; refusing here would break every "
        "unenforced deployment's plaintext channel"
    )

    monkeypatch.setattr(settings, "entra_required", True)
    with pytest.raises(ValueError, match="cleartext"):
        webhook_channel(name="dev", url="http://chat.internal/hooks/chemclaw")


def test_the_posture_check_reads_the_destination_whatever_the_driver_calls_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The posture check reads the destination whatever the driver calls it.

    A `config:` block is free-form (the driver's signature is the schema), so every value that *is*
    a URL is checked, not a `url` key. A mounted share's `directory`/`suffix` must not count as a
    cleartext destination, which is asserted without relying on another module's constant.
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


def test_the_posture_check_walks_into_a_list_or_a_nested_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The posture check walks into a list or a nested destination.

    Free-form shape too: `urls: [a, b]` and `endpoints: {primary: …}` are realistic. Bounded rather
    than fully recursive (`_config_strings`).
    """
    from chemclaw.cli.validate_channels import problems

    root = tmp_path / "channels"
    _channel(
        root,
        "fanout",
        "name: fanout\ndescription: a site driver posting to several hooks\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  urls:\n    - https://chat.internal/a\n    - http://chat.internal/b\n",
    )
    _channel(
        root,
        "failover",
        "name: failover\ndescription: a site driver with a primary and a fallback\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  endpoints:\n    primary: https://chat.internal/a\n"
        "    fallback: http://chat.internal/b\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))

    found = problems()
    for name in ("fanout", "failover"):
        assert any(name in problem and "cleartext" in problem for problem in found), (
            f"the plaintext destination inside {name}'s config was never asked about: {found}. A "
            "check that only reads top-level strings passes every driver that groups its "
            "destinations, which is most of the ones a site would write"
        )


def test_the_posture_check_walks_into_a_fan_out_list_of_dicts_or_a_dict_of_lists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The posture check walks into a fan-out list of dicts or a dict of lists.

    `targets: [{url: …}]` and `endpoints: {primary: [url, …]}` are within the documented depth (a
    container, one level of nesting, the strings inside), and are refused like the shallower shapes.
    """
    from chemclaw.cli.validate_channels import problems

    root = tmp_path / "channels"
    _channel(
        root,
        "fanout-targets",
        "name: fanout-targets\ndescription: pairs each URL with its own per-target metadata\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  targets:\n    - url: https://chat.internal/a\n"
        "    - url: http://chat.internal/b\n",
    )
    _channel(
        root,
        "grouped-endpoints",
        "name: grouped-endpoints\ndescription: a dict of per-role URL lists\n"
        "driver: chemclaw.deliver.driver:webhook_channel\n"
        "config:\n  endpoints:\n    primary:\n      - https://chat.internal/a\n"
        "    fallback:\n      - http://chat.internal/b\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))
    monkeypatch.setattr(settings, "entra_required", True)

    found = problems()
    for name in ("fanout-targets", "grouped-endpoints"):
        assert any(name in problem and "cleartext" in problem for problem in found), (
            f"the plaintext destination nested two hops inside {name}'s config was never asked "
            f"about: {found}. A list-of-dicts or dict-of-lists one level past the already-handled "
            "list/dict shapes must not defeat the enforced posture's cleartext check"
        )


def test_config_strings_depth_is_bounded_not_unbounded() -> None:
    """`_config_strings` depth is bounded, not unbounded.

    A fourth hop (`targets: [{urls: [url]}]`) stays out of scope, and a pathologically deep
    structure must not raise or hang.
    """
    from chemclaw.cli.validate_channels import _config_strings

    one_hop_too_deep = {"targets": [{"urls": ["http://chat.internal/a"]}]}
    assert _config_strings(one_hop_too_deep) == [], (
        "a fourth container hop is past the documented and intended depth budget and must stay "
        "unseen, or the bound is not actually bounded"
    )

    # A structure nested far past the budget must return quickly rather than recursing without
    # limit — the whole point of a `depth` parameter is a deliberate, finite stop.
    deeply_nested: object = "http://chat.internal/pathological"
    for _ in range(500):
        deeply_nested = [deeply_nested]
    assert _config_strings(deeply_nested) == []


def test_a_file_channel_with_an_impossible_directory_is_a_config_fault_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file channel with an impossible directory is a config fault, not an outage.

    A regular file where the directory belongs fails every message until a manifest is edited, so it
    must surface at build time on the config counter rather than on
    `chemclaw_delivery_failures_total`. Both directions: a directory that does not exist yet is not
    a misconfiguration, since the first delivery creates it.
    """
    from chemclaw.core.metrics import METRICS

    blocked = tmp_path / "not-a-dir"
    blocked.write_text("a regular file sitting where the share should be", encoding="utf-8")

    root = tmp_path / "channels"
    _channel(
        root,
        "typo",
        "name: typo\ndescription: its directory is a regular file\n"
        f"driver: chemclaw.deliver.driver:file_channel\nconfig:\n  directory: {blocked}\n",
    )
    _channel(
        root,
        "fresh",
        "name: fresh\ndescription: a mount whose directory does not exist yet\n"
        "driver: chemclaw.deliver.driver:file_channel\n"
        f"config:\n  directory: {tmp_path / 'never' / 'made'}\n",
    )
    monkeypatch.setattr(settings, "delivery_channels_dir", str(root))
    monkeypatch.setattr(settings, "delivery_channels", "typo,fresh")

    def _series(line_prefix: str) -> float:
        for line in METRICS.render().splitlines():
            if line.startswith(line_prefix):
                return float(line.rsplit(" ", 1)[1])
        return 0.0

    config = 'chemclaw_degraded_total{subsystem="delivery_channel_config"}'
    outage = "chemclaw_delivery_failures_total"
    config_before, outage_before = _series(config), METRICS.value(outage)

    assert asyncio.run(deliver(Message(recipient="u-1", subject="s", body="b"))) == ["fresh"], (
        "a directory that does not exist yet must still be delivered to — creating it is what a "
        "first delivery to a fresh mount does"
    )
    assert _series(config) == config_before + 1, (
        "a directory that can never be a directory left no configuration-degradation signal; it "
        "was still being discovered at mkdir time and reported as the share being down"
    )
    assert METRICS.value(outage) == outage_before, (
        "the impossible path was counted as a destination outage, which is the conflation the "
        "config counter exists to end"
    )


def test_a_channel_that_cannot_be_built_is_not_counted_as_a_destination_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A channel that cannot be built is not counted as a destination outage.

    A send failure is usually transient; a build failure (bad `config:`, unimportable callable, a
    forbidden destination) is permanent until a manifest changes. Both continue to the next channel,
    and the permanent fault goes to `chemclaw_degraded_total{subsystem="delivery_channel_config"}`.
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


def test_a_secret_in_the_recipient_is_scrubbed_like_one_in_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret in the recipient is scrubbed like one in the body.

    `recipient` is free text that both shipped drivers put where the body goes; the guarantee is a
    property of the seam, not of today's caller.
    """
    from chemclaw.deliver.message import Message

    secret = "sk-connector-token-abc123"
    monkeypatch.setenv("CHEMCLAW_CALC_MCP_TOKEN", secret)

    scrubbed = Message(recipient=f"chemist@example.com {secret}", subject="s", body="b").redacted()

    assert secret not in scrubbed.recipient
    assert "***" in scrubbed.recipient


def test_a_recipient_the_scrub_rewrote_is_reported_rather_than_silently_undeliverable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recipient the scrub rewrote is reported rather than silently undeliverable.

    `redact_secrets` rewrites structural shapes, and a routable address can match one (a Teams URN's
    `TOKEN=`, URL userinfo, an `xoxb-` shape). The scrub stays, but a rewrite re-addresses the
    message, so it is logged — without the original, which may be a real credential.
    """
    urn = "urn:teams:channel:19:meeting_TOKEN=abc12345678@thread.v2"
    with caplog.at_level(logging.WARNING, logger="chemclaw.deliver.message"):
        scrubbed = Message(recipient=urn, subject="s", body="b").redacted()

    assert scrubbed.recipient != urn, "the scrub is the guarantee; it is not what is being relaxed"
    assert len(caplog.records) == 1, f"a re-addressed message was silent: {caplog.text!r}"
    assert scrubbed.recipient in caplog.text and urn not in caplog.text, (
        "the line carries the address the driver will actually get, never the original"
    )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="chemclaw.deliver.message"):
        intact = Message(recipient="chemist@example.com", subject="s", body="b").redacted()
    assert intact.recipient == "chemist@example.com" and not caplog.records, (
        "an ordinary address is untouched and unremarked"
    )


def _post_and_capture(message: Message) -> tuple[dict[str, str], dict[str, object]]:
    """Drive the real webhook driver once and return `(headers, json body)` off the wire.

    A `MockTransport` under the driver's own `httpx.AsyncClient`, so everything the driver decides
    — the projection, the headers, the redirect policy, `trust_env` — is what is being observed.
    """
    import httpx

    from chemclaw.deliver.driver import WebhookDeliveryDriver

    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    original = httpx.AsyncClient
    try:
        httpx.AsyncClient = lambda *a, **k: real_client(  # type: ignore[assignment,misc]
            *a, **{**k, "transport": httpx.MockTransport(_handle)}
        )
        driver = WebhookDeliveryDriver(name="probe", url="http://127.0.0.1:1/hook")
        asyncio.run(driver.deliver(message))
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    import json as _json

    return dict(seen[0].headers), _json.loads(seen[0].content)


#: How each shipped driver is driven, and what the destination then saw, as text.
#:
#: The test below derives the drivers from discovery and refuses to run unless this mapping covers
#: exactly them, so a new channel owes the same proof.
_DRIVER_PROBES: dict[str, str] = {
    "chemclaw.deliver.driver:file_channel": "file",
    "chemclaw.deliver.driver:webhook_channel": "webhook",
}


def _file_traces(message: Message, tmp_path: Path) -> list[str]:
    """Deliver twice through the real file driver; return what the destination holds, as names."""
    driver = FileDeliveryDriver(name="probe", directory=str(tmp_path / "outbox"))
    asyncio.run(driver.deliver(message))
    asyncio.run(driver.deliver(message))
    return [path.name for path in sorted((tmp_path / "outbox").iterdir())]


def _webhook_traces(message: Message, _tmp_path: Path) -> list[str]:
    """Deliver twice through the real webhook driver; return what went on the wire, as text."""
    import httpx

    from chemclaw.deliver.driver import WebhookDeliveryDriver

    seen: list[str] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{dict(request.headers)} {request.content.decode()}")
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    try:
        httpx.AsyncClient = lambda *a, **k: real_client(  # type: ignore[assignment,misc]
            *a, **{**k, "transport": httpx.MockTransport(_handle)}
        )
        driver = WebhookDeliveryDriver(name="probe", url="http://127.0.0.1:1/hook")
        asyncio.run(driver.deliver(message))
        asyncio.run(driver.deliver(message))
    finally:
        httpx.AsyncClient = real_client  # type: ignore[misc]
    return seen


def test_every_shipped_delivery_driver_makes_a_redelivery_identifiable(tmp_path: Path) -> None:
    """Every shipped delivery driver makes a redelivery identifiable.

    `deliver_message_activity` is at-least-once: a retry after a timeout or worker death re-walks
    every channel, including ones that already took the message, and no local record can know
    whether a POST landed. So each destination must be able to recognise a redelivery. The driver
    set is derived from discovery, so a new channel must carry `message_id` or be idempotent by
    construction.
    """
    from chemclaw.deliver.driver import message_id

    drivers = {manifest.driver for manifest in discovered().values()}
    assert drivers == set(_DRIVER_PROBES), (
        "a channel was added or its driver renamed without saying how a redelivery of one message "
        f"is identifiable at its destination: {sorted(drivers ^ set(_DRIVER_PROBES))}"
    )

    message = Message(recipient="u-1", subject="s", body="b", kind="job-result")
    identity = message_id(message)
    probes = {"file": _file_traces, "webhook": _webhook_traces}
    for driver, probe in sorted(_DRIVER_PROBES.items()):
        traces = probes[probe](message, tmp_path)
        assert traces, f"{driver} delivered nothing to observe"
        assert all(identity in trace for trace in traces), (
            f"{driver} left the destination no way to tell a redelivery of one message from a "
            f"second message: {traces}"
        )


def test_the_webhook_carries_a_dedup_handle_the_file_channel_already_had() -> None:
    """The webhook carries a dedup handle, as the file channel already did.

    A re-run activity re-POSTs, and `correlation_id` is excluded from the payload, so the receiver
    needs a key: `Idempotency-Key` (what chat and ticketing hosts read) and `message_id` in the body
    (what a site's own receiver reads).
    """
    from chemclaw.deliver.driver import message_id

    message = Message(recipient="u-1", subject="s", body="b", kind="digest")
    headers, payload = _post_and_capture(message)
    assert payload["message_id"] == message_id(message)
    assert headers["idempotency-key"] == message_id(message)


def test_two_messages_differing_only_in_kind_are_not_the_same_message() -> None:
    """A digest and a job result with the same body must not share an `Idempotency-Key`.

    Otherwise a receiver deduping on it would drop a real `job-result`.
    """
    from chemclaw.deliver.driver import message_id

    # Annotated because `Message` is no longer all-`str`: `attachments` makes an inferred
    # `dict[str, str]` unassignable to `**kwargs`, which is mypy telling the truth about a
    # widened model rather than a defect here.
    common: dict[str, Any] = {"recipient": "u-1", "subject": "s", "body": "b"}
    assert message_id(Message(**common, kind="digest")) != message_id(
        Message(**common, kind="job-result")
    )


def test_the_file_channel_is_never_observed_half_written(tmp_path: Path) -> None:
    """A reader on the share must never see a truncated digest.

    `Path.write_text` truncates then writes, and a redelivery overwrites the same content-hash path,
    so readers holding no lock could see a short file. Temp file plus `os.replace` is atomic within
    a filesystem, so this asserts "never", not a rate.
    """
    import threading

    from chemclaw.deliver.driver import FileDeliveryDriver as Driver

    driver = Driver(name="probeshare", directory=str(tmp_path))
    message = Message(recipient="u-1", subject="digest", body="x" * 200_000, kind="digest")
    asyncio.run(driver.deliver(message))
    full = next(tmp_path.glob("*.md")).stat().st_size

    stop = threading.Event()
    short: list[int] = []

    def _read() -> None:
        while not stop.is_set():
            for path in tmp_path.glob("*.md"):
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    continue
                if size != full:
                    short.append(size)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        for _ in range(40):
            asyncio.run(driver.deliver(message))
    finally:
        stop.set()
        reader.join(timeout=5)

    assert not short, (
        f"{len(short)} read(s) saw a partial digest (sizes {sorted(set(short))[:3]}); the share is "
        "read without a lock, so a rewrite must be an atomic replace"
    )


# --- attachments --------------------------------------------------------------------------------


def test_an_attachment_reaches_the_share_as_its_own_file(tmp_path: Path) -> None:
    """An attachment reaches the share as its own file.

    Asserted as a second file carrying the exact bytes, which a spreadsheet or ELN can open, not a
    mention of one in the message.
    """
    message = Message(
        recipient="u-1",
        subject="Report drafted",
        body="see attached",
        kind="report",
        attachments=[
            Attachment(filename="run-sheet.csv", media_type="text/csv", content=b"a,b\r\n1,2\r\n")
        ],
    )

    asyncio.run(FileDeliveryDriver("share", str(tmp_path)).deliver(message))

    sheet = next(path for path in tmp_path.iterdir() if path.name.endswith("run-sheet.csv"))
    assert sheet.read_bytes() == b"a,b\r\n1,2\r\n"
    note = next(path for path in tmp_path.iterdir() if path.suffix == ".md")
    # ...and the message names it, because a reader opening the `.md` must know the file exists.
    assert f"File: {sheet.name}" in note.read_text(encoding="utf-8")


def test_two_messages_carrying_one_filename_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Two designs both exporting `run-sheet.csv` is the ordinary case, not the edge one.

    The message file is content-addressed for exactly this reason, and the attachment is the half a
    chemist actually opens.
    """
    driver = FileDeliveryDriver("share", str(tmp_path))
    for body in (b"first", b"second"):
        asyncio.run(
            driver.deliver(
                Message(
                    recipient="u-1",
                    subject=f"sheet {body.decode()}",
                    kind="report",
                    attachments=[Attachment(filename="run-sheet.csv", content=body)],
                )
            )
        )

    sheets = sorted(path for path in tmp_path.iterdir() if path.name.endswith("run-sheet.csv"))
    assert len(sheets) == 2
    assert {path.read_bytes() for path in sheets} == {b"first", b"second"}


def test_an_attachment_filename_cannot_escape_the_outbox() -> None:
    """Exactly `kind`'s argument, one field over: the driver joins this onto a directory."""
    from pydantic import ValidationError

    for hostile in ("/etc/cron.d/x", "../../../etc/x", "a/b", ".hidden", ""):
        with pytest.raises(ValidationError):
            Attachment(filename=hostile, content=b"x")


def test_an_attachment_is_redacted_like_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scrub exists because a delivery leaves the cluster, and an attachment is what leaves.

    Typing the field `bytes` is not a reason to skip it: the artefacts this seam carries are text
    assembled from tool results, exactly as a body is.
    """
    monkeypatch.setenv("CHEMCLAW_TEST_DELIVERY_SECRET", "hunter2-abcdefghijklmnop")
    monkeypatch.setattr(
        "chemclaw.deliver.message._connector_secret_envs",
        lambda: ("CHEMCLAW_TEST_DELIVERY_SECRET",),
    )
    message = Message(
        recipient="u-1",
        subject="s",
        attachments=[
            Attachment(filename="log.txt", content=b"Authorization: hunter2-abcdefghijklmnop")
        ],
    )

    scrubbed = message.redacted().attachments[0].content

    assert b"hunter2-abcdefghijklmnop" not in scrubbed


def test_a_message_carries_no_credential_a_driver_quoted_back_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r"""A message carries no credential a driver quoted back as JSON.

    The structural (pattern) half of the outbound scrub, the only half that can catch a credential
    belonging to somebody else, e.g. a warehouse driver quoting its binding in an error. Driver
    messages often carry JSON inside a JSON string, so the separator arrives backslash-escaped
    (`{\"password\": …}`). Two credentials from two different rules (libpq `password`, compound
    `api_key`) so a regression in either shows here.
    """
    monkeypatch.setattr(
        "chemclaw.deliver.message._connector_secret_envs",
        lambda: (),  # nothing in the value inventory, so only the structural rules can catch these
    )
    quoted = json.dumps(json.dumps({"password": "W4rehousePw1", "api_key": "sk_live_9f3a2b1c8d7"}))
    text = f"The warehouse refused the binding. Its error was: {quoted}"
    message = Message(
        recipient="u-1",
        subject=f"run failed: {quoted}",
        body=text,
        attachments=[Attachment(filename="report.md", content=text.encode("utf-8"))],
    )

    scrubbed = message.redacted()
    delivered = "".join(
        [
            scrubbed.recipient,
            scrubbed.subject,
            scrubbed.body,
            scrubbed.attachments[0].content.decode("utf-8"),
        ]
    )

    for credential in ("W4rehousePw1", "sk_live_9f3a2b1c8d7"):
        assert credential not in delivered, (
            f"{credential} left the cluster in a message: {delivered}"
        )


def test_a_binary_attachment_survives_the_redaction_rather_than_failing_the_delivery() -> None:
    """A binary attachment survives the redaction rather than failing the delivery.

    `redact_secrets` works on text; bytes that do not decode have none to scrub, and raising would
    fail a courtesy copy of a result that is already durable.
    """
    payload = bytes(range(256))
    message = Message(
        recipient="u-1", subject="s", attachments=[Attachment(filename="x.bin", content=payload)]
    )

    assert message.redacted().attachments[0].content == payload


def test_an_attachment_crosses_the_wire_as_base64_and_comes_back_whole() -> None:
    """An attachment crosses the wire as base64 and comes back whole.

    `OutboundMessage` is a durable Temporal payload, and pydantic's default `bytes` handling is a
    utf-8 decode that raises outside it. Driven over the JSON a converter would send.
    """
    import json

    payload = bytes(range(256))
    message = Message(
        recipient="u-1", subject="s", attachments=[Attachment(filename="x.bin", content=payload)]
    )

    wire = json.loads(message.model_dump_json())

    assert isinstance(wire["attachments"][0]["content"], str)
    assert Message.model_validate(wire).attachments[0].content == payload


def test_the_same_message_with_and_without_a_file_are_not_the_same_delivery() -> None:
    """A receiver deduping on the key must not drop the copy that carries the artefact.

    The other direction matters as much: the key reads the attachment's *identity* and not its
    bytes, so a redraft of one report stays one delivery rather than becoming a second.
    """
    from chemclaw.deliver.driver import message_id

    bare = Message(recipient="u-1", subject="s", body="b", kind="report")
    with_file = bare.model_copy(
        update={"attachments": [Attachment(filename="r.md", content=b"first")]}
    )
    redrafted = bare.model_copy(
        update={"attachments": [Attachment(filename="r.md", content=b"second")]}
    )

    assert message_id(bare) != message_id(with_file)
    assert message_id(with_file) == message_id(redrafted)
