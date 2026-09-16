"""The live probe harness, and the shipped probe corpus as a declaration against the live surface.

Two kinds of test here, and the second is the one that earns its keep. The first exercises the
runner's own logic — folding an event stream into an outcome, catching a duplicate id, telling a
grounded citation from an invented one. The second gates `data/evals/probes/` the way
`skill-validate` and `template-validate` gate their declarations: a probe that expects a tool the
agent cannot resolve is a probe that can never pass, and it would show up in a run as a defect in
the *system* rather than a typo in the corpus.

The runner is driven through `httpx.MockTransport` rather than a live server. That is deliberate
and it is not a mock of the thing under test: the SSE bytes are the real contract, and feeding
exact wire frames is what lets a test assert that a `tool_failed` frame with no `answer` frame
produces `answered=False, failed_loudly=True` — the silent-death signal that no amount of scripted
agent testing could reach.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import SecretStr

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.cli import live_probes
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.evals.live import ProbeOutcome, _score_citations, load_probes, run_probe
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.kg.note import mentioned_ids
from tests.test_probe_coverage import fleet_expected_tools

PROBE_DIR = Path(__file__).resolve().parent.parent / "data" / "evals" / "probes"


def _fake_job_outcomes(states: dict[str, str]) -> object:
    """A stand-in for the Temporal lookup that returns a fixed verdict per workflow id."""

    async def _lookup(job_ids: list[str]) -> dict[str, str]:
        return {job_id: states[job_id] for job_id in job_ids}

    return _lookup


def _probe(**overrides: object) -> Probe:
    """A minimal valid probe; overrides name only what a case actually varies."""
    payload: dict[str, object] = {
        "id": "t-01",
        "section": 1,
        "persona": "lab_technician",
        "bucket": "A",
        "question": "what happened last time",
        "direction": "cites a note",
    }
    payload.update(overrides)
    return Probe.model_validate(payload)


SSE_HEADERS = {"content-type": "text/event-stream"}
"""What the front door actually answers a turn with, and what the client refuses without.

`sse_starlette` sets it on every stream, and `decoded_events` checks it before decoding a byte: a
JSON error body served at 200 is not a stream, and a harness that scanned it for `data:` prefixes
would find none and report a broken front door as a turn that said nothing. It is still a turn that
emitted nothing — see
`test_a_response_that_is_not_an_event_stream_yields_nothing_rather_than_raising` for why the reader
names it in the log rather than raising — but a fixture that omits this header is asserting against
a response the service cannot produce.
"""


def _sse(*events: dict[str, object]) -> bytes:
    """Exactly the wire shape the front door emits: one `data:` line per event."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


#: One turn's stream written in the parts of the SSE grammar the hand-written readers did not have.
#:
#: Every line here is legal and three of the four shapes were unreadable before `httpx_sse`:
#: sse-starlette's keepalive **comment**, the `event:` name `api/events.sse_frame` sets on every
#: real frame and no fixture in this suite used to send, `id:`/`retry:` fields, and a `data:` field
#: **split over two lines**, which the grammar says is joined with a newline before it is parsed.
#: The three old readers each took one `data:` line as a whole payload, so the split frame decoded
#: as two JSONDecodeErrors and the event simply disappeared — an answer the harness would have
#: recorded as the system going silent.
#:
#: **The split frame is latent, not live**, and the distinction is the point of writing it down:
#: `sse_starlette` serialises with `model_dump_json()`, which emits no raw newline, so this system
#: has never sent one. It is here because the reader's job is the wire format rather than this
#: server's current habits, and because a harness that misreads a legal frame reports the *system*
#: as broken.
#:
#: Defined once and read by `tests/test_live_storm.py` and `tests/test_live_benchmark.py` as well,
#: because "the three call sites agree" is the claim, and three copies of the fixture would be
#: three chances for them to stop agreeing.
AWKWARD_STREAM = (
    b": ping - 2026-09-16T00:00:00+00:00\n\n"
    b"event: tool_call\n"
    b'data: {"type": "tool_call", "tool": "gather_evidence", "arguments": "{}"}\n\n'
    b"event: answer\n"
    b"id: 7\n"
    b"retry: 3000\n"
    b'data: {"type": "answer",\n'
    b'data:  "text": "the corpus says ethanol."}\n\n'
)

#: A turn whose stream stops after the answer's `data:` line, with no blank line to terminate it.
#:
#: This is not an exotic frame — it is what every *interrupted* turn looks like on the wire, and
#: `cli/live_storm` is a chaos harness whose whole subject is producing them: a cancelled turn, a
#: worker killed mid-answer, a connection cut by a proxy. The SSE grammar dispatches an event on
#: the blank line that follows it, so a reader that only dispatches there drops the last frame of
#: every such stream — the frame *nearest the fault the storm was run to observe*.
#:
#: Read by `tests/test_live_storm.py` as well, for the same reason `AWKWARD_STREAM` is: the claim
#: is that the three call sites share one reader, and three copies of a fixture are three chances
#: for that to stop being true.
TRUNCATED_STREAM = (
    b'data: {"type": "token", "text": "the corpus "}\n\n'
    b'data: {"type": "answer", "text": "says ethanol."}\n'
)


def _transport(*events: dict[str, object]) -> httpx.MockTransport:
    """A front door that opens a session and then streams `events` for the turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=_sse(*events), headers=SSE_HEADERS)

    return httpx.MockTransport(handler)


def _run(probe: Probe, *events: dict[str, object]) -> ProbeOutcome:
    """Drive one probe against a scripted event stream."""

    async def go() -> ProbeOutcome:
        async with httpx.AsyncClient(
            transport=_transport(*events), base_url="http://front-door"
        ) as client:
            return await run_probe(client, probe)

    return asyncio.run(go())


def _result_event(tool: str, text: str) -> dict[str, object]:
    """A `tool_result` frame shaped exactly as `api.runner_trace` builds one from a full result.

    The point of going through the real derivation rather than hand-writing the fields is that the
    truncation is *in* the fixture: `preview` is cut at the wire budget while `numbers` is not, so
    a test can show the two answering differently about the same result.
    """
    from chemclaw.core.quantities import returned_values

    return {
        "type": "tool_result",
        "tool": tool,
        "preview": text[:200],
        "note_ids": [],
        "numbers": returned_values(text),
    }


def test_tool_call_arguments_and_answer_are_recorded() -> None:
    """The happy path: a tool call, its result, and an answer all reach the outcome."""
    outcome = _run(
        _probe(expects_tools=["screen_hazards"]),
        {"type": "tool_call", "tool": "screen_hazards", "arguments": '{"smiles": ["CCO"]}'},
        {"type": "tool_result", "tool": "screen_hazards", "preview": "no rule matched"},
        {"type": "answer", "text": "Nothing in the rule table matched."},
    )
    assert outcome.tools_called == ["screen_hazards"]
    assert outcome.answered is True
    assert outcome.expected_tools_met is True
    assert outcome.failed_loudly is False


def test_a_turn_that_dies_without_an_error_is_recorded_as_a_silent_failure() -> None:
    """No answer and no error is the defect class a passing test suite cannot see.

    `failed_loudly` must stay False here. If a future change made any unanswered turn count as
    loud, the run would report a system that broke visibly when it did not, and the one signal
    worth having would be gone.
    """
    outcome = _run(_probe(), {"type": "tool_call", "tool": "gather_evidence", "arguments": "{}"})
    assert outcome.answered is False
    assert outcome.failed_loudly is False


def test_a_failed_tool_is_loud_even_when_the_turn_still_answers() -> None:
    """A tool can fail and the answer still be good — the failure must remain visible."""
    outcome = _run(
        _probe(),
        {"type": "tool_failed", "tool": "compute_reaction_energy", "message": "worker unreachable"},
        {"type": "answer", "text": "The calculation could not be started."},
    )
    assert outcome.tools_failed == ["compute_reaction_energy"]
    assert outcome.answered is True
    assert outcome.failed_loudly is True


def test_expected_tools_is_any_of_not_all_of() -> None:
    """One of several acceptable tools is a pass; demanding all would grade routing taste."""
    outcome = _run(
        _probe(expects_tools=["find_notes", "gather_evidence"]),
        {"type": "tool_call", "tool": "gather_evidence", "arguments": "{}"},
        {"type": "answer", "text": "ok"},
    )
    assert outcome.expected_tools_met is True


def test_a_legal_frame_the_old_readers_could_not_parse_is_read() -> None:
    """The grammar, not the habit: a split `data:`, a comment, an `event:`, an `id:` and a `retry:`.

    Driven through `run_probe` rather than through the decoder alone, because what broke before was
    a whole event vanishing from an outcome rather than a function returning the wrong thing: the
    `tool_call` has to reach `tools_called` and the split `answer` has to reach `answer`, out of
    one stream that also contains a keepalive comment nothing may turn into an event.

    Every assertion here failed before `httpx_sse` — the split frame decoded as two parse errors
    and was dropped, so this probe recorded an unanswered turn, which is the silent-death signal
    this harness exists to report. `AWKWARD_STREAM` says why that is latent against this system's
    own server and why the test is worth having anyway.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=AWKWARD_STREAM, headers=SSE_HEADERS)

    async def go() -> ProbeOutcome:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await run_probe(client, _probe())

    outcome = asyncio.run(go())
    assert outcome.tools_called == ["gather_evidence"]
    assert outcome.answer == "the corpus says ethanol."
    assert outcome.transport_error is None
    # The keepalive is a comment, which carries no fields — it must not arrive as an event at all,
    # or every idle second of a long turn would show up in the counts as something that happened.
    assert outcome.event_counts == {"tool_call": 1, "answer": 1}


def _run_bytes(body: bytes, headers: dict[str, str]) -> ProbeOutcome:
    """Drive one probe against exact wire bytes rather than against `_sse`'s well-formed frames."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=body, headers=headers)

    async def go() -> ProbeOutcome:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await run_probe(client, _probe())

    return asyncio.run(go())


def test_the_final_event_of_a_stream_that_ends_without_a_blank_line_still_arrives() -> None:
    """A truncated stream keeps its last frame, which is the one worth having.

    The SSE grammar dispatches an event when the decoder sees a **blank line**, so a driver that
    does nothing at end-of-stream silently drops the final frame of every stream that is cut off
    — and a cut-off stream is exactly what `cli/live_storm` exists to produce. Measured on this
    fixture, `EventSource.aiter_sse` yielded **1** event where all three hand-written readers it
    replaced yielded **2**; `decoded_events` supplies the blank line the stream owed it.

    Asserted through `run_probe` rather than against the decoder alone, because what a dropped
    frame costs is an *outcome*: the answer vanishes, `answered` goes False, and the probe books a
    silent death against the system under test. That is the signal this whole harness exists to
    report, so a decoder defect and the defect it reports are one character apart.
    """
    outcome = _run_bytes(TRUNCATED_STREAM, SSE_HEADERS)
    assert outcome.answer == "says ethanol."
    assert outcome.answered is True
    assert outcome.event_counts == {"token": 1, "answer": 1}
    assert outcome.transport_error is None


def test_a_response_that_is_not_an_event_stream_yields_nothing_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 carrying JSON is an empty turn and a named warning, never an exception.

    A proxy in front of the front door answering an error as JSON at 200 is the realistic way to
    get one. `httpx_sse` refuses that content type by raising `SSEError` from inside the iterator,
    which sounds stricter and is worse placed: whether the misconfiguration is *recorded* or
    *fatal* then depends on which caller happens to hold a handler. `run_probe` holds one, so this
    probe would be recorded — but `cli/live_benchmark._ask` holds none, and there the same response
    ends the whole benchmark run with every already-answered question collected and lost.

    So the turn reads as the turn that emitted nothing, which is what it was, and the finding the
    old assertion was written for is kept where it costs nobody a run: the log names the content
    type that arrived. A `transport_error` here would be a claim about the *network*, which was
    fine.
    """
    with caplog.at_level(logging.WARNING, logger="chemclaw.evals.live"):
        outcome = _run_bytes(
            b'{"detail": "a proxy answered instead"}', {"content-type": "application/json"}
        )
    assert outcome.transport_error is None
    assert outcome.answered is False
    assert outcome.event_counts == {}
    assert "application/json" in caplog.text
    assert "text/event-stream" in caplog.text


def test_no_expected_tools_leaves_the_check_unscored_rather_than_failed() -> None:
    """A probe that names no tool skips the check; `None` must not read as a miss."""
    outcome = _run(_probe(), {"type": "answer", "text": "ok"})
    assert outcome.expected_tools_met is None


def test_a_transport_failure_is_recorded_not_raised() -> None:
    """One dead turn must not cost the other 189 results."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("front door refused the connection")

    async def go() -> ProbeOutcome:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await run_probe(client, _probe())

    outcome = asyncio.run(go())
    assert outcome.transport_error is not None
    assert outcome.answered is False


def test_a_citation_counts_only_when_a_tool_result_actually_returned_it() -> None:
    """The grounding check, in both directions.

    The negative case is the point: an id the model produced from memory must be flagged even
    though the note genuinely exists in the corpus, because the question is whether *this turn*
    saw it. A check that re-retrieved instead would pass the invented citation.
    """
    returned = {"rxn-suzuki-biaryl"}
    assert _score_citations("see [[rxn-suzuki-biaryl]]", returned) == []
    assert _score_citations("see [[evidence-for:rxn-suzuki-biaryl]]", returned) == []
    assert _score_citations("see [[rxn-never-retrieved]]", returned) == ["rxn-never-retrieved"]


def _notes_event(tool: str, note_ids: list[str]) -> dict[str, object]:
    """A `tool_result` frame that returned these note ids, which is what a gold set grades."""
    return {"type": "tool_result", "tool": tool, "preview": "", "note_ids": note_ids, "numbers": []}


def test_the_gold_set_scores_what_retrieval_returned_and_not_what_the_answer_cited() -> None:
    """The recall arithmetic, and the choice of denominator, neither of which had a test.

    `expects_notes` grades **retrieval**: `expected & returned_ids` over `expected`. Scoring the
    answer's citations instead would fold two different failures into one number — a turn handed
    the right note and failing to cite it is a citation defect, and `uncited_note_ids` is where it
    belongs. The fixture makes the two disagree on purpose: the answer cites one note it was never
    given and omits one it was, so a scorer reading citations would produce 0.5 with a different
    numerator and a different denominator.
    """
    outcome = _run(
        _probe(expects_notes=["opt-a", "opt-b", "opt-c"]),
        _notes_event("gather_evidence", ["opt-a", "opt-b", "unexpected-d"]),
        {"type": "answer", "text": "see [[opt-a]] and [[never-returned]]"},
    )

    assert outcome.expected_notes_recall == pytest.approx(2 / 3)
    assert outcome.expected_notes_missing == ["opt-c"]
    # The two axes stay apart: retrieval got 2 of 3, and the answer invented a citation.
    assert outcome.uncited_note_ids == ["never-returned"]


def test_a_probe_expecting_notes_that_got_none_scores_zero_rather_than_nothing() -> None:
    """`0.0` and `None` are different findings, and one line of the report depends on it.

    `cli/live_probes` filters on `expected_notes_recall is not None` to decide which probes are in
    the gold-set mean, then reads `o.expected_notes_recall or 0.0` — so a real zero that arrived as
    `None` would leave the failing probe out of its own denominator and raise the reported mean.
    The distinction is the same one `expected_tools_met` already keeps.
    """
    missed = _run(
        _probe(expects_notes=["opt-a"]),
        _notes_event("gather_evidence", ["something-else"]),
        {"type": "answer", "text": "nothing relevant came back"},
    )
    ungraded = _run(_probe(), {"type": "answer", "text": "nothing was expected"})

    assert missed.expected_notes_recall == 0.0
    assert missed.expected_notes_missing == ["opt-a"]
    assert ungraded.expected_notes_recall is None, (
        "a probe declaring no expected notes must stay out of the gold-set mean entirely"
    )
    assert ungraded.expected_notes_missing == []


def test_the_report_counts_a_zero_scoring_probe_in_the_gold_set_mean() -> None:
    """The reporting half, which is where the `None`-versus-`0.0` distinction is spent.

    Driven through the real `_summary` rather than re-deriving the arithmetic: one probe at 1.0,
    one at 0.0 and one that declares no notes must read as a mean of **0.50 over 2 probes**. A
    reader of that line is asking "how much of what the questions are about did retrieval reach",
    and a mean that silently dropped its failures would answer 1.00.
    """
    probes = [_probe(id=f"t-0{i}") for i in (1, 2, 3)]

    def _outcome(probe: Probe, **scored: object) -> ProbeOutcome:
        """One outcome for `probe`, carrying only the gold-set fields a case varies."""
        return ProbeOutcome(
            probe_id=probe.id,
            section=probe.section,
            persona=probe.persona,
            bucket=probe.bucket,
            question=probe.question,
            **scored,  # type: ignore[arg-type]
        )

    outcomes = [
        _outcome(probes[0], expected_notes_recall=1.0),
        _outcome(probes[1], expected_notes_recall=0.0, expected_notes_missing=["opt-a"]),
        _outcome(probes[2]),
    ]

    report = live_probes._summary(probes, outcomes, [], "provenance: a test")

    assert "mean recall over 2 probes) | 0.50" in report
    assert "probes missing at least one expected note | 1" in report


def test_a_citation_past_the_preview_budget_is_still_grounded() -> None:
    """The defect that made the metric unusable: 40 retrieved chunks scored against 200 characters.

    Built so a substring scan over previews gives the wrong answer and nothing else does. Only the
    first id fits inside the preview budget, so the old form reported the other 39 as ungrounded —
    which is how a live run graded 19 of 36 answers as fabrication with nine of nine checked
    verdicts false.
    """
    ids = [f"reaction-bh-amination-btmg-{n:04d}" for n in range(40)]
    result = "".join(
        f'<retrieved-note-abc id="{note_id}">\nsome body text\n</retrieved-note>\n'
        for note_id in ids
    )
    answer = " ".join(f"[[{note_id}]]" for note_id in ids)

    returned = set(mentioned_ids(result))
    assert _score_citations(answer, returned) == []

    # The old shape, kept as the contrast rather than described: scoring against a preview-sized
    # window would have called the overwhelming majority of these citations ungrounded.
    preview_grounded = [note_id for note_id in ids if note_id in result[:200]]
    assert len(preview_grounded) < 5
    assert len(ids) - len(preview_grounded) >= 35


def test_the_figures_a_live_judge_called_invented_are_verified_against_the_real_tool_result() -> (
    None
):
    """gr-26, rebuilt from the real tool result and the real answer: the six PDEs are quotations.

    The tool result is `ich_impurity_limit`'s own output, recorded rather than hand-written —
    `tests/recorded_tool_results.py` says why it is a recording now that the ICH tables are
    `Chemclaw3-mcp`'s. What is under test is the citation scorer, not the guideline.

    This is the defect that survived the `note_ids` fix. On the re-run with untruncated ids in
    place the judge still wrote "the answer invents specific PDE numbers (Pd: 100/10/1 µg/day; Cu:
    3000/300/30 µg/day)… the tool results shown are truncated previews that do not display the
    numerical limits" — and it was right about the previews, which is why the assertion below on
    where character 200 falls is part of the test rather than a comment.
    """
    from tests.recorded_tool_results import RECORDED_ICH_LIMITS

    results = [RECORDED_ICH_LIMITS[name] for name in ("palladium", "copper")]
    answer = (
        "## **Palladium** — *Class 2B*\n"
        "- **Oral:** 100 µg/day\n- **Parenteral:** 10 µg/day\n- **Inhalation:** 1 µg/day\n"
        "## **Copper** — *Class 3*\n"
        "- **Oral:** 3000 µg/day\n- **Parenteral:** 300 µg/day\n- **Inhalation:** 30 µg/day\n"
    )
    outcome = _run(
        _probe(),
        _result_event("ich_impurity_limit", results[0]),
        _result_event("ich_impurity_limit", results[1]),
        {"type": "answer", "text": answer},
    )
    # "3" is the class, which the copper result states in prose ("Class 3 — relatively low oral
    # toxicity"). It belongs on the list for the same reason the PDEs do: a tool returned it.
    assert outcome.verified_numbers == ["100", "10", "1", "3", "3000", "300", "30"]

    # Why the event needed a second field at all: not one of those figures is inside the preview
    # the browser gets, so a check reading `preview` reports every one of them as unsupported.
    assert not any(figure in results[0][:200] for figure in ("100.0", "10.0", "1.0"))


def test_a_figure_no_tool_returned_is_simply_not_on_the_verified_list() -> None:
    """The whitelist's boundary: it vouches for what it saw and stays silent about the rest.

    Deliberately *not* the inverse of `uncited_note_ids`. A citation has a syntax that can only
    come from retrieval; a number has none — an answer legitimately subtracts two values it was
    given, totals a column or quotes a textbook constant — so "no tool returned this" was measured
    on gr-18 and gr-29 and produced eleven flags and zero fabrications (`_verified_numbers`). The
    harness therefore asserts membership and never absence, and this pins that: the unsupported
    figure is missing from the list, not reported by it.
    """
    text = '{"limits": [{"basis": "oral PDE", "value": 100.0, "unit": "\\u00b5g/day"}]}'
    outcome = _run(
        _probe(),
        _result_event("ich_impurity_limit", text),
        {"type": "answer", "text": "The oral PDE is 100 µg/day; parenteral is 250 µg/day."},
    )
    assert outcome.verified_numbers == ["100"]


def test_an_unreadable_figure_costs_that_figure_and_not_the_turn() -> None:
    """A value the harness cannot read is an observation, never a network failure.

    `float(value) for value in event.get("numbers", [])` sat inside the stream loop, and that
    loop's `except` catches `ValueError`. So one non-numeric entry raised out of the `async for`:
    every later event was dropped — the answer with them — and the turn was stamped
    `transport_error="ValueError: could not convert string to float: 'n/a'"`, which
    `cli/live_probes.py` then lists under "failed silently". A defect in the system under test,
    filed as the network between us and it, on a turn that in fact answered.
    """
    with pytest.raises(ValueError):
        float("n/a")  # the entry below really is one this harness cannot read

    outcome = _run(
        _probe(),
        {
            "type": "tool_result",
            "tool": "compute_reaction_energy",
            "preview": "-12.5 kcal/mol",
            "numbers": [-12.5, "n/a", 3.0],
        },
        {"type": "answer", "text": "The energy is -12.5 kcal/mol with a 3.0 kcal/mol barrier."},
    )
    assert outcome.transport_error is None
    assert outcome.answered is True
    # The readable figures are still what the answer is checked against; only the bad one is lost.
    assert outcome.verified_numbers == ["-12.5", "3.0"]


def test_a_hyphen_suffixed_id_does_not_ground_its_prefix() -> None:
    """Set membership closed a hole the substring scan had, and this pins it closed.

    `playbook-degassing-old` containing `playbook-degassing` made the retired note ground a
    citation of the live one — both are in the committed corpus, so this was reachable.
    """
    returned = set(mentioned_ids('{"id": "playbook-degassing-old"}'))
    assert _score_citations("see [[playbook-degassing]]", returned) == ["playbook-degassing"]


def test_duplicate_probe_ids_across_files_are_fatal(tmp_path: Path) -> None:
    """Two probes sharing an id would overwrite one transcript and overstate coverage."""
    one = {"probes": [_probe(id="dup-01").model_dump()]}
    two = {"probes": [_probe(id="dup-01", question="different").model_dump()]}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(one), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(two), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate probe id"):
        load_probes(str(tmp_path))


def test_an_unknown_key_in_a_probe_file_is_rejected(tmp_path: Path) -> None:
    """`extra="forbid"`: a misspelled field must fail loudly, not be silently dropped."""
    payload = {"probes": [{**_probe().model_dump(), "expects_tool": ["find_notes"]}]}
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_probes(str(tmp_path))


def test_shipped_probes_load_and_cover_every_user_story_section() -> None:
    """The corpus is a declaration: it claims to cover all seventeen sections, so check it."""
    probes = load_probes(str(PROBE_DIR))
    assert len(probes) >= 150
    assert {p.section for p in probes} == set(range(1, 18))
    assert {p.bucket for p in probes} == {"A", "B", "C"}


def test_every_expected_tool_in_the_shipped_corpus_exists_on_the_agent_surface() -> None:
    """A probe expecting a tool the agent cannot resolve can never pass.

    The same declaration-versus-surface check `skill-validate` and `template-validate` already
    apply, for the same reason: without it a typo in the corpus reports as a defect in the system.

    **The fleet exemption is imported rather than restated**, from the file whose whole subject is
    this check in both directions. This assertion and
    `tests/test_probe_coverage.py::test_no_probe_expects_a_tool_that_does_not_exist` are one
    invariant written twice, and the second copy had already drifted into being the weaker: it
    reads `load_probes`, which does not recurse, so it covers 336 probes where the other covers 338
    (the `m12/` suites are outside it). Keeping the *exemption* in one place is what stops that gap
    widening into a disagreement — a probe naming a tool `Chemclaw3-mcp` serves is legitimate under
    `needs_bundle:`, and a rule about it that lives in two files will shortly mean two things.
    """
    surface = available_tool_names()
    unknown = {t for p in load_probes(str(PROBE_DIR)) for t in p.expects_tools if t not in surface}
    assert unknown - fleet_expected_tools() == set(), (
        f"probes expect tools that do not exist: {sorted(unknown - fleet_expected_tools())}"
    )


def test_a_bucket_c_probe_expects_no_tool() -> None:
    """Bucket C means nothing backs the ask, so naming a tool for it is a mis-bucketed probe."""
    offenders = [p.id for p in load_probes(str(PROBE_DIR)) if p.bucket == "C" and p.expects_tools]
    assert offenders == [], f"bucket-C probes naming tools: {offenders}"


def test_every_shipped_probe_names_at_least_one_forbidden_claim() -> None:
    """`forbids_claims` is how fabrication is caught; a probe without one cannot catch it."""
    probes = load_probes(str(PROBE_DIR))
    assert [p.id for p in probes if not p.forbids_claims] == []


def test_probe_files_carry_nothing_but_probes() -> None:
    """A stray top-level key would be silently ignored by a looser reader."""
    for path in sorted(PROBE_DIR.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(payload) == {"probes"}, f"{path.name} has unexpected top-level keys"
        ProbeSet.model_validate(payload)


def test_a_run_that_graded_nothing_writes_no_grades_file(tmp_path: Path) -> None:
    """`--no-judge` must not replace real verdicts with an empty list.

    It did: the outputs were written to the transcript directory's *parent*, so a six-probe
    `--no-judge` run overwrote a 190-probe run's `grades.json` with `[]`, and the file survived
    only because it had been committed. An empty grades file is indistinguishable from a run in
    which every single answer failed.
    """
    from chemclaw.cli.live_probes import _write_outputs

    _write_outputs(tmp_path / "transcripts", "# report\n", [])
    assert (tmp_path / "transcripts" / "summary.md").exists()
    assert not (tmp_path / "transcripts" / "grades.json").exists()


def test_outputs_land_beside_their_own_transcripts(tmp_path: Path) -> None:
    """Two runs into different transcript directories must not overwrite each other."""
    from chemclaw.cli.live_probes import _write_outputs
    from chemclaw.evals.live_judge import Judgement

    one = Judgement(probe_id="a-01", verdict="served")
    _write_outputs(tmp_path / "before", "# before\n", [one])
    _write_outputs(tmp_path / "after", "# after\n", [one])

    assert (tmp_path / "before" / "summary.md").read_text(encoding="utf-8") == "# before\n"
    assert (tmp_path / "after" / "summary.md").read_text(encoding="utf-8") == "# after\n"
    # The shared parent must hold neither, which is what made the collision possible.
    assert not (tmp_path / "summary.md").exists()
    assert not (tmp_path / "grades.json").exists()


def test_a_probe_expecting_a_job_resolves_its_workflow_against_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`expects_job` is what turns "the turn said it started a job" into an observation.

    The stream below is a *truthful* one: the turn really did start a workflow and really does say
    so. That is exactly the case the event stream alone cannot grade, because a job tool returns an
    id the moment the launch is accepted — so an answer can be honest about starting work the
    broker never ran. The outcome must therefore carry what Temporal says, not what the turn said.
    """
    monkeypatch.setattr(
        "chemclaw.evals.live._job_outcomes",
        _fake_job_outcomes({"calc-compute_reaction_energy-abc": "FAILED"}),
    )
    outcome = _run(
        _probe(expects_tools=["compute_reaction_energy"], expects_job=True),
        {"type": "tool_call", "tool": "compute_reaction_energy", "arguments": "{}"},
        {"type": "job_started", "job_id": "calc-compute_reaction_energy-abc"},
        {"type": "answer", "text": "Started it — I'll have the energy shortly."},
    )
    assert outcome.jobs_started == ["calc-compute_reaction_energy-abc"]
    assert outcome.job_outcomes == {"calc-compute_reaction_energy-abc": "FAILED"}


def test_a_probe_not_expecting_a_job_never_asks_the_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup is opt-in, so an ordinary probe run costs no Temporal round trip.

    Not merely an efficiency point: `_job_outcomes` records `unreachable` when it cannot connect,
    and running it for every probe would put that string on 200-odd outcomes of a run that never
    cared about durable work — noise indistinguishable from a finding.
    """
    called = False

    async def _boom(job_ids: list[str]) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("chemclaw.evals.live._job_outcomes", _boom)
    outcome = _run(
        _probe(expects_tools=["gather_evidence"]),
        {"type": "job_started", "job_id": "calc-x"},
        {"type": "answer", "text": "done"},
    )
    assert outcome.jobs_started == ["calc-x"]
    assert outcome.job_outcomes == {}
    assert called is False


def test_an_unreachable_broker_is_recorded_as_such_rather_than_as_a_dead_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure to reach the broker and a failed job are different findings.

    Collapsing them would make every probe run without a broker report fabricated durable work,
    which is the mirror image of the defect this signal exists to catch.
    """

    async def _unreachable() -> object:
        raise SubsystemUnavailableError("the durable execution backend is unreachable")

    monkeypatch.setattr("chemclaw.evals.live.temporal_connect", _unreachable)
    outcome = _run(
        _probe(expects_job=True),
        {"type": "job_started", "job_id": "calc-y"},
        {"type": "answer", "text": "started"},
    )
    assert outcome.job_outcomes == {"calc-y": "unreachable"}


def test_every_durable_probe_declares_the_job_expectation_it_is_named_for() -> None:
    """The `du-*` corpus exists to exercise the durable path, so it must ask to be checked.

    A `du-` probe with `expects_job` left off would run, pass and prove nothing about Temporal —
    the precise shape of the gap the file was written to close.
    """
    durable = [p for p in load_probes(str(PROBE_DIR)) if p.id.startswith("du-")]
    assert durable, "no durable probes found — this test would assert nothing"
    # Two exceptions, both of the same kind: they ask about the *record* of durable work, and
    # starting a workflow to answer either would be the wrong instinct. du-04 asks what past jobs
    # ran; du-10 asks what this system is still waiting on, which `check_pending_requests` answers
    # from the `pending_requests` projection without touching the broker. Naming the exemptions
    # rather than enumerating the expectant probes is what keeps an exception a named one rather
    # than a habit, while letting the corpus grow without editing a list here.
    exempt = {"du-04", "du-10"}
    silent = sorted(p.id for p in durable if not p.expects_job and p.id not in exempt)
    assert not silent, (
        f"durable probe(s) {silent} do not declare `expects_job` — they would run, pass and prove "
        "nothing about Temporal. Set it, or add the id to `exempt` above with the reason."
    )
    assert exempt <= {p.id for p in durable}


def test_no_probe_direction_asserts_which_deployment_it_meets() -> None:
    """A grading key that names one configuration stops being true when the stack changes.

    Six directions across three files asserted "Temporal is not running in this test". They were
    honest when written and became wrong the day `make live-up` started the workers: a *successful*
    launch would have been graded a failure. Directions describe behaviour; the environment is the
    runner's business.
    """
    offenders: list[str] = []
    for probe in load_probes(str(PROBE_DIR)):
        text = probe.direction.lower()
        if "is not running" in text or "not reachable in this run" in text:
            offenders.append(probe.id)
    assert not offenders, f"probe directions asserting the deployment they meet: {offenders}"


def test_a_job_that_finished_inside_the_turn_is_not_reported_as_no_job_at_all() -> None:
    """The first thing the durable signal got wrong, live.

    A job answering inside `inline_wait_seconds` is deliberately never announced — `connectors/jobs`
    returns the result instead of an id, because an already-finished run would never emit the
    matching `job_completed` and the surface would draw a row that stays "running" forever. So
    `jobs_started` is legitimately empty for a job that ran end to end.

    Scoring "started none" off that emptiness reported du-01 as a miss while Temporal held
    `calc-compute_reaction_energy-4cf212292f8f8e4e` in COMPLETED. A signal that flags a working
    path is worse than no signal: it spends the reader's attention on the thing that was fine.
    """
    from chemclaw.cli.live_probes import _summary

    probe = _probe(id="du-01", expects_tools=["compute_reaction_energy"], expects_job=True)
    outcome = ProbeOutcome(
        probe_id="du-01",
        section=1,
        persona="lab_technician",
        bucket="A",
        question=probe.question,
        answer="ΔG is -32.6 kcal/mol.",
        answered=True,
        tools_called=["compute_reaction_energy"],
    )

    report = _summary([probe], [outcome], [], "a test")

    assert "ran none" not in report, "an inline-completed durable job was reported as no job at all"
    assert "finished inside the turn" in report, "the inline case must still be visible, not hidden"


def test_a_probe_that_needed_a_job_and_called_no_job_tool_is_still_flagged() -> None:
    """The other direction: correcting the false positive must not blind the real miss.

    du-03 called nineteen retrieval tools and never reached `start_optimization_campaign`. That is
    the finding the signal exists for, and it has to survive the fix for the inline case.
    """
    from chemclaw.cli.live_probes import _summary

    probe = _probe(id="du-03", expects_tools=["start_optimization_campaign"], expects_job=True)
    outcome = ProbeOutcome(
        probe_id="du-03",
        section=1,
        persona="lab_leader",
        bucket="A",
        question=probe.question,
        answered=False,
        tools_called=["find_notes", "gather_evidence", "find_past_jobs"],
    )

    report = _summary([probe], [outcome], [], "a test")

    assert "du-03" in report
    assert "ran none" in report


def test_an_announced_outage_does_not_hide_a_silent_death() -> None:
    """The harness's most important signal, and it could not fire on any broker-less deployment.

    `capability_degraded` is announced *before the turn runs anything* — it names what this turn
    will not have, which is the system working. Once the runner began probing Temporal per turn,
    every deployment without a broker announced `durable-jobs (Temporal)` on every single turn, so
    `failed_loudly` was true everywhere and "answered nothing, said nothing went wrong" became
    unobservable. A run would report zero silent failures and mean it as a fact about the harness.

    The stream here is exactly that shape: an outage announced, and then nothing at all. That is
    the silent death this signal exists to find, not an exception to it.
    """
    outcome = _run(
        _probe(),
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
    )

    assert outcome.degraded == ["durable-jobs (Temporal)"], "the announcement is still recorded"
    assert not outcome.answered
    assert not outcome.failed_loudly, (
        "a pre-turn capability announcement is not this turn's work failing; counting it as one "
        "is what made every broker-less run report a clean zero"
    )


def test_a_turn_whose_tool_failed_is_still_loud_beside_an_outage() -> None:
    """The other direction, so the fix above cannot have simply turned the signal off.

    Same announced outage, plus a tool that actually fell over. `failed_loudly` must still be true:
    what changed is which events count as a failure, not whether any do.
    """
    outcome = _run(
        _probe(),
        {"type": "capability_degraded", "connectors": ["durable-jobs (Temporal)"]},
        {"type": "tool_failed", "tool": "predict_pka", "message": "connection refused"},
        {"type": "answer", "text": "I could not compute that."},
    )

    assert outcome.tools_failed == ["predict_pka"]
    assert outcome.failed_loudly


def test_the_harness_makes_no_token_cost_claim_it_cannot_take() -> None:
    """An absence pinned, so re-adding the claim without a caller turns this red.

    `ProbeOutcome.tokens` was the only consumer of `session_tokens`, and `session_tokens` had no
    caller anywhere — not even a test. So every probe of every live run recorded `tokens=None`,
    which the field's own comment defined as "the ledger could not be asked", while two long
    docstrings argued about *how* the measurement was taken and a fixed defect
    ("15/15 turns priced `None` with 26 rows sitting in `turn_costs`") sat behind a function
    nothing called. The reader it was built for — the routing comparison — went with the specialist
    team in `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`.

    Deleted rather than wired, because wiring it would have restored a column no report renders.
    Whoever wants per-probe cost back needs the producer, a reader that shows it, and this test
    updated in the same change.
    """
    module = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "evals" / "live.py"
    source = module.read_text(encoding="utf-8")
    assert "tokens" not in ProbeOutcome.model_fields
    assert "session_tokens" not in source, (
        "`evals/live.py` declares a token measurement again. It needs a caller and a reader in the "
        "same change, or it records `None` on every probe the way it did before."
    )


def test_a_run_where_every_judgement_is_ungraded_is_not_a_pass() -> None:
    """The empty-selection rule below, reached through the other door.

    `_main` ended `return 0` unconditionally, so a run against a gateway that cannot grade — the
    scripted mock, which `infra/live/processes.sh` starts by default — reported three probes,
    100% ungraded, exit 0, with both bolded honesty rows reading zero because nothing was judged.
    Measured on the live lane before this rule existed; it is now exit 2 on the same run.

    A verdict that is not `ungraded` is enough: the boundary is "nothing was measured", not a
    quality bar, and any share in between is a real result about the probes it names.
    """
    from chemclaw.cli.live_probes import _grading_status
    from chemclaw.evals.live_judge import Judgement

    ungraded = Judgement(probe_id="an-01", verdict="ungraded")
    served = Judgement(probe_id="an-02", verdict="served")

    assert _grading_status([]) == 2
    assert _grading_status([ungraded]) == 2
    assert _grading_status([ungraded, served]) == 0


def test_a_run_that_reached_nothing_is_not_a_pass() -> None:
    """The third arm of the same hole, and the one `_grading_status` does not close.

    Measured with nothing listening: three probes came back 100% `ConnectError`, the judge called
    the empty answers `unserved` — real verdicts, so the grading rule was satisfied — and the run
    exited **0**. Exit 3 follows `validate_template_args_live`: could not reach, never counted as
    checked. It binds `--no-judge` too, which is why that flag can keep exiting 0 otherwise.
    """
    from chemclaw.cli.live_probes import _reachability_status

    def outcome(probe_id: str, error: str = "") -> ProbeOutcome:
        return ProbeOutcome(
            probe_id=probe_id,
            section=1,
            persona="lab_technician",
            bucket="A",
            question="q",
            transport_error=error,
        )

    assert _reachability_status([]) == 0
    assert _reachability_status([outcome("an-01", "ConnectError")]) == 3
    assert _reachability_status([outcome("an-01", "ConnectError"), outcome("an-02")]) == 0


def test_a_run_writes_under_its_own_directory_and_never_over_the_record() -> None:
    """A live run used to write over tracked files in the committed transcripts directory.

    One review pass modified 196 of them and had to restore each with `git show HEAD:<p>`. The
    parent stays committed on purpose — `.gitignore` says why, in the file that enforces it — so
    the fix is a directory per run beneath it, shared by `live_probes` and `live_jobs` so two
    writers cannot disagree about where a run's output goes.
    """
    from chemclaw.cli.live_probes import _suite_dir, run_output_dir

    root = Path(settings.live_probe_transcript_dir)
    corpus = run_output_dir("corpus")
    assert corpus.parent.parent == root
    assert corpus != root and corpus.parent != root
    # Twice in one process is one run, or a suite's transcripts and its summary would split.
    assert run_output_dir("corpus") == corpus
    # An explicit --transcript-dir still wins, unstamped: promoting a run is a deliberate act.
    assert _suite_dir("somewhere/else", "corpus") == Path("somewhere/else")


def test_a_regrade_over_a_directory_with_no_transcripts_is_an_error(tmp_path: Path) -> None:
    """`--regrade` had no empty guard at all, and its report is committed evidence.

    It printed a "0 probes" summary, wrote it over `summary.md` in a directory `.gitignore`
    deliberately exempts so a live result can be read back later, and exited 0. The artefact
    survived; the run it describes never happened.
    """
    from chemclaw.cli import live_probes

    args = live_probes._parse_args(["--regrade", "--transcript-dir", str(tmp_path)])
    assert asyncio.run(live_probes._main(args)) == 2
    assert not (tmp_path / "summary.md").exists(), "a report was written over a run of nothing"


def test_the_report_names_the_gateway_that_produced_it() -> None:
    """A mock run and a real run used to produce files a reader cannot tell apart.

    `live_storm` prints its gateway for the same reason. The mock is recognised by asking
    `cli.mock_llm` rather than by a string written here — the transcription rule
    `tests/test_config.py` already enforces on `infra/live/processes.sh`.
    """
    from chemclaw.cli.live_probes import _gateway_line
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    line = _gateway_line()
    assert settings.llm_base_url in line
    if settings.llm_base_url == MOCK_BASE_URL:
        assert "not gradeable" in line


def test_a_selection_that_matches_no_probe_is_an_error_not_a_clean_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filtering every probe out ran zero, summarised nothing, and exited 0.

    `--only` filters the list and `--limit` slices it, and neither guarded an empty result — so
    `make live-probes ARGS="--only nosuchid"` measured nothing and reported success. A renamed
    probe id turns a scripted invocation into a permanent green line over an empty set. The M12
    suites in this same file already take the opposite convention (`0 if findings and all(...)
    else 1`), so this is the file disagreeing with itself.

    A non-positive `--limit` is refused by argparse rather than checked here, matching
    `sync_share._positive`: `probes[:-1]` silently drops the last probe, and `probes[:0]` is the
    empty selection above wearing a different spelling.
    """
    from chemclaw.cli import live_probes

    monkeypatch.setattr(live_probes, "load_probes", lambda _dir: [])
    args = live_probes._parse_args(["--only", "nosuchid"])
    assert asyncio.run(live_probes._main(args)) == 2

    with pytest.raises(SystemExit):
        live_probes._parse_args(["--limit", "0"])
    with pytest.raises(SystemExit):
        live_probes._parse_args(["--limit", "-1"])


# ---------------------------------------------------------------------------------------------
# The judge, after it stopped importing a vendor SDK
# (`D-2026-09-04-a-gateway-is-the-only-provider`).
#
# It was the last first-party importer of `anthropic`, and its one non-portable need was the
# truncation signal: `stop_reason == "max_tokens"`, which separates `ungraded` — the *absence* of a
# verdict — from a fabricated `unserved`. Conflating those mislabelled 65 of 190 probes in the first
# run and inflated the headline unserved rate from at most 22 to 87, so it is the property this port
# had to carry across rather than the one to lose quietly.
# ---------------------------------------------------------------------------------------------


def _graded_probe() -> tuple[Probe, ProbeOutcome]:
    """One answered probe, the cheapest input `judge_outcome` will actually spend a call on."""
    probe = Probe(
        id="j-01",
        section=1,
        persona="lab_technician",
        bucket="A",
        question="what is the pKa of acetic acid?",
        direction="a number with its method",
    )
    outcome = ProbeOutcome(
        probe_id="j-01",
        section=1,
        persona="lab_technician",
        bucket="A",
        question=probe.question,
        answer="4.76, computed with GFN2-xTB.",
        answered=True,
    )
    return probe, outcome


class _ScriptedJudge:
    """A chat model that answers with one prepared message, recording what it was asked."""

    def __init__(self, message: object) -> None:
        self.message = message
        self.prompts: list[object] = []

    async def ainvoke(self, messages: object) -> object:
        """Return the prepared reply, whatever was asked."""
        self.prompts.append(messages)
        return self.message


@pytest.mark.parametrize("key", ["finish_reason", "stop_reason"])
def test_a_truncated_judge_reply_is_ungraded_rather_than_a_verdict(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A reply cut off by its own token ceiling has no verdict, and must not be read as one.

    **The payload here is deliberately parseable**, which is what makes this test worth having: a
    truncated reply usually loses its closing brace and the JSON parse already yields `ungraded`, so
    a test over a mangled payload would pass with the truncation check deleted. This one is a
    complete, plausible `{"verdict": "unserved"}` — the shape that, read on its own, records a
    grading crash as a system failure.

    Both spellings, because LangChain does not normalise this: an OpenAI-compatible gateway reports
    `finish_reason: "length"` and one relaying a vendor's own field can say `stop_reason:
    "max_tokens"`. The vendor SDK this module used to import exposed only the second.
    """
    from langchain_core.messages import AIMessage

    from chemclaw.evals import live_judge

    value = "length" if key == "finish_reason" else "max_tokens"
    reply = AIMessage(
        content='{"verdict": "unserved", "reason": "no substance", "fabricated_claims": []}',
        response_metadata={key: value},
    )
    monkeypatch.setattr(live_judge, "_judge_client", lambda: _ScriptedJudge(reply))

    judgement = asyncio.run(live_judge.judge_outcome(*_graded_probe()))

    assert judgement.verdict == "ungraded"
    assert "token ceiling" in judgement.reason


def test_a_complete_judge_reply_is_graded_on_its_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: an ordinary reply is parsed, so the check above is not just refusing work.

    The axis this holds constant against the test above is exactly the one that broke a reader
    before — truncated vs complete — and without it the truncation check could be "return ungraded
    always" and both tests would still be green.
    """
    from langchain_core.messages import AIMessage

    from chemclaw.evals import live_judge

    reply = AIMessage(
        content='```json\n{"verdict": "served", "reason": "gives the number and the method", '
        '"fabricated_claims": []}\n```',
        response_metadata={"finish_reason": "stop"},
    )
    judge = _ScriptedJudge(reply)
    monkeypatch.setattr(live_judge, "_judge_client", lambda: judge)

    judgement = asyncio.run(live_judge.judge_outcome(*_graded_probe()))

    assert judgement.verdict == "served"
    assert judgement.reason == "gives the number and the method"
    # The prompt is a system message plus the rendered answer — the shape the seam expects, rather
    # than the vendor-specific `system=` + `messages=[]` pair this module used to post by hand.
    prompt = list(judge.prompts[0])  # type: ignore[call-overload]
    assert [m.type for m in prompt] == ["system", "human"]
    assert "4.76" in prompt[1].content


@pytest.mark.parametrize(
    "body",
    [
        '{"verdict": "Served", "reason": "ok"}',  # right word, wrong case
        '{"verdict": "pass", "reason": "ok"}',  # a vocabulary the prompt never offered
        '{"verdict": null, "reason": "ok"}',
        '{"reason": "ok"}',  # no verdict at all
    ],
)
def test_a_verdict_outside_the_vocabulary_is_ungraded_rather_than_a_crash(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """A judge that answers off-vocabulary did not grade — the same failure as an unparseable reply.

    Every other grader failure here already degrades to `ungraded`: the token ceiling, a reply with
    no JSON object, a `JSONDecodeError`. This one raised `ValidationError` straight out of
    `judge_outcome` on the `Literal` field, and the three callers gather without
    `return_exceptions=True` and report *after* the gather — so one `"Served"` among 190 probes
    discarded every grade in the run.
    """
    from langchain_core.messages import AIMessage

    from chemclaw.evals import live_judge

    reply = AIMessage(content=body, response_metadata={"finish_reason": "stop"})
    monkeypatch.setattr(live_judge, "_judge_client", lambda: _ScriptedJudge(reply))

    judgement = asyncio.run(live_judge.judge_outcome(*_graded_probe()))

    assert judgement.verdict == "ungraded"
    # The raw value is carried, not merely survived: a grader answering off-vocabulary is a defect
    # to see, and an `ungraded` with no reason is indistinguishable from a token ceiling.
    assert "no known verdict" in judgement.reason


def test_an_unrouted_judge_says_it_is_grading_with_the_model_under_test(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The one property of this judge worth a log line, since it has no setting of its own now.

    `live_probe_judge_model` was a vendor model id checked into this repository, which is what
    `model_routes` exists so that nobody has to do. The cost of routing it instead is that an unset
    route falls back to `llm_model` — the agent under test — and a judge sharing the agent's blind
    spots ratifies them. That degradation is silent by construction, so it is announced.
    """
    import logging

    from chemclaw.core.config import settings
    from chemclaw.evals import live_judge

    monkeypatch.setattr(settings, "model_routes", {})
    live_judge._judge_client.cache_clear()
    with caplog.at_level(logging.WARNING, logger="chemclaw.evals.live_judge"):
        live_judge._judge_client()
    assert any("live-probe-judge" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(settings, "model_routes", {"live-probe-judge": "big"})
    # Deliberately *not* the shipped 4096, which is also `llm_max_tokens`' default: at equal values
    # this assertion would pass with the `.bind` deleted. The first version of this test did exactly
    # that and its own guard caught it.
    monkeypatch.setattr(settings, "live_probe_judge_max_tokens", 7331)
    live_judge._judge_client.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="chemclaw.evals.live_judge"):
            client = live_judge._judge_client()
        assert not caplog.records
        # The route reaches the model, and the judge's own ceiling reaches the *request* rather
        # than only the constructed object — `llm_max_tokens` is the agent's answer allowance and
        # is not what a judge reply needs. Read off the payload for the reason
        # `tests/test_llm_effort.py` gives, and it is load-bearing here twice over: `ChatOpenAI`
        # *renames* the kwarg to `max_completion_tokens` on the wire, so an assertion on the
        # constructed object would neither have found the value nor noticed the rename.
        assert client.bound.model_name == "big"
        payload = client.bound._get_request_payload([("user", "x")], **client.kwargs)
        assert payload["max_completion_tokens"] == settings.live_probe_judge_max_tokens
        assert payload["max_completion_tokens"] != settings.llm_max_tokens, (
            "pick a judge ceiling that differs from llm_max_tokens, or this proves nothing"
        )
    finally:
        # Process-cached, so a later test must not inherit this route.
        live_judge._judge_client.cache_clear()


def test_the_corpus_fidelity_run_writes_under_its_own_directory_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`live_data` was the third writer into the committed transcripts directory.

    It wrote `tasks/live-test/transcripts/corpus-fidelity.md` — a *tracked* file — so every
    fidelity run dirtied the working tree and replaced the previous run's report with nothing
    marking which run either came from. Fixed by routing it through the same `run_output_dir`
    `live_probes` and `live_jobs` share, rather than by a second path policy: two writers into one
    directory is how the overwrite happened, and three would not be better.

    The checks themselves are stubbed out. What is under test is where the report lands, and that
    is decided after they run.
    """
    from chemclaw.cli import live_data
    from chemclaw.cli.live_probes import run_output_dir

    monkeypatch.setattr(settings, "live_probe_transcript_dir", str(tmp_path))

    async def _no_checks(*_args: object, **_kwargs: object) -> live_data.DataRun:
        return live_data.DataRun(seconds=0.0)

    monkeypatch.setattr(live_data, "run_data_checks", _no_checks)
    real_data = tmp_path / "real_data"
    real_data.mkdir()
    assert live_data.main(["--corpus-only", "--real-data", str(real_data)]) == 0
    capsys.readouterr()

    written = run_output_dir("corpus-fidelity") / "corpus-fidelity.md"
    assert written.is_file(), "the report must land in this run's own directory"
    assert written.parent.parent.parent == tmp_path, "one suite dir, one run dir, then the file"
    assert not (tmp_path / "corpus-fidelity.md").exists(), "never over the record"


def test_the_probe_client_does_not_hand_its_bearer_to_an_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one live-lane client carrying a credential, driven rather than scanned.

    `tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` is a ratchet
    over the *tree*: it reads `trust_env=False` out of the source and cannot say what httpx then
    does with it. This drives the real `_client()` against a loopback recorder standing in as the
    proxy, with the probe token set, and asserts the recorder saw nothing at all — not merely that
    it saw no `Authorization` header, because a proxy that receives the request receives the
    credential on the next hop whatever this one carried.

    The control arm is the same request through a client built without the keyword, which must
    reach the recorder; otherwise a recorder that was never wired up would prove the property by
    being broken.
    """
    received: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:
            received.append(self.headers.get("Authorization", "<no bearer>"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            """Silence the handler; `received` is the record."""

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setattr(settings, "live_probe_token", SecretStr("probe-secret"))

        async def post(client: httpx.AsyncClient) -> None:
            with contextlib.suppress(httpx.HTTPError, OSError):
                await client.post("/sessions", json={})
            await client.aclose()

        control = httpx.AsyncClient(base_url="http://front-door.invalid", timeout=5.0)
        asyncio.run(post(control))
        assert received, "the control arm reached no recorder, so this test proves nothing"
        received.clear()

        asyncio.run(post(live_probes._client("http://front-door.invalid")))
        assert received == [], (
            f"the probe client went through the ambient proxy ({received}) — the bearer it carries "
            "reaches whoever set the variable"
        )
    finally:
        server.shutdown()
        server.server_close()
