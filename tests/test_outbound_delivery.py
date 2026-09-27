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
    """The callee's name, qualified or bare: `OutboundMessage` and `x.OutboundMessage` alike.

    Both spellings, because reading only `ast.Name` is how the first version of this scan went red
    on an ordinary qualified import while staying green on a deleted producer.
    `tests/test_activity_queue_bound.py::_dispatch_calls` records fixing exactly this in its own
    walk — "three ordinary spellings walked straight past it" — and this test was written after it
    and did not carry the lesson across.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _kind_of(call: ast.Call, constants: dict[str, str] | None = None) -> str | None:
    """The kind an `OutboundMessage(...)` actually sends — its literal `kind=`, or its default.

    A non-literal `kind=` is deliberately not collected — it would be a value derived from a
    payload, which is the arbitrary-file-write shape `Message.kind`'s `Literal` exists to refuse,
    and this scan must not quietly credit it as a producer.

    **An *omitted* `kind=` is collected, and its absence is how a wrong-kind producer hid from this
    whole file.** `durable/check_in.py` built its outbound copy with no `kind=` at all, so it sent
    `OutboundMessage`'s default `"digest"` into the digest's own outbox file — and this scan saw
    nothing, because it only ever looked at keywords that were present. Every producer that forgets
    the keyword is a producer of the default, so that is what it is credited with: the equality in
    `test_every_declared_delivery_kind_has_a_producer` then reads as "somebody sends this" rather
    than "somebody wrote this word", and the default is no longer a place to hide.

    The default is read off the model rather than transcribed, for `_declared_kinds`'s reason: a
    changed default that this file spelled out itself would agree with nothing.

    A `**kwargs` splat is refused (`None`), because a `kind` may be in it and this cannot see.
    Fail-closed is the same direction the two-hop rule fails in.

    `constants` are the module's own `NAME = "literal"` bindings, so `kind=CHECK_IN_KIND` resolves.
    A module constant is source-fixed — the same property `degraded()`'s subsystem rule demands —
    so it is not the payload-derived value the refusal above is about, and refusing it would push a
    producer into spelling its mailbox kind twice. `durable/check_in.py` needs exactly one spelling:
    the claim that a check-in is distinguishable from a digest is the claim that the mailbox kind
    and the channel kind are the same string.
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

    **Constructing an `OutboundMessage` is not producing a message, and the first version of this
    scan could not tell the difference.** It collected every `OutboundMessage(kind=...)` in `src/`
    and never asked whether anything sent it — so deleting the `await deliver_best_effort(...)` in
    `AwaitingWorkflow._push`, or binding the report's message to an unused local, left a declared
    kind that nothing on earth delivers *and the test green*. Both mutations were driven; both
    passed. That is the regression this file exists to prevent, reintroduced in the only form that
    matters.

    So the walk starts at the send site and works inwards, one hop:

    - `deliver_best_effort(OutboundMessage(kind="digest"))` — the argument is the construction.
    - `deliver_best_effort(_awaiting_message(...))` — the argument is a call to a function in the
      same module, whose body constructs it. One hop, because that is the shape the tree has; a
      second would want a call graph, and a scan that silently followed further would be claiming
      a guarantee it cannot check.

    A producer that hides behind two hops is therefore *not* counted, which fails closed: the kind
    reads as unproduced and the test goes red, rather than being credited on a chain nobody
    verified.
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
    """Every kind that reaches `deliver_best_effort`, by the *function* that sends it.

    One granularity finer than `_sent_kinds`, and the difference is a whole delivery path.
    `connector_job.py` sends `job-result` twice — from `_finish` when a job completes and from
    `_notify_failure` when it does not — so a module-level set is satisfied by either one alone.
    Driven: deleting the entire `deliver_best_effort` block from `_notify_failure` left
    `tests/test_connector_job_workflow.py` and this file at 28 passed. That block is the half that
    matters most, because `_notify_failure` returns early when there is no session, which is
    exactly the Schedule- or inbox-started run an outbound copy exists for.

    Same one-hop rule and same fail-closed behaviour as `_sent_kinds`: a producer hiding behind two
    calls reads as absent rather than being credited on a chain nobody checked.
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
    """Both outcomes travel, or the silent one reads as the good one.

    `_run_child`'s own comment makes the argument — "an outcome that says nothing is not neutral,
    it is an invitation to assume the good one" — and the `job-result` copy shipped on `_finish`
    alone, so a job that completed travelled and a job that failed did not.
    `test_every_declared_delivery_kind_has_a_producer` cannot see it and says so in the code: the
    declared↔produced equality is over *kinds*, and `Message.kind` has no failure value, so the
    success path satisfies it by itself.

    Named functions rather than a count, so moving the send out of `_notify_failure` into a helper
    that nothing calls is red rather than a shrug.
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
    """The guard's own blind spot, as an assertion — it is what let a wrong-kind producer ship.

    `_kind_of` collected the `kind=` keyword and nothing else, so a producer that simply **omitted**
    it sent `OutboundMessage`'s default and appeared in no scan in this file. That is exactly what
    `durable/check_in.py` did: it declared `CHECK_IN_KIND` for its mailbox, built its outbound copy
    with no `kind=`, and delivered every check-in as a `digest` —
    `test_every_declared_delivery_kind_has_a_producer` stayed green throughout, because "digest" had
    a producer either way and "work-check-in" was not yet declared.

    Driven on parsed source rather than on the tree, so it keeps failing whatever `src/` does next.
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
    """The whole finding, as an assertion: three of four kinds were a vocabulary with no caller.

    Stated as set equality in both directions. A declared kind nobody constructs is a capability
    the README claims and the tree does not have; a constructed kind the `Literal` does not declare
    cannot be built at all, so the second direction is what tells whoever adds one that the model is
    where to add it.
    """
    produced = _sent_kinds()
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

    **The payload is one level up, and the depth is the whole reason this test works.** It shipped
    with `"../../../etc/cron.d/escape"`, which under `tmp_path`
    (`/tmp/pytest-of-root/pytest-N/test_x0/outbox`) traverses to `/tmp/pytest-of-root/etc/cron.d` —
    a directory that does not exist, so `_write_atomically`'s `NamedTemporaryFile(dir=...)` raised,
    the registry counted a failure, and every assertion here passed *because the traversal missed*.
    Driven with the `Literal` deleted and a deeper tempdir, the same payload wrote
    `/etc/cron.d/escape-<hash>.md` and the seam reported `took=['local']` — a successful delivery.
    One level up always has a parent, so the escape succeeds whenever the bound is gone, and the
    assertions below then have something to catch.
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
    """A wait open to anyone entitled, or a job launched by a Schedule, has nobody to write to.

    Proved by calling the wrapper outside a workflow: `workflow.execute_activity` raises there, so
    reaching it at all is the failure. That makes the early return a real short-circuit rather than
    a value this test asserts about its own mock.
    """
    assert asyncio.run(deliver_best_effort(OutboundMessage(recipient="", subject="s"))) == []


def test_one_misspelled_channel_does_not_cost_the_healthy_ones_their_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`deliver`'s headline promise was true of a delivery failure and false of a typo.

    "A failing channel does not stop the others" is what that function's docstring leads with, and
    it held for a destination having a bad afternoon because the `build`/`deliver` calls are inside
    a per-channel `try`. `enabled()` ran *before* the loop and raised on an unresolvable name, so
    one mistyped fourth channel took every working one down with it — measured before this:
    `took == []` and zero files on a share that was perfectly fine.

    Still loud, and that is the other half: the bad name is reported through `degraded()`, which is
    alerted rather than skimmed, and `enabled()` keeps raising for `make channel-validate` and for
    startup, where refusing is right.
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
    """The idempotency key has to separate a re-run from a redelivery, and it did not.

    `job-result` is the one kind whose body carries no natural discriminator — `subject` is
    `f"{connector}:{job} finished"` and `body` is a summary derived from the inputs — so two
    genuinely distinct runs of one job for one chemist are byte-identical. Keyed on content alone
    they shared an id, which means a compliant webhook receiver drops the second real result by
    design and the share overwrites it. Both directions are asserted here because fixing one at the
    other's expense is the easy mistake: a key that separates re-runs must still collapse a retry,
    which is what `BAD_DATA_RETRY` makes at-least-once.
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
    """The one reader this message has is by construction not looking at the graph.

    It went out saying "recorded as `report-…`" to a chemist who closed the tab while the fan-out
    ran — a note id, openable only by somebody who can already reach the knowledge graph. Asserted
    against the workflow's source, because driving `DevelopmentReportWorkflow` needs a broker and
    what is claimed here is about the message it builds.

    The filename is asserted too, and it is the interesting half: `note_ref` is the *writer's*
    reference — a commit sha, or the unchanged tree — so naming the file after it would tell a
    chemist nothing and could carry characters `Attachment`'s pattern rejects, failing the whole
    message inside the activity rather than just the file.
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
    """`OutboundAttachment` is loose for the same reason `OutboundMessage` is.

    A workflow constructing an `Attachment` with a rejected filename raises `ValidationError` in
    *workflow* code, which no best-effort wrapper can catch — so the notice that must never fail
    the job becomes the thing that fails it. Driven: the loose model accepts what the strict one
    refuses, and the activity is where the refusal lands.
    """
    from pydantic import ValidationError

    from chemclaw.deliver.message import Attachment
    from chemclaw.durable.deliver_message import OutboundAttachment, OutboundMessage

    hostile = OutboundAttachment(filename="../../etc/x", content=b"x")
    with pytest.raises(ValidationError):
        Attachment(filename=hostile.filename, content=hostile.content)

    # ...and the activity swallows it. **With a channel actually enabled**, because the activity's
    # first line returns `[]` when delivery is off — asserting an empty list against the shipped
    # default would pass whether or not the refusal exists, which is the vacuous-guard shape this
    # file was rewritten once to remove.
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
    """Driven through `_notify_failure`, because a call in a function is not a call on a path.

    `_sending_functions` above resolves a `deliver_best_effort` call to the function that holds it
    and says nothing about whether that function reaches it. Driven: putting the outbound copy back
    inside `if job.session_id:` — the exact defect the failure-path guard's own docstring names,
    "`_notify_failure` returns early when there is no session, which is exactly the Schedule- or
    inbox-started run an outbound copy exists for" — left `tests/test_outbound_delivery.py` and
    `tests/test_connector_job_workflow.py` at 29 passed. That is cause (g) in `tasks/lessons.md`,
    existence standing in for reachability, in the guard written to close a reachability defect.

    So the arm that matters is the sessionless one: a Schedule- or inbox-started run has no session
    to fall back on, and the outbound copy is the only thing that tells anybody.
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
