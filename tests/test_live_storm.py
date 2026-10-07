"""The storm harness's own honesty mechanisms, tested where they are pure.

What the storm measures needs a live stack (`make live-storm`). This file covers the pure functions
whose output is quoted as a finding and the guards that stop the harness overstating what it did:
the families count (planned versus ran), `_knee` (goodput, not refusals), `_bad_call_was_reported`
(the tool, not `empty_answer`), the `[[selector]]` assertion, and `mock_llm._validate` (catalogue
calls match the live tool surface). No network, database or broker.
"""

from __future__ import annotations

import asyncio

import pytest

from chemclaw.cli import live_storm
from chemclaw.cli.live_storm import (
    FAMILIES,
    Finding,
    TurnResult,
    _bad_call_was_reported,
    _completed_without_dying,
    _knee,
    noise,
    percentiles,
    report,
    storm,
)
from chemclaw.cli.mock_llm import Behaviour, ToolCall, _validate, already_has_tool_results
from chemclaw.cli.storm_behaviours import BEHAVIOURS


def _row(cap: int, goodput: float, spread: float = 0.0) -> dict[str, object]:
    """One admission-sweep row, with only the fields `_knee` and `noise` read.

    `spread` is the within-cap disagreement across repeated samples, as a fraction of the median. It
    defaults to zero so tests not about noise can ignore it.
    """
    return {"cap": cap, "goodput": goodput, "spread": spread, "samples": [goodput]}


# --------------------------------------------------------------------------- the knee


def test_the_knee_is_the_cap_whose_successor_stops_paying() -> None:
    """The knee is the cap whose successor stops paying, against a measured noise floor.

    Goodput 0.82, 1.01, 1.52, 1.58, 1.78 answered/s at caps 2 to 32 with a 5 % floor: the 8 to 16
    step is +3.9 %, inside the floor, so the knee is 8.
    """
    rows = [
        _row(2, 0.82, 0.05),
        _row(4, 1.01, 0.04),
        _row(8, 1.52, 0.03),
        _row(16, 1.58, 0.05),
        _row(32, 1.78, 0.04),
    ]
    assert _knee(rows) == 8


def test_a_sweep_too_noisy_to_read_reports_no_knee_rather_than_the_first_step() -> None:
    """A sweep too noisy to read reports no knee rather than the first step.

    Treating "a step smaller than the spread" as flat fires sooner as noise grows, so at large
    spread every step qualifies and the smallest cap would be named. `_knee` refuses above
    `_MAX_READABLE_NOISE`; `None` means "not known yet", whether from range or from noise.
    """
    readable = [
        _row(2, 0.82, 0.05),
        _row(4, 1.01, 0.04),
        _row(8, 1.52, 0.03),
        _row(16, 1.58, 0.05),
        _row(32, 1.78, 0.04),
    ]
    assert _knee(readable) == 8

    unreadable = [_row(2, 0.82, 0.20), *readable[1:]]
    assert noise(unreadable) == pytest.approx(0.20)
    assert _knee(unreadable) is None


def test_the_noise_floor_is_the_worst_cap_not_the_average() -> None:
    """An upper bound on how wrong one sample can be, which is the only safe reading of it.

    Averaging would let four well-behaved caps hide the one that disagreed with itself, and it is
    exactly that cap whose neighbours cannot be told apart.
    """
    assert noise([_row(2, 1.0, 0.02), _row(4, 2.0, 0.19), _row(8, 3.0, 0.03)]) == pytest.approx(
        0.19
    )
    assert noise([]) == 0.0


def test_a_sweep_that_never_flattens_reports_no_knee() -> None:
    """A sweep still improving at the top reports no knee.

    Returning the top of the range would present a limit of the measurement as a property of the
    system.
    """
    assert _knee([_row(2, 1.0), _row(4, 2.0), _row(8, 4.0), _row(16, 8.0)]) is None


def test_the_line_between_flat_and_still_climbing_is_the_measured_spread() -> None:
    """The line between flat and still climbing is the measured spread.

    A 9 % step is flat against a 10 % floor and a climb against a 5 % one; a constant threshold
    would be right only for the machine it was chosen on.
    """
    assert _knee([_row(2, 1.00, 0.10), _row(4, 1.09, 0.10)]) == 2
    assert _knee([_row(2, 1.00, 0.05), _row(4, 1.09, 0.05)]) is None


def test_a_single_step_sweep_has_no_successor_to_judge() -> None:
    """One row (or none) cannot show a knee — and must not claim one."""
    assert _knee([_row(8, 1.5)]) is None
    assert _knee([]) is None


# --------------------------------------------------------------------------- coverage honesty


def test_the_report_names_the_families_that_produced_nothing() -> None:
    """The report names the families that produced nothing.

    A pass count is silent about families that never ran, so the report states planned versus ran.
    """
    findings = [Finding(family="A", name="something", ok=True, observed="1")]
    text = report(findings, [], {}, ["A", "B", "C"])
    assert "**1/3 planned families ran.**" in text
    assert "Did not run: B, C" in text
    # And the per-family table marks the empty ones, so a skim catches it too.
    assert "| B | " in text and "**0**" in text


def test_a_full_report_says_so_without_a_did_not_run_line() -> None:
    """The other direction: a complete run must not carry a warning it has not earned."""
    findings = [Finding(family=letter, name="x", ok=True, observed="1") for letter in FAMILIES]
    text = report(findings, [], {}, list(FAMILIES))
    assert f"**{len(FAMILIES)}/{len(FAMILIES)} planned families ran.**" in text
    assert "Did not run" not in text


def test_the_sweep_table_says_which_column_is_throughput() -> None:
    """Both rates are printed, and the report labels which one is throughput.

    `drain` explains earlier numbers but must never stand unlabelled beside the real measurement.
    """
    sweep = [
        {
            "cap": 8,
            "offered": 48,
            "turns": 48,
            "accepted": 16,
            "failed": 32,
            "p50": 8.1,
            "p95": 10.4,
            "goodput": 1.52,
            "drain": 4.55,
        }
    ]
    text = report([], sweep, {}, ["A"])
    assert "answered/s" in text and "offered drained/s" in text
    assert "is not throughput" in text


# --------------------------------------------------------------------------- the verdict predicates


def test_a_bad_call_is_only_reported_when_the_tool_says_so() -> None:
    """A bad call is reported only when the tool says so; `empty_answer` alone does not count.

    Every adversarial behaviour writes no prose and so produces `empty_answer`; accepting any error
    code would pass all of them without looking at the tool.
    """
    silent = TurnResult(status=200, error_code="empty_answer", result_previews=["[]"])
    assert not _bad_call_was_reported(silent)

    failed = TurnResult(status=200, error_code="empty_answer", tools_failed=["compute_x"])
    assert _bad_call_was_reported(failed)

    refused = TurnResult(status=200, result_previews=["Error: Argument parsing failed."])
    assert _bad_call_was_reported(refused)


def test_a_turn_that_never_reached_the_front_door_reported_nothing() -> None:
    """A non-200 cannot be evidence that the *tool* failed loudly — it never got that far."""
    assert not _bad_call_was_reported(TurnResult(status=503, tools_failed=["compute_x"]))


def test_surviving_a_large_input_is_not_the_same_as_refusing_it() -> None:
    """The distinction that split these two predicates, after one of them was wrong.

    A 100 KB search string is legitimate input: `find_notes` ran it and returned `[]`, and that is
    the correct outcome. Demanding a refusal was the check being wrong about what good looks like.
    """
    absorbed = TurnResult(status=200, answered=True, result_previews=["[]"])
    assert _completed_without_dying(absorbed)
    assert not _bad_call_was_reported(absorbed)

    dropped = TurnResult(status=200, transport_error="ReadError: connection closed")
    assert not _completed_without_dying(dropped)

    # An error code counts as reaching an end a client can read — silence does not.
    assert _completed_without_dying(TurnResult(status=200, error_code="empty_answer"))
    assert not _completed_without_dying(TurnResult(status=200))


def test_percentiles_ignore_turns_that_never_answered() -> None:
    """Latency over shed turns would report how fast the door says no."""
    results = [
        TurnResult(status=200, seconds=1.0),
        TurnResult(status=200, seconds=3.0),
        TurnResult(status=429, seconds=0.01),
    ]
    p50, p95 = percentiles(results)
    assert p50 == 2.0
    assert p95 == 3.0
    assert percentiles([TurnResult(status=429, seconds=0.01)]) == (0.0, 0.0)


# --------------------------------------------------------------------------- the selector


def test_a_custom_message_without_its_selector_is_refused() -> None:
    """A custom message without its `[[name]]` selector is refused.

    Family H sends user-shaped text, and the selector is how the mock knows its scenario; without it
    the turn would be graded against a behaviour it never ran.
    """
    with pytest.raises(ValueError, match=r"\[\[h-unicode\]\]"):
        asyncio.run(storm("h-unicode", turns=1, concurrency=1, message="no marker here"))


def test_every_declared_family_has_a_description() -> None:
    """`FAMILIES` is what the report prints and what `--families` validates against.

    One declaration, so a family cannot be runnable and undescribed (or described and unrunnable).
    """
    assert set(FAMILIES) == set("ABCDEFGH")
    assert all(description.strip() for description in FAMILIES.values())


def test_the_lane_scripts_the_chaos_family_drives_exist() -> None:
    """The chaos family shells out to these; a rename would fail only mid-run, minutes in."""
    for script in ("processes.sh", "bootstrap.sh"):
        assert (live_storm._LANE_DIR / script).is_file()


def test_the_note_repo_is_provisioned_before_the_docker_branch_takes_over() -> None:
    """The note repo is provisioned before the Docker branch's `exec docker compose`.

    `exec` never returns, so a step placed after it runs only without Docker; without the dedicated
    clone, `note_repo_dir` falls back to the working checkout. The invariant is positional, so the
    test pins order textually, matching the commands rather than prose a nearby comment could move.
    """
    script = (live_storm._LANE_DIR / "bootstrap.sh").read_text(encoding="utf-8")
    provision = script.index("\n    ensure_note_repo\n")
    handover = script.index("exec docker compose -f ")
    assert provision < handover, (
        "ensure_note_repo must run before `exec docker compose` hands the process over; "
        "below it, the Docker path silently skips the PR-gate's clone"
    )


# --------------------------------------------------------------------------- the mock's own guard


def test_the_catalogue_passes_the_load_1_guard() -> None:
    """Every shipped behaviour validates against the live tool surface.

    Run over the whole catalogue on every diff, so a behaviour cannot silently rot against a renamed
    parameter, sending calls that die before any tool body runs.
    """
    for behaviour in BEHAVIOURS:
        _validate(behaviour)


def test_a_wrong_argument_name_is_refused_by_name() -> None:
    """LOAD-1's own shape, rejected with a message that says what it is."""
    with pytest.raises(ValueError, match="exactly LOAD-1"):
        _validate(
            Behaviour(name="t", calls=[ToolCall(tool="find_notes", arguments={"query": "benzene"})])
        )


def test_a_tool_the_agent_does_not_advertise_is_refused() -> None:
    """A typo'd tool name would otherwise be measured as "the system rejected an unknown tool"."""
    with pytest.raises(ValueError, match="does not advertise"):
        _validate(Behaviour(name="t", calls=[ToolCall(tool="find_notez", arguments={})]))


def test_deliberate_malformation_must_be_declared() -> None:
    """`adversarial=True` is the opt-out, and raw arguments cannot be sent without it.

    The polarity matters: an undeclared malformed call is indistinguishable from a stale behaviour,
    and the guard's whole value is that it can tell them apart.
    """
    bad = Behaviour(
        name="t", calls=[ToolCall(tool="find_notes", arguments={}, raw_arguments='{"text": ')]
    )
    with pytest.raises(ValueError, match="mark the behaviour adversarial"):
        _validate(bad)
    _validate(Behaviour(name="t", calls=bad.calls, adversarial=True))  # declared: allowed


def test_a_request_carrying_tool_output_is_recognised_as_a_continuation() -> None:
    """A request carrying tool output is recognised as a continuation.

    Otherwise the mock replays its calls after every result and a turn never finishes. Read off the
    request rather than per-session state, so concurrent turns cannot corrupt each other's counters.
    """
    first = {"input": [{"type": "message", "role": "user", "content": "hello"}]}
    after_tools = {"input": [{"type": "function_call_output", "call_id": "c1", "output": "[]"}]}
    assert not already_has_tool_results(first)
    assert already_has_tool_results(after_tools)
    assert not already_has_tool_results({"input": "a bare string"})
    assert not already_has_tool_results({})
    # Chat completions says the same thing with a `role: "tool"` message. Reading only the
    # Responses shape would run that protocol to the iteration cap — the identical runaway.
    assert already_has_tool_results({"messages": [{"role": "tool", "content": "[]"}]})
    assert not already_has_tool_results({"messages": [{"role": "user", "content": "hello"}]})


def test_the_mock_serves_the_protocol_the_engine_actually_posts_to() -> None:
    """The mock serves `/v1/chat/completions`, the route `ChatOpenAI` posts to.

    Without it every credential-free lane gets a 404 and every turn dies with no answer, reading as
    a system defect. Asserted against the app's routing table, since a missing route is invisible to
    a handler test.
    """
    from chemclaw.cli.mock_llm import MockLlm, build_app

    served = {getattr(route, "path", None) for route in build_app(MockLlm(BEHAVIOURS)).routes}
    assert "/v1/chat/completions" in served, sorted(p for p in served if p)
    # Both, not either: the Responses route is still what a deployment on that API would reach,
    # and dropping it would trade one silent 404 for another.
    assert "/v1/responses" in served


def test_every_declared_behaviour_is_reached_by_some_check() -> None:
    """Every declared behaviour is reached by some check.

    A behaviour nothing drives is a scenario the catalogue advertises and the run never has. Checked
    by reading the harness source for each name, since names travel as `[[selector]]` strings inside
    turn messages and there is no symbol to resolve.
    """
    harness = (live_storm._LANE_DIR.parents[1] / "src/chemclaw/cli/live_storm.py").read_text(
        encoding="utf-8"
    )
    unreached = [b.name for b in BEHAVIOURS if b.name not in harness]
    assert not unreached, (
        f"{len(unreached)} behaviour(s) are declared and driven by nothing: {unreached}. "
        "Wire a check that asserts something about them, or delete them — a catalogue entry that "
        "no run reaches is coverage the report cannot claim and a reader will assume."
    )


def test_run_turn_reads_the_same_awkward_stream_the_probe_harness_does() -> None:
    """`run_turn` reads the same awkward stream the probe harness does.

    The storm grades whether a turn answered, so a reader dropping a legal frame would report a
    system going silent under load. All three harnesses share `evals.live.decoded_events` and the
    fixture, served through `httpx.MockTransport`.
    """
    import httpx

    from tests.test_live_probes import AWKWARD_STREAM, SSE_HEADERS

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=AWKWARD_STREAM, headers=SSE_HEADERS)

    async def go() -> TurnResult:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await live_storm.run_turn(client, "hello [[a-cheap]]")

    result = asyncio.run(go())
    assert result.status == 200
    assert result.announced == 1
    assert result.answered is True
    assert result.transport_error is None


def test_run_turn_keeps_the_answer_of_a_turn_whose_stream_was_cut_off() -> None:
    """`run_turn` keeps the answer of a turn whose stream was cut off.

    A cancelled turn's stream ends after its last `data:` line with no blank line; dropping that
    frame would turn an answered turn into a silent one. The fixture is
    `tests/test_live_probes.TRUNCATED_STREAM`, imported rather than copied.
    """
    import httpx

    from tests.test_live_probes import SSE_HEADERS, TRUNCATED_STREAM

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=TRUNCATED_STREAM, headers=SSE_HEADERS)

    async def go() -> TurnResult:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await live_storm.run_turn(client, "hello [[a-cheap]]")

    result = asyncio.run(go())
    assert result.answered is True
    assert result.transport_error is None


def test_a_refused_turn_is_recorded_as_its_status_rather_than_as_a_transport_failure() -> None:
    """A refused turn is recorded by its status, not as a transport failure.

    Admission control answers with a 429 and a JSON body, which `decoded_events` reads as an empty
    stream. The status is taken off the response before the stream is touched, so shed turns stay in
    the `status` histogram the shedding check reads.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(429, json={"detail": "at capacity"})

    async def go() -> TurnResult:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://front-door"
        ) as client:
            return await live_storm.run_turn(client, "hello [[a-cheap]]")

    result = asyncio.run(go())
    assert result.status == 429
    assert result.transport_error is None
    assert result.answered is False


def test_the_calibration_check_reads_the_ratio_not_the_in_flight_estimate() -> None:
    """The calibration check reads the ratio leaving its clamp, not the in-flight estimate.

    The clamp is exactly 1.0. `turn_costs.estimated_tokens` covers only in-flight or cancelled
    prompts and is 0 on every completed turn.
    """
    exposition = (
        "# HELP chemclaw_context_estimator_ratio x\n"
        "# TYPE chemclaw_context_estimator_ratio gauge\n"
        "chemclaw_context_estimator_ratio 2.0625\n"
        "chemclaw_context_estimator_ratio_other 9\n"
    )
    ratio = live_storm.metric_sample(exposition, live_storm.ESTIMATOR_RATIO_GAUGE)
    assert ratio == 2.0625
    assert live_storm.metric_sample("", live_storm.ESTIMATOR_RATIO_GAUGE) is None

    assert live_storm._calibration_finding(200, 41_000, ratio).ok
    assert not live_storm._calibration_finding(200, 41_000, 1.0).ok, "1.0 is the clamp itself"
    assert not live_storm._calibration_finding(200, 41_000, None).ok, "unreadable is not a pass"
    assert not live_storm._calibration_finding(200, 0, ratio).ok, "nothing billed, nothing shown"
    assert not live_storm._calibration_finding(500, 41_000, ratio).ok
