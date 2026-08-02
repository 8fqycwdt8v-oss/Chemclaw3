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
from chemclaw.evals.live import ProbeOutcome, _score_citations, load_probes, run_probe
from chemclaw.evals.probe import Probe, ProbeSet

PROBE_DIR = Path(__file__).resolve().parent.parent / "data" / "evals" / "probes"


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
    previews = ["matched note rxn-suzuki-biaryl (confidence 0.8)"]
    assert _score_citations("see [[rxn-suzuki-biaryl]]", previews) == []
    assert _score_citations("see [[evidence-for:rxn-suzuki-biaryl]]", previews) == []
    assert _score_citations("see [[rxn-never-retrieved]]", previews) == ["rxn-never-retrieved"]


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
