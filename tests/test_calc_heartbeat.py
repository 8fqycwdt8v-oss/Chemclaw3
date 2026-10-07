"""Every remote calculation inside a durable job beats the heartbeat (REV-3, D-136).

Every minute of a calc job is spent inside a remote `await`, so each goes through
`chemclaw.durable.heartbeat.beating`; an unbeating call is declared dead and retried. The timer is
tested in `tests/test_durable_heartbeat.py`; this tests the wiring through the real activity entry
point, with only `activity.heartbeat` stubbed.
"""

import asyncio
from typing import Any

import pytest
from temporalio import activity

from chemclaw.connectors.calc import activities
from chemclaw.connectors.calc.specs import EnsembleJobSpec, ReactionJobSpec, XtbJobSpec
from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FakeCalcServer, install


class _SlowServer(FakeCalcServer):
    """A calculation server whose every compute call takes longer than one beat interval."""

    def __init__(self, delay: float, slow: str) -> None:
        """`slow` names the one tool that sleeps, so a test can say which call it is covering."""
        super().__init__()
        self._delay = delay
        self._slow = slow

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Answer as the fake does, after a delay on the tool under test."""
        if name == self._slow:
            await asyncio.sleep(self._delay)
        return await super().call_tool(name, arguments)


def _beats_during(monkeypatch: pytest.MonkeyPatch, spec: XtbJobSpec, slow: str) -> list[str]:
    """Run one job with `slow` taking longer than a beat, and return every heartbeat recorded."""
    beats: list[str] = []
    monkeypatch.setattr(settings, "xtb_job_heartbeat_timeout_seconds", 4.0)  # -> 1 s beat interval
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))
    monkeypatch.setattr(activities, "default_store", lambda: InMemoryStore())
    install(monkeypatch, _SlowServer(1.3, slow))  # clears the 1 s interval a 4 s timeout implies
    asyncio.run(activities.run_xtb_calculation(spec))
    return beats


def test_a_crest_search_beats_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original case: one opaque search, no unit boundary, and it must not go silent."""
    beats = _beats_during(
        monkeypatch, EnsembleJobSpec(smiles="C"), slow="search_conformer_ensemble"
    )
    assert any("still running" in beat for beat in beats), (
        "a search longer than xtb_job_heartbeat_timeout_seconds produced no timer heartbeat — the "
        f"ensemble job is not routed through the shared heartbeat timer: {beats}"
    )


def test_a_remote_hessian_inside_a_reaction_beats_while_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote Hessian inside a reaction beats while it runs.

    Progress is reported between species, which says nothing during one species' Hessian; the timer
    covers that wait.
    """
    spec = ReactionJobSpec(
        reactants=["[H][H]", "ClCl"],
        products=["Cl", "Cl"],
        symmetry_numbers={"[H][H]": 2, "ClCl": 2, "Cl": 1},
    )
    beats = _beats_during(monkeypatch, spec, slow="compute_hessian")
    assert any("still running" in beat for beat in beats), (
        f"a remote Hessian longer than the heartbeat timeout produced no timer heartbeat: {beats}"
    )
    # And the per-species progress line is still there beside it: the two are complementary,
    # "how far" and "alive".
    assert any(beat.startswith("species ") for beat in beats)


def test_no_remote_call_in_the_composites_bypasses_its_runner() -> None:
    """No remote call in the composites bypasses its runner.

    A remote call may run for `calc_server_timeout_seconds`, longer than
    `xtb_job_heartbeat_timeout_seconds`, so any unbeating call lets Temporal retry a job still
    running. Static, because the defect is a call site never written to heartbeat. `remote_version`
    is exempt: a metadata probe no durable job calls.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "src/chemclaw/connectors/calc/compose.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    bare: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)):
                continue
            called = node.value.func
            name = getattr(called, "id", getattr(called, "attr", ""))
            if name in {"remote_call", "cached_remote"}:
                bare.append(f"compose.py:{node.lineno} awaits {name} directly in {function.name}()")

    assert not bare, (
        "a remote call bypasses its `RemoteRunner`, so a durable job holding it reports no "
        "heartbeat while it runs: " + "; ".join(bare)
    )
