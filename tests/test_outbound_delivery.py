"""Every declared delivery kind has a producer, and the seam they share cannot fail a job.

`deliver/message.py` bounds `Message.kind` to four values, and until
`D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel` exactly one of them was ever
constructed: the nightly digest. The other three named the three things a chemist most needs to hear
about while they are *not* in a session — a question waiting on them, a job they launched finishing,
a report they asked for being written — and each of those workflows stopped at `session_events`,
which only a session reads.

The first test here is therefore an **absence** test, in the shape
`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` established. It is not enough
that three producers exist today, because what shipped was a `Literal` nobody was obliged to
satisfy: a vocabulary with no caller is a claim about a capability. It reads the `Literal` and the
tree together, so adding a fifth kind without a producer, or deleting the producer of a fourth, is
red rather than silent.

The rest drive the real activity against a real file channel. The seam's contract is that it never
raises — every caller's scientific result is already durable by the time it runs — so the two ways
it can be handed something unusable (an empty addressee, a kind outside the vocabulary) are asserted
to be *counted* rather than thrown, and the second is asserted against the outbox directory as well,
because that `Literal` is what stops a `kind` becoming an arbitrary file write.
"""

import ast
import asyncio
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.deliver.message import Message
from chemclaw.durable.deliver_message import (
    OutboundMessage,
    deliver_best_effort,
    deliver_message_activity,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def _declared_kinds() -> set[str]:
    """The vocabulary `Message.kind` bounds, read off the model rather than transcribed."""
    annotation = Message.model_fields["kind"].annotation
    assert get_origin(annotation) is Literal, "Message.kind must stay a Literal — it is the bound"
    return {str(value) for value in get_args(annotation)}


def _produced_kinds() -> dict[str, set[str]]:
    """Every `kind=` literal passed to an `OutboundMessage(...)` in `src/`, by module.

    AST rather than grep for the reason `no_egress.py` gives one repository over: `kind="report"`
    and `kind = "report"` read differently as text and identically as a tree. A non-literal `kind=`
    is deliberately not collected — it would be a value derived from a payload, which is the
    arbitrary-file-write shape `Message.kind`'s `Literal` exists to refuse, and this scan must not
    quietly credit it as a producer.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "OutboundMessage":
                continue
            for keyword in node.keywords:
                if keyword.arg == "kind" and isinstance(keyword.value, ast.Constant):
                    found.setdefault(str(keyword.value.value), set()).add(
                        str(path.relative_to(SRC))
                    )
    return found


def test_every_declared_delivery_kind_has_a_producer() -> None:
    """The whole finding, as an assertion: three of four kinds were a vocabulary with no caller.

    Stated as set equality in both directions. A declared kind nobody constructs is a capability
    the README claims and the tree does not have; a constructed kind the `Literal` does not declare
    cannot be built at all, so the second direction is what tells whoever adds one that the model is
    where to add it.
    """
    produced = _produced_kinds()
    assert set(produced) == _declared_kinds(), (
        "every value of Message.kind needs a producer in src/ and vice versa; "
        f"produced={ {k: sorted(v) for k, v in produced.items()} }"
    )


def test_each_kind_is_produced_by_the_workflow_that_owns_that_event() -> None:
    """Named, so a producer moved into the wrong workflow is a failure rather than a shrug.

    The three added kinds are not interchangeable: each is the *only* out-of-session notice its
    workflow can send, so a `job-result` constructed anywhere but the connector-job wrapper means
    either a second answer to one question or a finished job that still tells nobody.
    """
    produced = _produced_kinds()
    assert produced["digest"] == {"durable/digest.py"}
    assert produced["report"] == {"durable/report_workflow.py"}
    assert produced["job-result"] == {"durable/connector_job.py"}
    assert produced["awaiting"] == {"durable/awaiting.py"}


def _local_channel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Enable one real file channel and return its outbox, the way `test_delivery.py` does."""
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
    return outbox


def _degraded_series(subsystem: str) -> str:
    """The one rendered line for this subsystem, or `""` before its first increment.

    Read off `render()` rather than `value()`, because `value()` sums a counter across every label
    set — right for its own purpose and useless here, where a concurrent degradation in another
    subsystem would move the number this test is asserting about.
    """
    marker = f'chemclaw_degraded_total{{subsystem="{subsystem}"}}'
    for line in METRICS.render().splitlines():
        if line.startswith(marker):
            return line
    return ""


def test_every_kind_reaches_a_configured_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Driven through the real driver, because the point of the fix is a file that lands.

    One message per declared kind, so this fails if a kind is declared that the driver cannot
    actually name a file after — the `Literal` and the filename are the same decision.
    """
    outbox = _local_channel(monkeypatch, tmp_path)
    for kind in sorted(_declared_kinds()):
        took = asyncio.run(
            deliver_message_activity(
                OutboundMessage(recipient="u-1", subject=f"a {kind}", body="body", kind=kind)
            )
        )
        assert took == ["local"], kind
    written = sorted(path.name for path in outbox.iterdir())
    for kind in sorted(_declared_kinds()):
        owned = [name for name in written if name.startswith(f"{kind}-")]
        assert len(owned) == 1, f"the driver names a file after the kind; {kind!r} owns {owned}"
    assert len(written) == len(_declared_kinds()), f"nothing else was written: {written}"


def test_an_unaddressable_message_is_counted_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`Message.recipient` is `min_length=1`, and this activity is the only place that may notice.

    The inversion this prevents is on record twice: a `ValidationError` raised in workflow code is
    not catchable by any best-effort wrapper, so the notice that must never fail the job becomes the
    thing that fails it. `AwaitingWorkflow._push` carries a guard for exactly this and the digest's
    delivery carried a comment; here it is an assertion.
    """
    _local_channel(monkeypatch, tmp_path)
    before = _degraded_series("message_delivery")
    took = asyncio.run(deliver_message_activity(OutboundMessage(recipient="", subject="s")))
    assert took == []
    assert _degraded_series("message_delivery") != before


def test_a_kind_outside_the_vocabulary_never_reaches_the_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The `Literal` is a path bound, and this seam now carries values from four callers.

    `FileDeliveryDriver` builds `directory / f"{kind}-{identity}{suffix}"`, so a `../`-bearing kind
    escapes the outbox with `mkdir(parents=True)` creating whatever it traverses to.
    `OutboundMessage` is deliberately a plain `str` there — the constraint has to fail inside the
    activity — so this is the assertion that the looser wire model did not loosen the bound.
    """
    outbox = _local_channel(monkeypatch, tmp_path)
    before = _degraded_series("message_delivery")
    took = asyncio.run(
        deliver_message_activity(
            OutboundMessage(recipient="u-1", subject="s", kind="../../../etc/cron.d/escape")
        )
    )
    assert took == []
    assert _degraded_series("message_delivery") != before
    assert not outbox.exists() or list(outbox.iterdir()) == []


def test_delivery_that_is_off_is_not_a_degradation() -> None:
    """The shipped posture. Nothing is configured, so nothing is sent and nothing is wrong."""
    before = _degraded_series("message_delivery")
    assert settings.delivery_channel_list == []
    message = OutboundMessage(recipient="u-1", subject="s")
    assert asyncio.run(deliver_message_activity(message)) == []
    assert _degraded_series("message_delivery") == before


def test_a_message_with_no_addressee_never_schedules_the_activity() -> None:
    """A wait open to anyone entitled, or a job launched by a Schedule, has nobody to write to.

    Proved by calling the wrapper outside a workflow: `workflow.execute_activity` raises there, so
    reaching it at all is the failure. That makes the early return a real short-circuit rather than
    a value this test asserts about its own mock.
    """
    assert asyncio.run(deliver_best_effort(OutboundMessage(recipient="", subject="s"))) == []
