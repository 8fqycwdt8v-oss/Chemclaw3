"""Every declared delivery kind has a producer, and the seam they share cannot fail a job.

`Message.kind` names what a chemist needs to hear about outside a session: the digest, a question
waiting on them, a job finishing, a report written. The first test reads the `Literal` and the
tree together so a kind without a producer is red
(`D-2026-09-14-a-declared-kind-with-no-producer-is-not-a-channel`). The rest drive the real
activity against a file channel: it never raises, so an empty addressee or an out-of-vocabulary
kind is counted, and the latter never becomes a file write outside the outbox.
"""

import ast
import asyncio
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest

from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.deliver.message import Message
from chemclaw.durable import connector_job
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


def _constructor_name(node: ast.AST) -> str | None:
    """The callee's name, qualified or bare: `OutboundMessage` and `x.OutboundMessage` alike."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kind_of(call: ast.Call, constants: dict[str, str] | None = None) -> str | None:
    """The kind an `OutboundMessage(...)` actually sends: its literal `kind=`, or the model's
    default.

    A non-literal `kind=` is not credited (it would be payload-derived, the shape the `Literal`
    refuses). An omitted `kind=` is credited with the default, read off the model, so a producer
    that forgets the keyword still shows up. A `**kwargs` splat returns `None` (fail closed).
    `constants` are the module's own `NAME = "literal"` bindings, which are source-fixed, so
    `kind=CHECK_IN_KIND` resolves.
    """
    if _constructor_name(call.func) != "OutboundMessage":
        return None
    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        if keyword.arg == "kind":
            if isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
            if isinstance(keyword.value, ast.Name):
                return (constants or {}).get(keyword.value.id)
            return None
    return str(OutboundMessage.model_fields["kind"].default)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """This module's top-level `NAME = "literal"` bindings — nothing nested, nothing computed."""
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = node.value.value
    return found


def _sent_kinds() -> dict[str, set[str]]:
    """Every kind that actually reaches `deliver_best_effort`, by module.

    Constructing a message is not sending it, so the walk starts at the send site and follows one
    hop: the argument is the construction, or a call to a same-module function whose body constructs
    it. A producer behind two hops is not counted, which fails closed.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        builders = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _constructor_name(node.func) != "deliver_best_effort":
                continue
            for argument in node.args:
                reached: list[ast.Call] = []
                if isinstance(argument, ast.Call):
                    if _kind_of(argument, constants) is not None:
                        reached.append(argument)
                    else:
                        name = _constructor_name(argument.func)
                        body = builders.get(name or "")
                        if body is not None:
                            reached.extend(
                                inner
                                for inner in ast.walk(body)
                                if isinstance(inner, ast.Call)
                                and _kind_of(inner, constants) is not None
                            )
                for call in reached:
                    kind = _kind_of(call, constants)
                    if kind is not None:
                        found.setdefault(kind, set()).add(str(path.relative_to(SRC)))
    return found


def _sending_functions() -> dict[str, set[str]]:
    """Every kind that reaches `deliver_best_effort`, by the function that sends it.

    Finer than `_sent_kinds` because `connector_job.py` sends `job-result` from both `_finish` and
    `_notify_failure`, and a module-level set is satisfied by either alone. Same one-hop,
    fail-closed rule.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_constants(tree)
        builders = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for holder in builders.values():
            for node in ast.walk(holder):
                if not isinstance(node, ast.Call):
                    continue
                if _constructor_name(node.func) != "deliver_best_effort":
                    continue
                for argument in node.args:
                    if not isinstance(argument, ast.Call):
                        continue
                    reached = [argument] if _kind_of(argument, constants) is not None else []
                    if not reached:
                        body = builders.get(_constructor_name(argument.func) or "")
                        if body is not None:
                            reached = [
                                inner
                                for inner in ast.walk(body)
                                if isinstance(inner, ast.Call)
                                and _kind_of(inner, constants) is not None
                            ]
                    for call in reached:
                        kind = _kind_of(call, constants)
                        if kind is not None:
                            where = f"{path.relative_to(SRC)}::{holder.name}"
                            found.setdefault(kind, set()).add(where)
    return found


def test_a_job_that_fails_tells_its_requester_and_not_only_a_job_that_finishes() -> None:
    """A failing job tells its requester, not only a finishing one.

    A silent outcome invites assuming the good one. `Message.kind` has no failure value, so the
    kind-level equality cannot see this; the sending functions are named instead.
    """
    sending = _sending_functions()
    assert "durable/connector_job.py::_notify_failure" in sending.get("job-result", set()), (
        "a connector job that fails sends nothing to the chemist who launched it; the outbound "
        "copy exists on the success path alone, and `_notify_failure`'s early return covers the "
        "Schedule- or inbox-started run that has no session to fall back on"
    )
    assert "durable/connector_job.py::_finish" in sending.get("job-result", set()), (
        "the success path stopped sending; this test names both outcomes on purpose"
    )


def test_a_producer_that_forgets_the_keyword_is_not_invisible() -> None:
    """A producer that omits `kind=` is credited with the default, not invisible.

    Otherwise a producer could deliver under the wrong kind (as a `digest`) with every scan green.
    Driven on parsed source so it holds whatever `src/` does next.
    """
    omitted = ast.parse('OutboundMessage(recipient="u-1", subject="s")').body[0]
    assert isinstance(omitted, ast.Expr) and isinstance(omitted.value, ast.Call)
    assert _kind_of(omitted.value) == OutboundMessage.model_fields["kind"].default, (
        "a producer that omits `kind=` still sends the default, and a scan that cannot see it "
        "cannot tell a deliberate digest from a forgotten one"
    )

    named = ast.parse('OutboundMessage(recipient="u-1", kind=CHECK_IN_KIND)').body[0]
    assert isinstance(named, ast.Expr) and isinstance(named.value, ast.Call)
    assert _kind_of(named.value, {"CHECK_IN_KIND": "work-check-in"}) == "work-check-in"

    splat = ast.parse("OutboundMessage(**payload)").body[0]
    assert isinstance(splat, ast.Expr) and isinstance(splat.value, ast.Call)
    assert _kind_of(splat.value) is None, "a splat may carry a kind this cannot see; fail closed"


def test_every_declared_delivery_kind_has_a_producer() -> None:
    """Declared and produced kinds are equal sets.

    A declared kind nobody sends is a claimed capability the tree lacks; a sent kind the `Literal`
    does not declare cannot be built, and this tells whoever adds one where to declare it.
    """
    produced = _sent_kinds()
    assert set(produced) == _declared_kinds(), (
        "every value of Message.kind needs a producer in src/ and vice versa; "
        f"produced={ {k: sorted(v) for k, v in produced.items()} }"
    )


def test_each_kind_is_produced_by_the_workflow_that_owns_that_event() -> None:
    """Each kind is produced by the workflow that owns that event.

    Each is the only out-of-session notice its workflow sends, so a producer in the wrong workflow
    is either a second answer or a silent event.
    """
    produced = _sent_kinds()
    assert produced["digest"] == {"durable/digest.py"}
    assert produced["report"] == {"durable/report_workflow.py"}
    assert produced["job-result"] == {"durable/connector_job.py"}
    assert produced["awaiting"] == {"durable/awaiting.py"}
    assert produced["work-check-in"] == {"durable/check_in.py"}


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

    Read off `render()` because `value()` sums across label sets, including other subsystems.
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
    """An unaddressable message is counted rather than raised.

    `Message.recipient` is `min_length=1`; a `ValidationError` in workflow code escapes every
    best-effort wrapper, so the activity is where it must be noticed.
    """
    _local_channel(monkeypatch, tmp_path)
    before = _degraded_series("message_delivery")
    took = asyncio.run(deliver_message_activity(OutboundMessage(recipient="", subject="s")))
    assert took == []
    assert _degraded_series("message_delivery") != before


def test_a_kind_outside_the_vocabulary_never_reaches_the_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A kind outside the vocabulary never reaches the outbox.

    `FileDeliveryDriver` builds `directory / f"{kind}-{identity}{suffix}"`, so the `Literal` is a
    path bound; `OutboundMessage` is a plain `str` so the refusal lands in the activity. The payload
    climbs exactly one level, which always has a parent, so without the bound the write would
    succeed and the assertions would catch it.
    """
    outbox = _local_channel(monkeypatch, tmp_path)
    before = _degraded_series("message_delivery")
    took = asyncio.run(
        deliver_message_activity(OutboundMessage(recipient="u-1", subject="s", kind="../escape"))
    )
    assert took == [], "a kind outside the vocabulary was delivered rather than refused"
    assert _degraded_series("message_delivery") != before
    # The refusal is what is asserted, not the outbox being empty: a *successful* escape leaves
    # the outbox empty too, by definition, so that assertion cannot tell the two apart. The
    # escape's landing site is checked directly instead.
    assert list(outbox.parent.glob("escape-*")) == []
    assert not outbox.exists() or list(outbox.iterdir()) == []


def test_delivery_that_is_off_is_not_a_degradation() -> None:
    """The shipped posture. Nothing is configured, so nothing is sent and nothing is wrong."""
    before = _degraded_series("message_delivery")
    assert settings.delivery_channel_list == []
    message = OutboundMessage(recipient="u-1", subject="s")
    assert asyncio.run(deliver_message_activity(message)) == []
    assert _degraded_series("message_delivery") == before


def test_a_message_with_no_addressee_never_schedules_the_activity() -> None:
    """A message with no addressee never schedules the activity.

    Called outside a workflow, where `workflow.execute_activity` raises, so reaching it is the
    failure.
    """
    assert asyncio.run(deliver_best_effort(OutboundMessage(recipient="", subject="s"))) == []


def test_one_misspelled_channel_does_not_cost_the_healthy_ones_their_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One misspelled channel does not cost the healthy ones their message.

    Each channel is tried independently, and an unresolvable name is reported through `degraded()`.
    `enabled()` still raises for `make channel-validate` and startup.
    """
    outbox = _local_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "delivery_channels", "local,typo")
    before = _degraded_series("delivery_channel_config")

    took = asyncio.run(
        deliver_message_activity(
            OutboundMessage(recipient="u-1", subject="a report", body="body", kind="report")
        )
    )

    assert took == ["local"], "the channel that resolves must still receive the message"
    assert len(list(outbox.iterdir())) == 1
    assert _degraded_series("delivery_channel_config") != before, (
        "an unresolvable channel name is a configuration fault and must be counted, not silent"
    )


def test_two_runs_of_one_job_are_two_messages_and_a_retry_is_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two runs of one job are two messages; a retry of one is one.

    A `job-result` body has no natural discriminator, so a content-only key would make a receiver
    drop a genuine second result. Both directions are asserted, since a retry (at-least-once under
    `BAD_DATA_RETRY`) must still collapse.
    """
    outbox = _local_channel(monkeypatch, tmp_path)

    def _result(correlation_id: str) -> OutboundMessage:
        return OutboundMessage(
            recipient="chemist@corp",
            subject="calc:run_conformer_search finished",
            body="Found 12 conformers.",
            kind="job-result",
            correlation_id=correlation_id,
        )

    asyncio.run(deliver_message_activity(_result("corr-A")))
    asyncio.run(deliver_message_activity(_result("corr-A")))
    assert len(list(outbox.iterdir())) == 1, "a retry of one delivery is one message"

    asyncio.run(deliver_message_activity(_result("corr-B")))
    assert len(list(outbox.iterdir())) == 2, (
        "a second run of the same job is a second result and must not be deduped away"
    )


def test_the_report_message_carries_the_draft_and_not_only_its_reference() -> None:
    """The report message carries the draft, not only its reference.

    Its reader is not looking at the knowledge graph. Asserted against the workflow source because
    driving it needs a broker. The attachment is named for the report, not `note_ref` (a commit sha
    that tells a chemist nothing and might fail `Attachment`'s pattern).
    """
    source = (SRC / "durable" / "report_workflow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _constructor_name(node.func) == "deliver_best_effort"
    ]
    assert len(sends) == 1, "the report has one delivery site; this test reads that one"
    built = sends[0].args[0]
    assert isinstance(built, ast.Call)
    attachments = [keyword.value for keyword in built.keywords if keyword.arg == "attachments"]
    assert attachments, "the report delivery carries no attachment; a note id is not a deliverable"
    named = ast.unparse(attachments[0])
    assert "drafted.id" in named and "note_ref" not in named
    assert "drafted.body" in named


def test_an_attachment_a_workflow_builds_is_checked_where_it_can_be_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`OutboundAttachment` is loose so a bad filename is refused in the activity, not workflow
    code.

    A `ValidationError` in workflow code would fail the job the notice must never fail.
    """
    from pydantic import ValidationError

    from chemclaw.deliver.message import Attachment
    from chemclaw.durable.deliver_message import OutboundAttachment, OutboundMessage

    hostile = OutboundAttachment(filename="../../etc/x", content=b"x")
    with pytest.raises(ValidationError):
        Attachment(filename=hostile.filename, content=hostile.content)

    # ...and the activity swallows it, with a channel enabled: with delivery off the activity
    # returns `[]` first, which would make this assertion vacuous.
    outbox = _local_channel(monkeypatch, tmp_path)
    took = asyncio.run(
        deliver_message_activity(
            OutboundMessage(recipient="u-1", subject="s", attachments=[hostile])
        )
    )

    assert took == []
    assert not list(outbox.iterdir()) if outbox.exists() else True


def test_a_sessionless_job_that_fails_still_tells_its_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sessionless job that fails still tells its requester, driven through `_notify_failure`.

    `_sending_functions` shows the call exists, not that it is reachable. The sessionless arm
    matters: a Schedule- or inbox-started run has no session, and the outbound copy is all that
    tells anyone.
    """
    from chemclaw.durable.connector_job import ConnectorJobInput, ConnectorJobWorkflow

    sent: list[OutboundMessage] = []

    async def _record(message: OutboundMessage) -> list[str]:
        sent.append(message)
        return ["local"]

    monkeypatch.setattr(connector_job, "deliver_best_effort", _record)

    job = ConnectorJobInput(
        connector="calc",
        job="run_conformer_refinement",
        workflow="ConformerRefinementWorkflow",
        task_queue="connector-calc",
        payload={},
        rationale="why the tests run it",
        requested_by="u-1",
        session_id="",
        correlation_id="corr-1",
    )
    asyncio.run(ConnectorJobWorkflow()._notify_failure(job, RuntimeError("the pod died")))

    assert sent, (
        "a job started by a Schedule or the inbox failed and told nobody: it has no session to "
        "push back to, so the outbound copy is the whole of what reaches its requester"
    )
    assert sent[0].kind == "job-result" and sent[0].recipient == "u-1"
    assert "the pod died" in sent[0].body
