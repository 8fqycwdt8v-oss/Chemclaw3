"""Every remote calculation inside a durable job beats the heartbeat (REV-3, D-136; Conn-F2).

Before the split this mattered for two jobs: the CREST searches were a single opaque subprocess with
no unit boundary, and against a 600 s heartbeat timeout a longer run was declared dead and retried
from zero — roughly fifty minutes of saturated CPU spent failing a calculation that would have
succeeded.

After `D-2026-08-16-the-physics-leaves-the-cache-stays` it matters for **all five**, and for a
stronger reason: every minute of every job is now spent inside a remote call. A relaxation, a
Hessian, a scan point and a binding-mode search are each one `await` with nothing finer to report
than "still running", which is precisely the shape `chemclaw.durable.heartbeat.beating` was
extracted for. A blocking call with no heartbeat is an activity Temporal declares dead.

The timer itself is tested once, generically, in `tests/test_durable_heartbeat.py`. What is specific
to this connector — and so tested here — is the **wiring**: that `run_xtb_calculation` actually
routes its remote calls through the timer with `settings.xtb_job_heartbeat_timeout_seconds`, end to
end through the real activity entry point, with `beating` unmodified and only `activity.heartbeat`
stubbed. So a real heartbeat has to fire through the real call chain rather than through a mock
recording its arguments.
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
    """The case the split created, and the one a per-species progress callback does not cover.

    A reaction reports progress *between* species, which is a real boundary and the better signal
    where it exists. It says nothing during the minutes one species' second derivatives take, and
    that wait is now a network call: without the timer underneath it, a single large species
    silently outlives the heartbeat timeout and the whole job is retried from the cache it already
    filled.
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
