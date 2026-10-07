"""The live probe harness, and the shipped probe corpus as a declaration against the live surface.

The first kind of test exercises the runner's logic: folding an event stream into an outcome,
catching duplicate ids, telling grounded citations from invented ones. The second gates
`data/evals/probes/` like `skill-validate` gates skills: a probe expecting an unresolvable tool can
never pass and would read as a system defect.

The runner is driven through `httpx.MockTransport` with exact SSE wire frames, which is what lets a
test assert that a `tool_failed` frame with no `answer` yields `answered=False, failed_loudly=True`.
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
from tests.test_probe_coverage import fleet_expected_tools, withheld_tools

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


#: One turn's stream using the less common parts of the SSE grammar: a keepalive comment, `event:`
#: names, `id:`/`retry:` fields, and a `data:` field split over two lines (joined with a newline
#: before parsing). This server never sends a split field, but the reader's contract is the wire
#: format, and misreading a legal frame would report the system as broken. Shared with
#: `tests/test_live_storm.py` and `tests/test_live_benchmark.py` so the three readers are checked
#: against one object.
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

#: A stream that stops after the answer's `data:` line with no terminating blank line: what every
#: interrupted turn looks like. A reader that dispatches only on blank lines drops the last frame,
#: the one nearest the fault. Shared with `tests/test_live_storm.py`.
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

    The real derivation puts the truncation in the fixture: `preview` is cut at the wire budget
    while `numbers` is not.
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
    """No answer and no error is recorded as a silent failure, with `failed_loudly` False."""
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
    """Legal frames are read: a split `data:`, a comment, an `event:`, an `id:` and a `retry:`.

    Driven through `run_probe` because the failure is a whole event vanishing from an outcome: the
    `tool_call` must reach `tools_called` and the split `answer` must reach `answer`, and the
    keepalive comment must not become an event.
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
    """A stream that ends without a blank line still delivers its final event.

    `decoded_events` supplies the blank line the stream owed, since `EventSource.aiter_sse` alone
    drops the last frame. Asserted through `run_probe`, because a dropped answer turns into a false
    silent death against the system under test.
    """
    outcome = _run_bytes(TRUNCATED_STREAM, SSE_HEADERS)
    assert outcome.answer == "says ethanol."
    assert outcome.answered is True
    assert outcome.event_counts == {"token": 1, "answer": 1}
    assert outcome.transport_error is None


def test_a_response_that_is_not_an_event_stream_yields_nothing_rather_than_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 200 carrying JSON yields an empty turn and a named warning, never an exception.

    `httpx_sse` raises `SSEError` for that content type, which would be fatal in a caller without a
    handler (`cli/live_benchmark._ask`). The log names the content type that arrived;
    `transport_error` stays empty because the network was fine.
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
    """A citation counts only when a tool result in this turn actually returned it.

    An id produced from memory is flagged even if the note exists in the corpus.
    """
    returned = {"rxn-suzuki-biaryl"}
    assert _score_citations("see [[rxn-suzuki-biaryl]]", returned) == []
    assert _score_citations("see [[evidence-for:rxn-suzuki-biaryl]]", returned) == []
    assert _score_citations("see [[rxn-never-retrieved]]", returned) == ["rxn-never-retrieved"]


def _notes_event(tool: str, note_ids: list[str]) -> dict[str, object]:
    """A `tool_result` frame that returned these note ids, which is what a gold set grades."""
    return {"type": "tool_result", "tool": tool, "preview": "", "note_ids": note_ids, "numbers": []}


def test_the_gold_set_scores_what_retrieval_returned_and_not_what_the_answer_cited() -> None:
    """The gold-set recall scores what retrieval returned, not what the answer cited.

    `expects_notes` grades retrieval as `expected & returned_ids` over `expected`; an uncited
    returned note is a citation defect reported by `uncited_note_ids`. The fixture makes the two
    disagree.
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
    """A probe expecting notes that got none scores `0.0`, not `None`.

    `cli/live_probes` includes a probe in the gold-set mean only when recall is not `None`, so a
    real zero reported as `None` would drop out of its own denominator.
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
    """The report counts a zero-scoring probe in the gold-set mean.

    Through the real `_summary`: probes at 1.0, 0.0 and one declaring no notes read as 0.50 over 2.
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
    """A citation past the preview budget is still grounded.

    Only the first id fits in the preview, so a substring scan over previews is the one approach
    that gives the wrong answer.
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
    """Figures quoted from a real tool result are verified against the full result, not the preview.

    The tool result is a recorded `ich_impurity_limit` output (`tests/recorded_tool_results.py`);
    the citation scorer is under test. The assertion on where character 200 falls shows the figures
    lie beyond the preview a judge would otherwise be shown.
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
    """A figure no tool returned is simply absent from the verified list, not reported.

    Numbers, unlike citations, are legitimately derived (differences, totals, textbook constants),
    so the harness asserts membership and never absence.
    """
    text = '{"limits": [{"basis": "oral PDE", "value": 100.0, "unit": "\\u00b5g/day"}]}'
    outcome = _run(
        _probe(),
        _result_event("ich_impurity_limit", text),
        {"type": "answer", "text": "The oral PDE is 100 µg/day; parenteral is 250 µg/day."},
    )
    assert outcome.verified_numbers == ["100"]


def test_an_unreadable_figure_costs_that_figure_and_not_the_turn() -> None:
    """An unreadable figure costs that figure, not the turn.

    A non-numeric `numbers` entry must not raise out of the stream loop, dropping the answer and
    filing the turn as a transport error.
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
    """Every expected tool in the shipped corpus exists on the agent surface.

    The fleet exemption is imported from `tests/test_probe_coverage.py` rather than restated, so the
    rule about tools `Chemclaw3-mcp` serves under `needs_bundle:` lives in one place.
    """
    surface = available_tool_names()
    unknown = {t for p in load_probes(str(PROBE_DIR)) for t in p.expects_tools if t not in surface}
    exempt = fleet_expected_tools() | withheld_tools()
    assert unknown - exempt == set(), (
        f"probes expect tools that do not exist: {sorted(unknown - exempt)}"
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
    """`--no-judge` writes no grades file rather than overwriting real verdicts with an empty list.

    An empty grades file is indistinguishable from a run in which every answer failed.
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
    """`expects_job` resolves the workflow against the broker.

    A job tool returns an id once the launch is accepted, so a truthful stream cannot show whether
    the broker ran it; the outcome must carry what Temporal says.
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
    """The broker lookup is opt-in, so an ordinary probe costs no Temporal round trip.

    `_job_outcomes` records `unreachable` when it cannot connect, which would be noise on every
    outcome of a run that never cared about durable work.
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
    # Two named exceptions ask about the record of durable work rather than starting any: du-04
    # (what past jobs ran) and du-10 (what is pending, answered from `pending_requests`).
    exempt = {"du-04", "du-10"}
    silent = sorted(p.id for p in durable if not p.expects_job and p.id not in exempt)
    assert not silent, (
        f"durable probe(s) {silent} do not declare `expects_job` — they would run, pass and prove "
        "nothing about Temporal. Set it, or add the id to `exempt` above with the reason."
    )
    assert exempt <= {p.id for p in durable}


def test_no_probe_direction_asserts_which_deployment_it_meets() -> None:
    """No probe direction asserts which deployment it meets (e.g. "Temporal is not running").

    Such a key goes false when the stack changes; directions describe behaviour, and the environment
    is the runner's business.
    """
    offenders: list[str] = []
    for probe in load_probes(str(PROBE_DIR)):
        text = probe.direction.lower()
        if "is not running" in text or "not reachable in this run" in text:
            offenders.append(probe.id)
    assert not offenders, f"probe directions asserting the deployment they meet: {offenders}"


def test_a_job_that_finished_inside_the_turn_is_not_reported_as_no_job_at_all() -> None:
    """A job that finished inside the turn is not reported as no job at all.

    A job answering within `inline_wait_seconds` returns its result instead of an id and is never
    announced, so `jobs_started` is legitimately empty for a completed job.
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
    """An announced outage does not hide a silent death.

    `capability_degraded` is announced before the turn runs and is the system working, so it must
    not set `failed_loudly`; otherwise a broker-less deployment could never show a silent failure.
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
    """The harness makes no per-probe token-cost claim.

    The field had no producer, so every probe recorded `None`. Restoring it needs a producer, a
    reader that shows it, and this test updated in the same change.
    """
    module = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "evals" / "live.py"
    source = module.read_text(encoding="utf-8")
    assert "tokens" not in ProbeOutcome.model_fields
    assert "session_tokens" not in source, (
        "`evals/live.py` declares a token measurement again. It needs a caller and a reader in the "
        "same change, or it records `None` on every probe the way it did before."
    )


def test_a_run_where_every_judgement_is_ungraded_is_not_a_pass() -> None:
    """A run where every judgement is ungraded is not a pass.

    A gateway that cannot grade (the scripted mock) would otherwise exit 0 with nothing measured.
    Any verdict that is not `ungraded` suffices: the boundary is "nothing was measured", not
    quality.
    """
    from chemclaw.cli.live_probes import _grading_status
    from chemclaw.evals.live_judge import Judgement

    ungraded = Judgement(probe_id="an-01", verdict="ungraded")
    served = Judgement(probe_id="an-02", verdict="served")
    answered = [_answered("an-01"), _answered("an-02")]

    assert _grading_status([], [], scripted=False) == 2
    assert _grading_status([ungraded], answered, scripted=False) == 2
    assert _grading_status([ungraded, served], answered, scripted=False) == 0


def _answered(probe_id: str, *, answered: bool = True) -> ProbeOutcome:
    """An outcome that did (or did not) produce an answer event — all `_grading_status` reads."""
    return ProbeOutcome(
        probe_id=probe_id,
        section=1,
        persona="lab_technician",
        bucket="A",
        question="q",
        answer="an answer" if answered else "",
        answered=answered,
    )


def test_an_unserved_turn_that_never_answered_is_not_a_graded_verdict() -> None:
    """An `unserved` verdict for a turn that never answered is not a grade.

    `judge_outcome` returns it without asking the judge, so it records a transport failure; the same
    verdict from a judge that read a real answer is a grade.
    """
    from chemclaw.cli.live_probes import _grading_status
    from chemclaw.evals.live_judge import Judgement

    grades = [
        Judgement(probe_id="an-01", verdict="ungraded"),
        Judgement(probe_id="an-02", verdict="unserved", reason="no answer event was produced"),
    ]
    broken = [_answered("an-01"), _answered("an-02", answered=False)]
    assert _grading_status(grades, broken, scripted=False) == 2

    judged = [_answered("an-01"), _answered("an-02")]
    assert _grading_status(grades, judged, scripted=False) == 0


def test_a_run_against_the_scripted_mock_exits_non_zero_whatever_it_graded() -> None:
    """`_gateway_line` promises a mock run "will exit non-zero"; this is the promise kept.

    A judge on the mock's gateway is the mock, so even a clean `served` is a script grading a
    script — evidence about the double, never about the system.
    """
    from chemclaw.cli.live_probes import _grading_status
    from chemclaw.evals.live_judge import Judgement

    grades = [Judgement(probe_id="an-01", verdict="served")]
    assert _grading_status(grades, [_answered("an-01")], scripted=True) == 2
    assert _grading_status(grades, [_answered("an-01")], scripted=False) == 0


def test_a_run_that_reached_nothing_is_not_a_pass() -> None:
    """A run that reached nothing is not a pass: it exits 3.

    All-`ConnectError` turns judged `unserved` satisfy the grading rule, so reachability is checked
    separately, as `validate_template_args_live` does. It applies under `--no-judge` too.
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
    """A run writes under its own directory and never over the committed transcripts.

    The directory per run is shared by `live_probes` and `live_jobs` so the writers cannot disagree.
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
    """`--regrade` over a directory with no transcripts is an error.

    Otherwise it would write a "0 probes" summary as committed evidence of a run that never
    happened.
    """
    from chemclaw.cli import live_probes

    args = live_probes._parse_args(["--regrade", "--transcript-dir", str(tmp_path)])
    assert asyncio.run(live_probes._main(args)) == 2
    assert not (tmp_path / "summary.md").exists(), "a report was written over a run of nothing"


def test_the_report_names_the_gateway_that_produced_it() -> None:
    """The report names the gateway that produced it, so mock and real runs are distinguishable.

    The mock is recognised by asking `cli.mock_llm` rather than by a string written here.
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
    """A selection that matches no probe is an error, not a clean run.

    `--only` and `--limit` can produce an empty list, which would measure nothing and exit 0. A
    non-positive `--limit` is refused by argparse, matching `sync_share._positive`: `probes[:-1]`
    drops a probe and `probes[:0]` is the empty selection.
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
# The judge, through the OpenAI-compatible gateway.
#
# The truncation signal is the property to keep: it separates `ungraded` (no verdict) from a
# fabricated `unserved`.
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
    """A reply cut off by its token ceiling is `ungraded`, even when its payload parses.

    The payload is a complete `{"verdict": "unserved"}`, so the truncation check alone must catch
    it. Both spellings are covered: `finish_reason: "length"` and a relayed `stop_reason:
    "max_tokens"`.
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
    """A complete judge reply is graded on its verdict.

    Holds the truncated/complete axis against the test above, so "always ungraded" cannot pass both.
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
    """An off-vocabulary verdict is `ungraded` rather than a crash.

    A `ValidationError` out of `judge_outcome` would propagate through callers that gather without
    `return_exceptions=True`, discarding every grade in the run.
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
    """An unrouted judge logs that it is grading with the model under test.

    An unset route falls back to `llm_model`, and a judge sharing the agent's blind spots ratifies
    them; that degradation is silent unless announced.
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
        # The judge's own ceiling must reach the request, not just the constructed object:
        # `ChatOpenAI` renames the kwarg to `max_completion_tokens` on the wire, so it is read off
        # the payload.
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
    """The corpus-fidelity run also writes under its own run directory.

    It routes through the shared `run_output_dir` rather than a second path policy. The checks are
    stubbed; only where the report lands is under test.
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
    """The probe client does not hand its bearer to an ambient proxy, driven rather than scanned.

    Drives the real `_client()` against a loopback recorder posing as the proxy and asserts it saw
    nothing, since a proxy that receives the request receives the credential. The control arm, a
    client without `trust_env=False`, must reach the recorder, proving it is wired up.
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


def test_the_plan_gate_suite_refuses_to_stage_against_the_scripted_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 3, before a probe is asked — not the 0/5 FAIL the mock used to earn.

    `cli.mock_llm` never writes a plan, so every approval POST is a 409 and each check failed on a
    scenario that was never staged. "Could not reach it" is this harness's exit 3.
    """
    from chemclaw.cli.mock_llm import MOCK_BASE_URL

    def no_front_door(base_url: str | None) -> httpx.AsyncClient:
        raise AssertionError("the suite dialled the front door against a gateway that cannot plan")

    monkeypatch.setattr(settings, "llm_base_url", MOCK_BASE_URL)
    monkeypatch.setattr(live_probes, "_client", no_front_door)
    args = live_probes._parse_args(["--suite", "plan-gate"])
    assert asyncio.run(live_probes._run_plan_gate(args)) == 3


#: Openings of live answers (2026-09-27, DeepSeek V4 Pro) that replied to the verifier's revision
#: note instead of to the chemist, verbatim — every one from a single-question probe, so there was
#: no earlier turn for the model to be "right" about.
_LEAKED_OPENINGS = [
    "You're right — I wrote `compute_thermochemistry` in prose without calling it, which is the",
    "Understood. I am dropping both claims. Here is the corrected assessment.\n\n---\n\n## What",
    "Understood. Looking back at what the tools actually returned this turn:\n\n- **`screen_",
    "Good catch — my closing sentence named tools I did not call, which is exactly the rule.",
    "You're right, and I'll be direct about where things stand.\n\nI searched the durable job",
    "## Corrected answer\n\n### What the evidence supports\n\nThe record is silent on HPLC",
    "Here is the corrected answer, with every claim traced to the evidence that supports it.",
    "Here is the answer, stripped of the unsupported claim and built strictly from what was",
    "---\n\n# Pd-catalysed couplings of deactivated aryl chlorides — evidence sweep (corrected)",
    # From the measurement of the old note against the same model: a reply to the critic that
    # names neither agreement nor a correction in its first words.
    "I acknowledge your note, but the two claims you flagged — the decomposition onset is 180",
    "The note retrieved in this turn corrects two physicochemical estimates from my last answer",
]

#: Openings that must *not* count: ordinary answers, and one that correctly acknowledges something
#: the chemist themselves said (live probe ws-03), which is why "Got it" is not in the pattern.
_CHEMIST_FACING_OPENINGS = [
    "Got it — DMF is off the table for this project, permanently. I've recorded that as a",
    "Here's what the evidence actually supports:\n\n- **The knowledge graph is silent.**",
    "## ⚠️ High-severity hazard: do not isolate this solid\n\nThe hazard screen returned",
    "Yes — 4-bromoanisole has been used in two distinct Pd-catalysed couplings in this programme",
    "I don't see any structures attached or listed in your message. Could you share the twelve",
    "The record doesn't hold the yield data needed to run this test — no past job, no stored",
    "You can run this at 40 °C, but you're right to worry about the exotherm: the record shows",
]


@pytest.mark.parametrize("opening", _LEAKED_OPENINGS)
def test_an_answer_replying_to_the_revision_note_is_counted(opening: str) -> None:
    """The eval half of the revision-note fix: whether the model wrote to the chemist is scored."""
    from chemclaw.evals.live import opens_by_acknowledging_a_critique

    assert opens_by_acknowledging_a_critique(opening)


@pytest.mark.parametrize("opening", _CHEMIST_FACING_OPENINGS)
def test_an_answer_written_to_the_chemist_is_not_counted(opening: str) -> None:
    """The control: the pattern must not fire on an answer that simply answers."""
    from chemclaw.evals.live import opens_by_acknowledging_a_critique

    assert not opens_by_acknowledging_a_critique(opening)


def test_the_signal_reaches_the_run_summary() -> None:
    """A signal the summary never prints is a field nobody reads.

    Driven through the real `_summary` with one outcome that leaked and one that did not, so the
    row and its count are both what is asserted.
    """
    probes = [_probe(id="t-01"), _probe(id="t-02")]

    def _outcome(probe: Probe, answer: str) -> ProbeOutcome:
        """One answered outcome for `probe`, scored the way `run_probe` scores it."""
        from chemclaw.evals.live import opens_by_acknowledging_a_critique

        return ProbeOutcome(
            probe_id=probe.id,
            section=probe.section,
            persona=probe.persona,
            bucket=probe.bucket,
            question=probe.question,
            answer=answer,
            answered=True,
            acknowledged_critique=opens_by_acknowledging_a_critique(answer),
        )

    outcomes = [
        _outcome(probes[0], _LEAKED_OPENINGS[0]),
        _outcome(probes[1], _CHEMIST_FACING_OPENINGS[1]),
    ]
    report = live_probes._summary(probes, outcomes, [], "provenance: a test")
    assert "answers opening on a critique the chemist never made** | **1**" in report
