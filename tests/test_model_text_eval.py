"""The model-text evaluation pipeline, end to end on deterministic fakes, and its refusals.

A `--dry-run` drives every stage (offline gate, inventory prefix, runs, metrics, decision, table,
files) with fake answers, grades and spend. Nothing here measures any text: the tests assert the
plumbing, that the verdicts follow the rule, and that a dry run says it is not evidence.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from chemclaw.agent.text_overlay import DIGEST_CHARS, load_overlay
from chemclaw.cli import model_text_eval as cli
from chemclaw.cli.mock_llm import MOCK_BASE_URL
from chemclaw.cli.model_text_inventory import INVENTORY_PATH
from chemclaw.core.config import settings
from chemclaw.evals import delegation_run
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement, Verdict
from chemclaw.evals.model_text import METRIC_NAMES, GradedProbe
from chemclaw.evals.probe import Probe

PROBE = Probe(
    id="p1",
    section=1,
    persona="lab_technician",
    bucket="A",
    question="q",
    direction="d",
    expects_tools=["t"],
)


@pytest.fixture
def no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment of a checkout with no gateway credential: every default."""
    monkeypatch.setattr(settings, "llm_base_url", MOCK_BASE_URL)
    monkeypatch.setattr(settings, "llm_model", "mock")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr(""))


def _dry(tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any], str]:
    """One dry run into `tmp_path`: its exit code, `results.json` and `summary.md`."""
    code = cli.main(["--dry-run", "--skip-offline", "--transcript-dir", str(tmp_path), *extra])
    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    return code, results, (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_without_a_gateway_credential_the_live_arm_is_unreached_and_names_the_three_settings(
    no_gateway: None, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Exit 3, before anything is spent, saying exactly what to set."""
    code = cli.main(["--candidate-url", "http://candidate:8000", "--transcript-dir", str(tmp_path)])
    assert code == cli.EXIT_UNREACHED == 3
    message = capsys.readouterr().err
    for name in cli.CREDENTIAL_SETTINGS:
        assert name in message
    assert not list(tmp_path.iterdir()), "nothing may be written for a run that measured nothing"


def test_credential_gap_reports_only_what_is_missing(
    no_gateway: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each setting is judged on its own, against the defaults that mean "none"."""
    assert cli.credential_gap() == list(cli.CREDENTIAL_SETTINGS)
    monkeypatch.setattr(settings, "llm_base_url", "https://gateway.example/v1")
    assert cli.credential_gap() == ["CHEMCLAW_LLM_MODEL", "CHEMCLAW_LLM_API_KEY"]
    monkeypatch.setattr(settings, "llm_model", "a-real-model")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("k"))
    assert cli.credential_gap() == []


def test_a_dry_run_exercises_the_whole_pipeline_and_says_it_is_not_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every stage runs; every output is labelled; the exit code refuses to read as a result."""
    code, results, summary = _dry(tmp_path, "--sample", "40")
    printed = capsys.readouterr().out
    assert code == cli.EXIT_UNDECIDED
    assert printed.startswith("**NOT EVIDENCE")
    assert summary.startswith("**NOT EVIDENCE")
    assert results["provenance"]["evidence"] is False
    assert len(results["provenance"].keys()) >= 5
    runs = results["runs"]
    assert {arm: len(runs[arm]) for arm in ("control", "candidate")} == {
        "control": 3,
        "candidate": 3,
    }
    assert set(runs["control"][0]) == set(METRIC_NAMES)
    decision = results["decision"]
    assert [m["metric"] for m in decision["metrics"]] == list(METRIC_NAMES)
    assert decision["prefix"]["candidate_tokens"] < decision["prefix"]["control_tokens"]
    assert "per-request prefix (tokens)" in summary
    assert ("**SHIP**" in summary) != ("**NO SHIP**" in summary)


def test_a_dry_run_is_deterministic(tmp_path: Path) -> None:
    """The same arguments give the same numbers, so CI can pin a plumbing change."""
    _, first, _ = _dry(tmp_path / "a", "--sample", "30")
    _, second, _ = _dry(tmp_path / "b", "--sample", "30")
    assert first["runs"] == second["runs"]
    assert first["decision"] == second["decision"]


def test_a_dry_run_with_a_worse_candidate_does_not_ship(tmp_path: Path) -> None:
    """The verdict follows the rule: a candidate that is clearly worse is not shipped."""
    _, results, summary = _dry(tmp_path, "--sample", "60", "--runs", "5", "--dry-run-shift", "-0.3")
    assert results["decision"]["ship"] is False
    failing = [m["metric"] for m in results["decision"]["metrics"] if not m["ok"]]
    assert failing, "a candidate shifted against every metric must fail at least one"
    assert "**NO SHIP**" in summary


def test_a_dry_run_with_a_better_candidate_and_a_smaller_prefix_ships(tmp_path: Path) -> None:
    """The other direction: better on every metric and smaller, so the rule says ship."""
    _, results, summary = _dry(tmp_path, "--sample", "60", "--runs", "5", "--dry-run-shift", "0.3")
    assert results["decision"]["ship"] is True, results["decision"]["reason"]
    assert "**SHIP**" in summary


def test_fewer_than_three_runs_cannot_be_asked_for() -> None:
    """The floor is not a flag's to lower."""
    with pytest.raises(SystemExit):
        cli.main(["--dry-run", "--runs", "2"])


def test_both_arms_may_not_be_the_same_front_door() -> None:
    """Two identical arms would be a control measured against itself."""
    with pytest.raises(SystemExit):
        cli.main(["--control-url", "http://x:8000", "--candidate-url", "http://x:8000"])


def test_a_failed_offline_gate_stops_before_any_run_is_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The offline gate is part of the rule, and cheaper than the live arm."""
    monkeypatch.setattr(
        cli,
        "run_offline_gate",
        lambda: [
            cli.GateResult("eval-strict", True, ""),
            cli.GateResult("prose-contract", False, "x"),
        ],
    )

    async def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a run was asked for after the offline gate failed")

    monkeypatch.setattr(cli, "collect", refuse)
    code = cli.main(["--dry-run", "--transcript-dir", str(tmp_path)])
    assert code == cli.EXIT_NO_SHIP
    printed = capsys.readouterr().out
    assert "**FAIL** `prose-contract`" in printed
    assert "no live run was spent" in printed


def test_the_offline_gate_runs_every_check_even_after_one_fails() -> None:
    """The report shows all three, so a reviewer fixes them in one pass."""
    import subprocess

    seen: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 1 if len(seen) == 1 else 0, "out", "err")

    results = cli.run_offline_gate(fake)
    assert [r.name for r in results] == ["eval-strict", "eval-baseline-check", "prose-contract"]
    assert [r.ok for r in results] == [False, True, True]
    assert seen[0] == ("make", "eval-strict")


def test_collect_alternates_the_arms_and_refuses_a_run_the_judge_did_not_grade() -> None:
    """Arms alternate within each run index; an all-ungraded run is the judge's failure."""
    order: list[tuple[str, int]] = []

    class Driver:
        async def run_once(self, arm: str, run: int, probes: list[Probe]) -> list[GradedProbe]:
            order.append((arm, run))
            outcome = ProbeOutcome(
                probe_id="p1", section=1, persona="lab_technician", bucket="A", question="q"
            )
            verdict: Verdict = "ungraded" if (arm, run) == ("candidate", 2) else "served"
            return [GradedProbe(PROBE, outcome, Judgement(probe_id="p1", verdict=verdict), 1, 1)]

    with pytest.raises(cli.Undecidable, match="candidate run 2"):
        asyncio.run(cli.collect(Driver(), [PROBE], 3))
    assert order == [("control", 1), ("candidate", 1), ("control", 2), ("candidate", 2)]


def test_the_live_driver_joins_what_the_harness_recorded_to_what_was_billed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tokens and cost come from the ledger row of the session each probe opened."""
    seen_urls: list[str | None] = []

    async def fake_run_probes(probes: list[Probe], **kwargs: Any) -> list[ProbeOutcome]:
        seen_urls.append(kwargs["base_url"])
        return [
            ProbeOutcome(
                probe_id="p1",
                section=1,
                persona="lab_technician",
                bucket="A",
                question="q",
                session_id="s1",
                first_calls=2,
                first_call_argument_errors=["t"],
            )
        ]

    async def fake_grade(probes: list[Probe], outcomes: list[ProbeOutcome]) -> dict[str, Judgement]:
        return {"p1": Judgement(probe_id="p1", verdict="served")}

    async def fake_spend(sessions: list[str]) -> dict[str, delegation_run.SessionSpend]:
        assert sessions == ["s1"]
        return {"s1": delegation_run.SessionSpend(tokens=1234, billed=1500)}

    monkeypatch.setattr(cli, "run_probes", fake_run_probes)
    monkeypatch.setattr(cli, "_grade_all", fake_grade)
    monkeypatch.setattr(delegation_run, "spend_by_session_when_booked", fake_spend)
    driver = cli.LiveDriver({"control": "http://c", "candidate": "http://k"}, tmp_path)
    [graded] = asyncio.run(driver.run_once("candidate", 1, [PROBE]))
    assert seen_urls == ["http://k"]
    assert (graded.tokens, graded.billed) == (1234, 1500)
    assert graded.outcome.first_call_argument_errors == ["t"]
    assert graded.judgement is not None and graded.judgement.verdict == "served"


def test_a_front_door_nothing_reached_is_unreached_not_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every probe failing in transport means the run measured nothing: exit 3."""

    async def nothing_arrives(probes: list[Probe], **_: Any) -> list[ProbeOutcome]:
        return [
            ProbeOutcome(
                probe_id=p.id,
                section=1,
                persona="lab_technician",
                bucket="A",
                question="q",
                transport_error="ConnectError: refused",
            )
            for p in probes
        ]

    monkeypatch.setattr(cli, "run_probes", nothing_arrives)
    driver = cli.LiveDriver({"control": "http://c", "candidate": "http://k"}, tmp_path)
    with pytest.raises(cli.Unreached, match="http://c"):
        asyncio.run(driver.run_once("control", 1, [PROBE]))


def test_the_candidate_prefix_is_measured_by_running_a_process_under_the_overlay(
    tmp_path: Path,
) -> None:
    """The overlay is read when the process starts, so the measurement is a real subprocess."""
    (tmp_path / "blocks").mkdir()
    (tmp_path / "blocks" / "0.txt").write_text("Short.", encoding="utf-8")
    shipped = cli.load_inventory(INVENTORY_PATH)
    candidate = cli.inventory_under_overlay(tmp_path)
    assert cli.prefix_tokens(candidate) < cli.prefix_tokens(shipped)
    [row] = [r for r in candidate["entries"] if r["id"] == "block:0"]
    assert row["chars"] < 20, "the overlay's text, in the shipped block's separators"


def test_an_unreadable_inventory_is_undecided_not_a_crash(tmp_path: Path) -> None:
    """A missing or malformed file names itself."""
    with pytest.raises(cli.Undecidable, match="not a readable model-text inventory"):
        cli.load_inventory(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    with pytest.raises(cli.Undecidable):
        cli.load_inventory(broken)


# ---------------------------------------------------------------------------- the arms differ


def _doors(**overlays: str | None) -> httpx.AsyncClient:
    """Front doors answering `/readyz`: each host reports the overlay digest given for it."""

    def handler(request: httpx.Request) -> httpx.Response:
        digest = overlays[request.url.host or ""]
        body: dict[str, Any] = {"status": "ready", "connectors_unhealthy": 0}
        if digest:
            body["model_text_overlay"] = digest
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _overlay_with_digest(root: Path) -> tuple[Path, str]:
    """A real overlay directory and the digest a door running it reports."""
    (root / "blocks").mkdir()
    (root / "blocks" / "0.txt").write_text("Candidate.", encoding="utf-8")
    return root, load_overlay(str(root.resolve())).digest[:DIGEST_CHARS]


def test_arms_that_run_what_they_are_labelled_with_pass(tmp_path: Path) -> None:
    """The control reports no overlay and the candidate reports this overlay's digest."""
    overlay, digest = _overlay_with_digest(tmp_path)
    client = _doors(control=None, candidate=digest)
    asyncio.run(cli.check_arms(client, "http://control:8000", "http://candidate:8000", overlay))


def test_a_candidate_door_started_without_the_overlay_is_a_second_control(tmp_path: Path) -> None:
    """The instrument would read "no effect" for any text, so it refuses before spending a probe."""
    overlay, _ = _overlay_with_digest(tmp_path)
    client = _doors(control=None, candidate=None)
    with pytest.raises(cli.Undecidable, match="reports overlay None"):
        asyncio.run(cli.check_arms(client, "http://control:8000", "http://candidate:8000", overlay))


def test_a_candidate_door_running_a_different_overlay_is_refused(tmp_path: Path) -> None:
    """Same refusal when the digest is another overlay's."""
    overlay, _ = _overlay_with_digest(tmp_path)
    client = _doors(control=None, candidate="0" * DIGEST_CHARS)
    with pytest.raises(cli.Undecidable, match="would not differ as labelled"):
        asyncio.run(cli.check_arms(client, "http://control:8000", "http://candidate:8000", overlay))


def test_a_control_door_running_an_overlay_is_refused() -> None:
    """The control must be the shipped text."""
    client = _doors(control="a" * DIGEST_CHARS, candidate=None)
    with pytest.raises(cli.Undecidable, match="control front door"):
        asyncio.run(cli.check_arms(client, "http://control:8000", "http://candidate:8000", None))


def test_a_door_that_does_not_answer_is_unreached() -> None:
    """No `/readyz` means nothing was measured: exit 3, not a verdict."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(cli.Unreached, match="did not answer /readyz"):
        asyncio.run(cli.check_arms(client, "http://control:8000", "http://candidate:8000", None))


def test_readyz_reports_the_overlay_digest_only_when_one_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real route: absent by default, present (and equal to the loader's digest) under one."""
    from fastapi.testclient import TestClient

    from chemclaw.api import app as service_app
    from chemclaw.connectors.health import ConnectorHealth

    async def healthy() -> list[ConnectorHealth]:
        return []

    monkeypatch.setattr(service_app, "probe_connectors", healthy)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", healthy)
    monkeypatch.setattr(settings, "session_store", "memory")
    overlay, digest = _overlay_with_digest(tmp_path)

    def no_connectors(_profile: str | None = None) -> list[Any]:
        return []

    def readyz() -> dict[str, Any]:
        with TestClient(service_app.create_app(connector_factory=no_connectors)) as client:
            body = client.get("/readyz").json()
        assert isinstance(body, dict)
        return body

    assert "model_text_overlay" not in readyz()
    monkeypatch.setattr(settings, "model_text_overlay_dir", str(overlay))
    assert readyz()["model_text_overlay"] == digest
