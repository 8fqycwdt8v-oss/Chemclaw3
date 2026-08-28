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
    _freed_without_the_lease,
    _knee,
    noise,
    percentiles,
    report,
    storm,
)
from chemclaw.cli.mock_llm import Behaviour, ToolCall, _validate, already_has_tool_results
from chemclaw.cli.storm_behaviours import BEHAVIOURS


def _row(cap: int, goodput: float, spread: float = 0.0, repeats: int = 3) -> dict[str, object]:
    """One admission-sweep row, with only the fields `_knee` and `noise` read.

    `spread` is the within-cap disagreement across that cap's repeated samples, as a fraction of
    its median. `repeats` is how many samples produced it, and the two are not independent: a
    sweep that really measured zero spread took **one** sample, which is the shape this whole
    mechanism exists to stop being read as an answer. So the samples list is synthesised at the
    declared length rather than being a one-element stand-in — the fixture used to say
    `samples=[goodput]` beside a 5 % spread, which is a row no sweep can produce.
    """
    samples = [goodput] * max(repeats, 1)
    if spread and len(samples) > 1:
        samples[0] = goodput * (1 - spread / 2)
        samples[-1] = goodput * (1 + spread / 2)
    return {"cap": cap, "goodput": goodput, "spread": spread, "samples": samples}


# --------------------------------------------------------------------------- the knee


def test_the_knee_is_the_cap_whose_successor_stops_paying() -> None:
    """The measured SCALE-3 shape: goodput climbs, then flattens — against a *measured* floor.

    Real numbers from run 2 in `docs/archive/storm-2026-08-04.md` — 0.82, 1.01, 1.52, 1.58, 1.78
    answered/s at caps 2 → 32 — with a 5 % noise floor. The 8 → 16 step is +3.9 %, inside the
    floor, so the knee is at 8.
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
    """The guard against a *fabricated* knee, which is the failure mode a noise floor introduces.

    Identical goodput series, one number different: a 20 % measured spread instead of 5 %. The
    naive reading — "a step smaller than the spread means it stopped paying" — fires *sooner* as
    noise grows, so at a large enough spread every step qualifies and the knee lands on the first
    pair. A sweep that could not see anything would confidently name the smallest cap.

    This was found by writing the test expecting the opposite and watching it fail, which is worth
    recording: the correction to a fixed threshold (D-2026-08-04-a-plateau-needs-the-noise-you-
    measured-it-with) introduced its own way to be confidently wrong, and only running it said so.

    So `_knee` refuses above `_MAX_READABLE_NOISE`, and None means "we do not know yet" for both
    reasons a sweep can fail to answer: it ran out of range, or it could not see well enough.
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
    """Still improving at the top means the sweep ran out, not that the system did.

    The honest answer there is "we do not know yet", and `None` is how the finding says so — a
    check that returned the top of the range would present a limit of the *measurement* as a
    property of the system.
    """
    assert _knee([_row(2, 1.0), _row(4, 2.0), _row(8, 4.0), _row(16, 8.0)]) is None


def test_the_line_between_flat_and_still_climbing_is_the_measured_spread() -> None:
    """The same two-row sweep answers differently depending on what its noise turned out to be.

    A 9 % step is flat against a 10 % floor and a real climb against a 5 % one. That the answer
    moves with the measurement is the point: the alternative is a constant that is right for
    whichever machine it was chosen on.
    """
    assert _knee([_row(2, 1.00, 0.10), _row(4, 1.09, 0.10)]) == 2
    assert _knee([_row(2, 1.00, 0.05), _row(4, 1.09, 0.05)]) is None


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

    `M` is in the set and is not a scenario family: it is the lane's own check — that every model
    call this run made was served by the mock — emitted whatever `--families` selected. It is
    declared here so it appears in the coverage table like everything else, because the claim it
    carries used to live in the notes where nothing reconciled it.
    """
    assert set(FAMILIES) == set("ABCDEFGHTM")
    assert all(description.strip() for description in FAMILIES.values())


def test_the_lane_scripts_the_chaos_family_drives_exist() -> None:
    """The chaos family shells out to these; a rename would fail only mid-run, minutes in."""
    for script in ("processes.sh", "bootstrap.sh"):
        assert (live_storm._LANE_DIR / script).is_file()


def test_the_note_repo_is_provisioned_before_the_docker_branch_takes_over() -> None:
    """`exec docker compose` never returns, so anything after it runs only without Docker.

    This is a defect that shipped: `ensure_note_repo` sat in the *native* list, below an
    `exec` — so on the branch `bootstrap.sh` itself calls "the right way", the lane came up with
    no dedicated clone, `note_repo_dir` fell back to the working checkout, and the PR-gate refused
    every submission before running a git command. Since job results, reports and distilled
    playbooks all take that gate (D-005), the knowledge-contribution half of a live run was
    unreachable on exactly the machines most likely to run it — and nothing failed loudly.

    Asserted on the ordering rather than on mere presence, because presence is what was already
    true and was not enough. A textual check because the only alternative is starting Docker in
    CI: the invariant is positional, so position is the honest thing to pin.

    Both anchors match the *commands* rather than any prose about them — the first draft searched
    for `exec docker compose`, found that string inside the very comment explaining the fix, and
    failed against the corrected script. A guard that a nearby sentence can move is not a guard.
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


def test_a_connector_tools_wrong_argument_name_is_refused_too() -> None:
    """LOAD-1's shape on the *other* half of the surface, where the guard used to fall through.

    The check resolved a name against `registered_tools()` and skipped anything it did not find,
    commented "an MCP connector tool — its schema lives in the bundle, not in-process". The schema
    does live in the bundle, and the bundle is in this tree: `make template-validate` has resolved
    exactly these signatures out of `connectors/<name>/server/tools.py` since the capability
    migration, to ask the identical question about a template step's arguments. So this call was
    green-lit and would have died in the parse-error branch before `compute_atomic_descriptors`
    ran — the run reporting it, afterwards, as a tool call that happened.
    """
    with pytest.raises(ValueError, match="exactly LOAD-1"):
        _validate(
            Behaviour(
                name="t",
                calls=[ToolCall(tool="compute_atomic_descriptors", arguments={"query": "CCO"})],
            )
        )


def test_the_guard_checks_arguments_for_every_connector_tool_this_tree_can_resolve() -> None:
    """The ratchet: a bundle whose signatures are readable here gets its behaviours checked.

    One wrong-argument call per advertised connector tool, because the one-example test above
    proves the mechanism and this proves the *coverage* — which is what silently regressed. The
    number is the point: before `tool_signatures` was one answer, 22 of the 99 names
    `available_tool_names()` accepts had their arguments checked, and every one of them was
    in-process. A bundle added to this tree now arrives inside the guard rather than beside it.

    Left out, and each for a reason no assertion here could invent: a bundle served from
    `Chemclaw3-mcp` ships no in-tree server module for those bundles, so their signatures
    are not readable at any price; a generated launcher takes one `params` object, whose fields
    `tests/test_storm_behaviour_coverage.py` validates against the model `build_job_tool`
    annotates; and the skills, harness and subagent tools are upstream's.
    """
    from chemclaw.agent.chemclaw_agent import tool_signatures
    from chemclaw.connectors.registry import enabled as enabled_connectors
    from chemclaw.connectors.registry import server_tools_module

    signatures = tool_signatures()
    readable = [
        (manifest.name, tool)
        for manifest in enabled_connectors()
        if manifest.endpoint is not None and server_tools_module(manifest.name) is not None
        for tool in manifest.endpoint.tools
    ]
    assert readable, "no enabled bundle serves an endpoint from this tree — the ratchet is vacuous"
    for connector, tool in readable:
        assert tool in signatures, f"{connector}.{tool} has no resolvable signature"
        with pytest.raises(ValueError, match="exactly LOAD-1"):
            _validate(
                Behaviour(
                    name="t",
                    calls=[ToolCall(tool=tool, arguments={"not_a_parameter_of_this_tool": 1})],
                )
            )


def test_the_mock_and_the_template_gate_read_one_answer_about_arguments() -> None:
    """Two guards against the same failure, resolving names through the same function.

    They diverged once and the divergence was invisible: `make template-validate` checked a
    template step's arguments against a connector tool's real signature while the mock, asking the
    identical question about a scripted model's arguments, skipped the same tool entirely. An
    equality rather than a subset — a second implementation that merely agreed today is what this
    is here to stop.
    """
    from chemclaw.agent.chemclaw_agent import tool_signatures
    from chemclaw.cli.validate_templates import _resolvable_signatures

    assert set(_resolvable_signatures()) == set(tool_signatures())


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
    # Chat completions says the same thing with a `role: "tool"` message. Reading only the
    # Responses shape would run that protocol to the iteration cap — the identical runaway.
    assert already_has_tool_results({"messages": [{"role": "tool", "content": "[]"}]})
    assert not already_has_tool_results({"messages": [{"role": "user", "content": "hello"}]})


def test_the_mock_serves_the_protocol_the_engine_actually_posts_to() -> None:
    """`ChatOpenAI` posts to `/v1/chat/completions`, and for a while the mock served neither.

    The mock was written when the conversation layer ran on the Microsoft Agent Framework, whose
    client resolved to the **Responses** API. The LangGraph rebuild builds a `ChatOpenAI`; nothing
    here followed. From that day every credential-free lane — `make live-degradation`,
    `make live-storm`, `make live-soak` — got a bare `404 Not Found` and every turn died with no
    answer and no tool call, which reads as a system defect rather than a missing route. Measured:
    a degradation run scored 1/3 with "the turn produced no token or answer at all" while the
    mock's own counter read `requests: 0`.

    Asserted against the app's real routing table rather than by calling the handler, because the
    defect was the *absence of a route* — the one thing a handler test cannot see.
    """
    from chemclaw.cli.mock_llm import MockLlm, build_app

    served = {getattr(route, "path", None) for route in build_app(MockLlm(BEHAVIOURS)).routes}
    assert "/v1/chat/completions" in served, sorted(p for p in served if p)
    # Both, not either: the Responses route is still what a deployment on that API would reach,
    # and dropping it would trade one silent 404 for another.
    assert "/v1/responses" in served


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


# --------------------------------------------------------------------- the audit's own do-nothing


def test_a_check_whose_arithmetic_cannot_fail_is_not_a_check() -> None:
    """A1 read `accepted + failed == turns` with `failed` *defined* as `turns - accepted`.

    That is an identity, not an observation: it holds for every conceivable sweep, including one
    where every turn was dropped on the floor. The check's name — "every offered turn is accounted
    for" — describes something a run can fail, so the buckets have to be counted independently.
    """
    results = [
        TurnResult(status=200, answered=True),
        TurnResult(status=429),
        TurnResult(status=200, error_code="empty_answer"),
        TurnResult(status=200, transport_error="ReadError"),
        # 200, no error, no answer: the silent shape. Nothing said what happened to this turn.
        TurnResult(status=200),
    ]
    outcomes = live_storm._turn_outcomes(results)
    assert outcomes == {"accepted": 1, "shed": 1, "errored": 1, "dropped": 1, "silent": 1}
    assert sum(outcomes.values()) == len(results)


def test_goodput_counts_only_turns_that_actually_answered() -> None:
    """The sweep's docstring says "turns that answered, per second"; the code said 200-and-no-error.

    A turn that returned 200, wrote nothing and reported nothing is not goodput, and counting it
    inflates exactly the number SCALE-3 is read off.
    """
    outcomes = live_storm._turn_outcomes(
        [TurnResult(status=200, answered=True), TurnResult(status=200)]
    )
    assert outcomes["accepted"] == 1


def test_a_single_sample_per_cap_cannot_resolve_a_knee() -> None:
    """`--sweep-repeats 1` measures zero spread at every cap, which is not a small noise floor.

    Zero spread makes `_knee` fire on the first pair that fails to improve *at all*, and makes the
    noise check pass with nothing measured — the fabricated knee its own docstring warns about,
    reached by the other door.
    """
    single = [
        {"cap": 2, "goodput": 1.0, "spread": 0.0, "samples": [1.0]},
        {"cap": 4, "goodput": 1.0, "spread": 0.0, "samples": [1.0]},
    ]
    assert live_storm._samples_per_cap(single) == 1
    assert _knee(single) is None


def test_the_knee_observation_says_which_none_it_is() -> None:
    """`no knee in range` and `too noisy to look` are different findings that read identically.

    `_knee` returns None for both, and the finding's observed text asserted the first — so a sweep
    that could not see anything reported the system as still improving at cap 32.
    """
    unreadable = [
        _row(2, 0.82, 0.26, repeats=3),
        _row(4, 1.01, 0.26, repeats=3),
    ]
    assert "unreadable" in live_storm._knee_observation(unreadable)

    climbing = [_row(2, 1.0, 0.05, repeats=3), _row(4, 2.0, 0.05, repeats=3)]
    text = live_storm._knee_observation(climbing)
    assert "unreadable" not in text and "limit of the sweep" in text

    flat = [_row(2, 1.0, 0.05, repeats=3), _row(4, 1.01, 0.05, repeats=3)]
    assert "stops improving at cap 2" in live_storm._knee_observation(flat)


def test_a_turn_that_made_no_tool_call_did_not_survive_forty_of_them() -> None:
    """`_completed_without_dying` passes on a turn with no tool activity whatsoever.

    Every behaviour it graded either answers or reports `empty_answer`, so the predicate was
    satisfied by the mock's own script — "forty parallel calls are survived" would have passed a
    turn in which zero calls were dispatched, and "a 100 KB argument document is survived" a turn
    in which the document never reached a tool.
    """
    silent = TurnResult(status=200, answered=True)
    assert _completed_without_dying(silent)
    assert not live_storm._every_call_came_back(silent, 40)

    flooded = TurnResult(status=200, answered=True, announced=40, returned=40)
    assert live_storm._every_call_came_back(flooded, 40)

    half = TurnResult(status=200, answered=True, announced=40, returned=17)
    assert not live_storm._every_call_came_back(half, 40)


def test_an_empty_turn_is_not_an_upstream_outage_reaching_the_asker() -> None:
    """`f-http-500` writes no prose, so `empty_answer` is guaranteed with or without the outage.

    A predicate accepting any error code therefore passed whether or not the 500 was surfaced —
    the vacuous shape `_bad_call_was_reported` was written to remove, still present one row down.
    """
    swallowed = TurnResult(status=200, error_code="empty_answer")
    assert not live_storm._outage_reached_the_asker(swallowed)
    assert live_storm._outage_reached_the_asker(TurnResult(status=200, error_code="internal"))
    assert live_storm._outage_reached_the_asker(TurnResult(status=503))


def test_the_expected_call_count_is_read_from_the_catalogue_not_from_a_name() -> None:
    """Family C printed "(6 expected)" in a check that only compared announced against returned.

    Five of six calls silently dropped reads as `1/1` and passes. The number is declared in
    `storm_behaviours.BEHAVIOURS`, so the check can read it instead of restating it in prose.
    """
    assert live_storm.declared_calls("c-parallel") == 6
    assert live_storm.declared_calls("c-fragmented") == 1
    assert live_storm.declared_calls("f-call-flood") == 40
    with pytest.raises(KeyError):
        live_storm.declared_calls("no-such-behaviour")


def test_a_tool_body_that_ran_before_this_run_is_not_evidence_about_this_run() -> None:
    """Family B counted `audit_events` rows for all time, so any residue passed it.

    Its own docstring claims the driving turn makes the question "asked of something this run
    actually did rather than of residue an earlier run left in the table" — but an unbounded
    `count(*)` is answered by the residue exactly as before. The delta is the measurement.
    """
    assert live_storm._tool_truth_finding("find_notes", 366, 367).ok
    stale = live_storm._tool_truth_finding("find_notes", 366, 366)
    assert not stale.ok
    assert "0 audited call(s) during this run" in stale.observed


def test_a_mock_that_is_merely_listening_does_not_prove_the_front_door_uses_it() -> None:
    """`_require_mock_lane` pinged port 8820 and concluded no real model would be reached.

    A mock left running by an earlier lane answers that ping while `CHEMCLAW_LLM_BASE_URL` points
    at a real endpoint — the storm then drives hundreds of paid turns having "proved" it would
    not. Only the counter moving in response to a turn this process drove says otherwise.
    """
    assert live_storm._mock_is_serving_the_front_door(4, 6)
    assert not live_storm._mock_is_serving_the_front_door(4, 4)
    # `mock_requests` reports -1 when it could not read the endpoint at all.
    assert not live_storm._mock_is_serving_the_front_door(-1, 6)
    assert not live_storm._mock_is_serving_the_front_door(4, -1)


def test_the_zero_live_model_claim_is_a_finding_rather_than_a_note() -> None:
    """The note `mock requests served: 516` sat there and nothing compared it to anything.

    `MockLlm`'s own docstring says the counter exists because "no LLM calls were made" is a claim
    the storm has to be able to prove, "and reconciling this number against the turn count is
    how". Nothing reconciled it, and a run that drove no turns could print the same note.
    """
    assert live_storm._mock_reconciliation(served=516, turns=310).ok
    short = live_storm._mock_reconciliation(served=4, turns=310)
    assert not short.ok
    assert "310" in short.observed
    # A run that drove nothing has proved nothing, whatever the counter says.
    assert not live_storm._mock_reconciliation(served=516, turns=0).ok


def test_a_family_that_raises_does_not_take_the_whole_run_with_it() -> None:
    """Only the chaos family caught its own exceptions; every other one could end the process.

    A twenty-minute run that dies in family G loses every finding families C, D and F already
    made, and writes no report at all — the run reports nothing rather than reporting what broke.
    """

    async def explodes() -> list[Finding]:
        raise RuntimeError("the front door refused a session")

    findings = asyncio.run(live_storm._run_family("G", explodes))
    assert len(findings) == 1
    assert findings[0].family == "G" and not findings[0].ok
    assert "RuntimeError" in findings[0].observed


def test_p95_over_twenty_samples_is_not_the_maximum() -> None:
    """`int(len * 0.95)` lands one past nearest-rank whenever `len * 0.95` is a whole number.

    At 20 answered turns it reports the slowest turn as the 95th percentile, which is the
    statistic the sweep table publishes beside p50.
    """
    results = [TurnResult(status=200, seconds=float(i)) for i in range(1, 21)]
    _, p95 = percentiles(results)
    assert p95 == 19.0


# ------------------------------------------------------------- the catalogue's unchecked payloads


def test_a_job_payload_the_launcher_would_reject_is_refused_before_the_run() -> None:
    """`_validate` skipped every connector tool, and a durable job's schema is *in this repo*.

    Its comment — "an MCP connector tool: its schema lives in the bundle, not in-process" — is
    true of the 46 endpoint tools and false of the 14 job launchers and 9 template runners, whose
    params models `build_job_tool` and `build_template_tool` generate from manifests here. Those
    payloads are the largest arguments in the catalogue and were the only ones nothing checked.
    """
    bad_job = Behaviour(
        name="t",
        calls=[
            ToolCall(
                tool="compute_reaction_energy",
                arguments={
                    "params": {"kind": "reaction", "reactants": ["N#N"], "level": "nonsense"},
                    "rationale": "storm",
                },
            )
        ],
    )
    with pytest.raises(ValueError, match="params"):
        _validate(bad_job)

    bad_template = Behaviour(
        name="t",
        calls=[ToolCall(tool="run_tautomer_resolution", arguments={"params": {"solvent": 3}})],
    )
    with pytest.raises(ValueError, match="params"):
        _validate(bad_template)


def test_a_missing_required_argument_is_the_same_defect_as_a_misspelled_one() -> None:
    """LOAD-1 was a *wrong* name; an *absent* required name dies in the identical branch.

    `_validate` compared only `set(arguments) - set(annotations)`, so a behaviour that dropped a
    required argument passed the guard and failed at the tool boundary, which is exactly the
    outcome the guard exists to make impossible.
    """
    with pytest.raises(ValueError, match="requires"):
        _validate(Behaviour(name="t", calls=[ToolCall(tool="find_notes", arguments={})]))


def test_a_broker_that_never_stopped_is_the_postgres_defect_one_family_over() -> None:
    """E4 asserted the launch failed without asserting the broker had gone away.

    That is exactly what `bootstrap.sh restart-postgres` turned out to be doing on a Docker lane —
    logging a restart and restarting nothing, while E3 reported PASS with "24/24 in-flight turns
    survived". A fix to one lane primitive is not a reason to keep trusting the sibling check that
    could not have seen it break either.
    """
    refused = TurnResult(status=200, tools_failed=["compute_reaction_energy"])
    assert live_storm._broker_outage_finding(stopped=True, result=refused).ok

    not_stopped = live_storm._broker_outage_finding(stopped=False, result=refused)
    assert not not_stopped.ok
    assert "STILL REACHABLE" in not_stopped.observed


# ------------------------------------------- assertions whose negative half is trivially satisfied


def test_a_truncated_document_check_that_saw_no_call_saw_no_document() -> None:
    """`not any(refusal word in preview)` is trivially true of a turn with no previews at all.

    The `bool(result_previews)` guard in front of it was the whole defence, and it is satisfied by
    a single empty-string preview — which the measured stream really does carry for `find_notes`.
    So "the truncated document was completed and the tool ran on the cut value" was scored by a
    turn in which nothing was announced. The call count is the missing positive half.
    """
    no_call = TurnResult(status=200, result_previews=[""])
    assert not live_storm._partial_document_was_completed(no_call)

    ran = TurnResult(status=200, announced=1, returned=1, result_previews=[""])
    assert live_storm._partial_document_was_completed(ran)

    refused = TurnResult(status=200, announced=1, returned=1, result_previews=["Error: invalid"])
    assert not live_storm._partial_document_was_completed(refused)


def test_a_sweep_that_offered_no_turns_lost_none_of_them() -> None:
    """`lost == 0` is the same trivially-satisfied negative one level up.

    `--sweep-turns 0` produces five rows in which nothing was offered, nothing was dropped, and
    the accounting check passes over zero observations. A count of zero is only evidence when
    something was counted.
    """

    def sweep(turns: int, unaccounted: int) -> list[dict[str, object]]:
        return [{"cap": 2, "turns": turns, "unaccounted": unaccounted, "goodput": 1.0}]

    assert not live_storm._accounting_is_clean(sweep(turns=0, unaccounted=0))
    assert live_storm._accounting_is_clean(sweep(turns=48, unaccounted=0))
    assert not live_storm._accounting_is_clean(sweep(turns=48, unaccounted=3))


def test_a_sweep_where_nothing_answered_has_no_noise_to_be_small() -> None:
    """Every cap at zero goodput measures a spread of zero, so the noise check passes.

    `spread` divides by `max(median, 1e-9)`, so a cap that answered nothing reports a 0 % spread
    rather than an undefined one — and "the sweep's own noise is small enough to read a knee
    against" is then true of a sweep with nothing in it to read.
    """
    dead = [_row(2, 0.0, 0.0, repeats=3), _row(4, 0.0, 0.0, repeats=3)]
    assert not live_storm._sweep_is_readable(dead)
    assert _knee(dead) is None
    assert "unreadable" in live_storm._knee_observation(dead)

    alive = [_row(2, 1.0, 0.05, repeats=3), _row(4, 1.02, 0.05, repeats=3)]
    assert live_storm._sweep_is_readable(alive)


# ------------------------------------------------------- the disconnect verdict (E1)


def test_a_disconnected_session_is_judged_against_the_lease_not_a_guessed_second() -> None:
    """The wait a detached turn costs is not a failure; the wait a lapsed lease costs is.

    `D-2026-08-27-a-disconnect-is-a-detach-not-a-stop` made a client disconnect detach the *view*
    of a turn instead of stopping it, so the session goes on refusing until the turn it is already
    running actually ends. E1's bar was five seconds, written when the stream's `finally` ended the
    turn, and the `[[f-slow]]` behaviour it drives thinks for eight — so the check asserted a design
    this system no longer has. The 2026-08-28 campaign measured 0.2 s, 10.4 s and 25.3 s against a
    60 s lease and reported two of the three as regressions.
    """
    lease = 60.0
    # A turn that outlives the old bar and frees the session well inside the lease: the shipped
    # behaviour, and the case that used to be reported as a failure.
    assert _freed_without_the_lease([409, 409, 409, 200], 10.4, lease)
    # A release that never happened, so the session only freed itself when the claim expired.
    assert not _freed_without_the_lease([409, 200], 61.0, lease)
    # Never refused at all: the probe arrived after the detached turn was over, so this run says
    # nothing about the guard and must not be counted as evidence that it works.
    assert not _freed_without_the_lease([200], 0.2, lease)
    # Still refusing when the probe budget ran out.
    assert not _freed_without_the_lease([409, 409], 59.0, lease)
    assert not _freed_without_the_lease([], 0.0, lease)
