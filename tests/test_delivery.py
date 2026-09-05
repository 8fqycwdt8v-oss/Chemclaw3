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
import logging
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


def test_the_webhook_never_follows_an_ambient_proxy() -> None:
    """A pod's `HTTP_PROXY` must not silently reroute a delivery — with its body and its bearer.

    Every other client in this tree that reaches a real dependency sets `trust_env=False` and says
    why (`connectors/registry.py`, `core/mcp_session.py`, `core/embeddings.py`,
    `connectors/health.py`, `agent/llm_provider.py`, and the sibling seam
    `publish/drivers/http.py`). The delivery channel — the one client whose payload is
    human-readable message content *and* which attaches `Authorization: Bearer` — was the
    exception. Measured against a recording listener installed as `HTTP_PROXY`: the proxy received
    the full `POST`, the JSON body and `Authorization: Bearer s3cr3t-bearer-value`, and the
    configured destination received nothing.

    Driven through a real socket rather than by inspecting the client's attributes, because the
    property under test is where the bytes go. A listener that accepts and answers `200` stands in
    for the proxy; the configured host is unroutable, so *any* delivery that completes at all
    completed through the proxy.
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

    **And it is asked whatever this environment's posture happens to be**, which is the half that
    shipped inert. This test used to assert the opposite — "the posture check must not fire where
    the posture is not enforced" — and that assertion was the defect written down as a contract:
    `entra_required` defaults `False`, nothing in `ci.yml`, the chart or the runbook sets it where
    `channel-validate` runs, so the rule added to stop a plaintext channel merging silently could
    only ever fire on a deployment that had already turned enforcement on. It is the same
    CI-blindness rules 2 and 3 work around by iterating discovered rather than enabled manifests,
    one layer further in. A validator asks about the manifest, not about today's environment: a
    channel that will be refused the day enforcement is turned on is a broken channel today.

    The construction site is the one that still reads the setting, and
    `test_a_driver_built_outside_the_gate_still_refuses_a_cleartext_destination` holds that half.
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
    """The other half of the one definition: construction asks about *this* deployment.

    `plaintext_channel_refusal` takes the posture as a parameter now, so there are two askers and
    they ask different questions. The validator asks "is this manifest legal under enforcement?"
    and always passes `enforced=True`. Building a driver is an act rather than a review — it opens
    a destination *now* — so it passes `settings.entra_required`, and a dev deployment with
    enforcement off must still be able to run a plaintext channel against a local host.

    Both directions here, because making the validator unconditional is exactly the change that
    could have made construction unconditional too by accident, and that would refuse every
    local-dev http channel in a repository whose whole live lane is local.
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


def test_the_posture_check_walks_into_a_list_or_a_nested_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Free-form means the shape is free too, not only the key's spelling.

    The first version of this rule looked at top-level `str` values, which is a second assumption
    about a block whose whole design point is that a site writes any driver against it. A fan-out
    driver taking `urls: [a, b]` and a failover driver taking `endpoints: {primary: …}` both
    escaped it entirely — and escaped it silently, which is the failure mode the rule exists to end.

    Bounded rather than fully recursive on purpose (`_config_strings`): the three shapes a
    destination is realistically written in, and then it stops.
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
    """The natural next step from the `urls`/`endpoints` examples was silently one hop too far.

    `_config_strings` shipped with `depth=2`, and its own docstring said it reached "a list, and one
    level of nesting inside either" — but the `depth <= 0` guard fired on the *container itself*
    before it looked inside it, one hop earlier than that prose promised. A fan-out driver pairing
    each URL with per-target metadata (`targets: [{url: …}, {url: …}]`, headers or its own
    `token_env` beside it) is exactly that natural next step, and it passed this security gate under
    `entra_required=True` with **zero** problems reported — confirmed directly:
    `_config_strings({"targets": [{"url": "http://b"}]})` returned `[]`. The gap was symmetric: a
    dict of per-target lists (`endpoints: {primary: [url, …]}`) escaped the same way. Both are
    asserted here, refused exactly like the shallower `urls`/`endpoints` shapes already are.
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
    """Correcting the depth to match its documented intent must not turn it into a free traversal.

    `_config_strings` is deliberately bounded — a destination buried deeper than the three
    documented container hops (`config`'s own values, one level of nesting, and the strings inside
    that nesting) is outside what rule 4 claims to see, and this proves the corrected depth still
    stops there rather than walking arbitrarily deep structures. A fourth hop
    (`targets: [{urls: [url]}]` — list of dicts of *lists*, one level past the fan-out shape the
    other test closes) must still be silently out of scope, and a pathologically deep structure must
    not raise or hang.
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
    """The config-versus-outage split had a hole exactly where the file driver is.

    `FileDeliveryDriver.__init__` touched no filesystem, so `build()` succeeded for any string at
    all and a bad `directory:` first surfaced at `mkdir` time inside `deliver()` — on
    `chemclaw_delivery_failures_total`, the series an operator reads as "the destination is having a
    bad afternoon". A path with a regular file where a directory belongs will fail identically on
    every message until somebody edits a manifest, which is the definition of the *other* counter.

    Both directions, because the easy over-correction is worse than the defect: a directory that
    does not exist yet is **not** a misconfiguration — creating it is what a first delivery to a
    fresh mount does — and refusing it would turn every new deployment's first message into an
    alert.
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


def test_a_secret_in_the_recipient_is_scrubbed_like_one_in_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam's rule is "every message is redacted once, in the registry" — every free-text field.

    `recipient` was the one it skipped. It is free text by construction — the address a driver
    resolves, whose shape only the driver knows — and both shipped drivers put it where the body
    goes: `FileDeliveryDriver` writes it into the file, `WebhookDeliveryDriver` POSTs it. Today's
    only caller passes an actor id, so nothing carries a credential there yet; the guarantee is
    supposed to be a property of the seam rather than of who happens to be calling it.
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
    """A rewritten address is an undelivered message, and nothing said so.

    `redact_secrets` rewrites *structural* shapes as well as this deployment's own secret values,
    and a routable address can be one: a Teams channel URN
    (`urn:teams:channel:19:meeting_TOKEN=…@thread.v2`) loses its token to the `TOKEN=` pattern, a
    webhook URL with userinfo loses its password, and a `xoxb-`-shaped address is replaced whole.
    The scrub stays — `recipient` is free text that both shipped drivers put where the body goes,
    and the seam's guarantee must not depend on who is calling it — but a substitution here does
    not merely redact a message, it re-addresses one, and the driver that fails to deliver it can
    only report the address it was given.

    The original is deliberately absent from the log line: what tripped the pattern may be a real
    credential, and this module is the half that leaves the cluster.
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
