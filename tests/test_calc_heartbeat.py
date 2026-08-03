"""The two CREST jobs heartbeat using the configured xTB heartbeat timeout (REV-3, D-136; Conn-F2).

Every other xTB task reports progress *between* units of work — one species, one solvent, one scan
point — and passes `activity.heartbeat` down as that callback. A CREST search has no unit boundary:
it is a single subprocess. So `run_cached_ensemble`/`run_cached_interaction` take no `progress`
argument, and the only beat these two jobs ever produced was the `"starting {kind}"` line at the
top of the activity.

That is the wrong way round. They are the only two jobs marked `expensive: true`, and their own
manifest says a search's cost "is not bounded by the input's size". Against a 600 s heartbeat
timeout a longer run was declared dead and retried from zero — the store is written only on
completion — so roughly fifty minutes of saturated CPU was spent failing a calculation that would
have succeeded.

The generic heartbeat-while-waiting timer this run wraps is `chemclaw.durable.heartbeat.beating`
(shared with `connectors.bo`, Conn-F2) and is tested once, generically, in
`tests/test_durable_heartbeat.py`. What is specific to this connector — and so tested here — is
the *wiring*: that `run_xtb_calculation` actually routes the ensemble/complex jobs through it with
`settings.xtb_job_heartbeat_timeout_seconds`, end to end through the real activity entry point.
"""

import asyncio
from typing import Any

import pytest

from chemclaw.connectors.calc import activities
from chemclaw.connectors.calc.specs import EnsembleJobSpec
from chemclaw.core.config import settings
from chemclaw.science.calc.conformers import Conformer, ConformerEnsemble
from chemclaw.science.calc.structure import structure_from_smiles


def _fake_ensemble() -> ConformerEnsemble:
    """A minimal, valid ensemble result — standing in for a real CREST search."""
    structure = structure_from_smiles("C")
    conformer = Conformer(relative_kcal=0.0, population=1.0, degeneracy=1, structure=structure)
    return ConformerEnsemble(
        smiles="C",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent=None,
        temperature_k=298.15,
        conformers=[conformer],
        total_found=1,
        conformational_entropy_cal_per_mol_k=0.0,
        ensemble_correction_kcal=0.0,
    )


def test_an_ensemble_job_heartbeats_through_the_configured_xtb_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_xtb_calculation` on an `EnsembleJobSpec` calls the shared timer with the *xTB* setting.

    `run_cached_ensemble` is monkeypatched (CREST is not installed in this sandbox) but
    `chemclaw.durable.heartbeat.beating` is the real, unmodified helper: it is `activity.heartbeat`
    that is stubbed, so a real heartbeat must actually fire through the real call chain, at the
    interval `xtb_job_heartbeat_timeout_seconds` implies — not through a mock recording call args.
    """
    from temporalio import activity

    beats: list[str] = []
    monkeypatch.setattr(settings, "xtb_job_heartbeat_timeout_seconds", 4.0)  # -> 1s beat interval
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _slow_ensemble(*_args: Any, **_kwargs: Any) -> tuple[ConformerEnsemble, bool]:
        await asyncio.sleep(1.3)  # clears the 1s beat interval a 4s timeout implies
        return _fake_ensemble(), False

    monkeypatch.setattr(activities, "run_cached_ensemble", _slow_ensemble)

    spec = EnsembleJobSpec(smiles="C")
    result = asyncio.run(activities.run_xtb_calculation(spec))

    assert result.ensemble is not None
    # The first beat is `run_xtb_calculation`'s own "starting {kind}" line; what proves the
    # shared timer actually ran is a *second* beat from inside it, "still running".
    assert any("still running" in b for b in beats), (
        "a run longer than xtb_job_heartbeat_timeout_seconds produced no timer heartbeat — the "
        f"ensemble job is not routed through the shared heartbeat timer with the xTB setting: "
        f"{beats}"
    )
