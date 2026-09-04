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
import json
from pathlib import Path

import httpx
import pytest
import yaml

from chemclaw.agent.chemclaw_agent import available_tool_names
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.evals.live import ProbeOutcome, _score_citations, load_probes, run_probe
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.kg.note import mentioned_ids

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


def _sse(*events: dict[str, object]) -> bytes:
    """Exactly the wire shape the front door emits: one `data:` line per event."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _transport(*events: dict[str, object]) -> httpx.MockTransport:
    """A front door that opens a session and then streams `events` for the turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": "s1"})
        return httpx.Response(200, content=_sse(*events))

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
    """
    surface = available_tool_names()
    unknown = {t for p in load_probes(str(PROBE_DIR)) for t in p.expects_tools if t not in surface}
    assert unknown == set(), f"probes expect tools that do not exist: {sorted(unknown)}"


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

    report = _summary([probe], [outcome], [])

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

    report = _summary([probe], [outcome], [])

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
