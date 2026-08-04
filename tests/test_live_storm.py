"""The storm harness's own honesty mechanisms, tested where they are pure.

Everything the storm *measures* needs a live stack, and that half is `make live-storm`'s job. What
this file covers is the other half: the small pure functions whose output is quoted as a finding,
and the guards that exist so the harness cannot overstate what it did. Those have to hold on a
diff, because the failure they prevent is silent by construction — a coverage claim that is wrong,
a knee that is an artefact, a check that passes for a reason unrelated to what it names.

That is not a hypothetical list. Each of these guards was added after the thing it prevents had
already happened once, in this repository, in a run that reported success:

* the families count, after a report printed "17/17 checks passed" for a matrix two of whose eight
  families were never implemented;
* `_knee`, after a throughput metric that counted refusals as completions inverted SCALE-3's answer;
* `_bad_call_was_reported`, after every adversarial check passed on `empty_answer` without once
  looking at the tool;
* the `[[selector]]` assertion, after the mock served the wrong behaviour for a turn's second model
  call and the storm graded the answer anyway;
* `mock_llm._validate`, after LOAD-1 — 100 tool calls reported as "the tool path is genuinely
  exercised" when every one had died in MAF's parse-error branch.

No network, no database, no broker. A test of the harness that needed the stack the harness tests
would only run where the harness already ran.
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
    percentiles,
    report,
    storm,
)
from chemclaw.cli.mock_llm import Behaviour, ToolCall, _validate, already_has_tool_results
from chemclaw.cli.storm_behaviours import BEHAVIOURS


def _row(cap: int, goodput: float) -> dict[str, object]:
    """One admission-sweep row, with only the fields `_knee` reads."""
    return {"cap": cap, "goodput": goodput}


# --------------------------------------------------------------------------- the knee


def test_the_knee_is_the_cap_whose_successor_stops_paying() -> None:
    """The measured SCALE-3 shape: goodput climbs, then flattens.

    These are the real numbers from `docs/archive/storm-2026-08-04.md` — 0.82, 1.01, 1.52, 1.58,
    1.78 answered/s at caps 2 → 32 — so this pins the function against the data whose answer is
    quoted in the record, rather than against a series invented to make it come out right.
    """
    rows = [_row(2, 0.82), _row(4, 1.01), _row(8, 1.52), _row(16, 1.58), _row(32, 1.78)]
    assert _knee(rows) == 8


def test_a_sweep_that_never_flattens_reports_no_knee() -> None:
    """Still improving at the top means the sweep ran out, not that the system did.

    The honest answer there is "we do not know yet", and `None` is how the finding says so — a
    check that returned the top of the range would present a limit of the *measurement* as a
    property of the system.
    """
    assert _knee([_row(2, 1.0), _row(4, 2.0), _row(8, 4.0), _row(16, 8.0)]) is None


def test_a_knee_inside_the_noise_is_still_a_knee_only_at_the_declared_threshold() -> None:
    """Ten percent is the line, and it is drawn on purpose rather than by accident of rounding.

    Just under it counts as flat (the successor bought nothing worth a restart); comfortably over
    it does not. The measurement's own run-to-run spread is a few percent, which is why the
    threshold is well outside it — a knee declared inside the noise is a number with no content.
    """
    assert _knee([_row(2, 1.00), _row(4, 1.09)]) == 2
    assert _knee([_row(2, 1.00), _row(4, 1.20)]) is None


def test_a_single_step_sweep_has_no_successor_to_judge() -> None:
    """One row (or none) cannot show a knee — and must not claim one."""
    assert _knee([_row(8, 1.5)]) is None
    assert _knee([]) is None


# --------------------------------------------------------------------------- coverage honesty


def test_the_report_names_the_families_that_produced_nothing() -> None:
    """The guard against the exact overstatement this harness already made once.

    "17/17 checks passed" was true of what ran and silent about the two families that did not, and
    a pass count can never say otherwise. So the report is asked here for the thing a reader needs:
    planned versus ran, with the missing ones named.
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
    """Both rates are printed, and the report states which one is the measurement.

    Printing `drain` at all is a deliberate choice — it is what the first version reported as
    throughput, and dropping it would hide why the earlier numbers looked the way they did. What
    it must never do is stand unlabelled beside the real one.
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
    """`empty_answer` alone must not count — that was the vacuous pass this predicate replaced.

    Every adversarial behaviour writes no prose, so every one of them produces `empty_answer`. A
    predicate that accepted any error code passed all eight without ever looking at the tool, which
    is a signal reporting success for a reason unrelated to what it claims to measure.
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
    """A message that lost its `[[name]]` would silently run the default behaviour.

    Family H sends the user's own words — unicode, an injection string — because those are what
    Postgres has to survive, so the message is not always the harness's own. The selector is how
    the mock knows which scenario it is in; a turn that dropped it would be graded against a
    behaviour it never ran, which is the drift this assertion exists to make impossible.
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


# --------------------------------------------------------------------------- the mock's own guard


def test_the_catalogue_passes_the_load_1_guard() -> None:
    """Every shipped behaviour is validated against the live tool surface, at import time here.

    This is the check that would have caught LOAD-1 in July: the previous load test sent
    `{"query": ...}` to a tool taking `text`, every call died in MAF's parse-error branch before a
    tool body ran, and the run reported "100 tool calls, the tool path is genuinely exercised".
    Running it over the whole catalogue on a diff means a behaviour cannot rot against a renamed
    parameter and be discovered by a storm three weeks later.
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
    """The runaway-loop guard: a mock that replays its calls forever never lets a turn finish.

    Measured before it existed — the first storm turn made **41** tool calls for a behaviour that
    declares one, because MAF re-invokes the model after every result and the mock answered
    identically each time. Read off the request rather than from per-session state, so concurrent
    turns cannot corrupt each other's counters at the concurrency this harness offers.
    """
    first = {"input": [{"type": "message", "role": "user", "content": "hello"}]}
    after_tools = {"input": [{"type": "function_call_output", "call_id": "c1", "output": "[]"}]}
    assert not already_has_tool_results(first)
    assert already_has_tool_results(after_tools)
    assert not already_has_tool_results({"input": "a bare string"})
    assert not already_has_tool_results({})


def test_every_declared_behaviour_is_reached_by_some_check() -> None:
    """A behaviour nothing drives is a scenario the catalogue advertises and the run never has.

    **This test exists because the claim it enforces was false when it was written.** The
    catalogue's docstring said "every behaviour here is reached by some check in
    `cli/live_storm.py`, and that is enforced rather than intended" — and `a-retrieval` and
    `d-status` were reached by nothing, one round after six other dead behaviours had been the
    finding that started this pass. Confident prose about coverage is exactly what this repository
    has learned not to trust, including its own.

    Checked by reading the harness's source for each name rather than by instrumenting a run,
    because the alternative — noticing during a twenty-five minute live run — is how the previous
    six survived. The names travel as `[[selector]]` strings inside turn messages, so the source is
    genuinely where the reference lives; there is no symbol to resolve.
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
