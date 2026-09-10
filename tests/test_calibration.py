"""Two labs, one compound: what the calibration ledger does with a second measurement.

`030_measurements.sql` keyed `measurements` on `(property, input_hash)` and argued the collapse in
its own comment: *"A re-measurement replaces the row rather than accumulating, because two values
for one property of one molecule is a correction, not two facts."* That is true of a lab correcting
its own typo and false of a replicate, a second solvent system, or a second site — and the `source`
column, the one field that can tell those apart, was written and then used only as the loser's
epitaph.

Measured through the production `record_observation` / `calibration_for` before the fix, one
prediction of −0.30 log S against two equally valid measurements arriving in order:

    lab-basel    reports -0.10   scored=1  n=1  bias=-0.200
    lab-shanghai reports -0.95   scored=1  n=1  bias=+0.650      <- the sign flipped
    measurements: [('solubility_logs', 'h-ethanol', -0.95, 'lab-shanghai')]

`n` reported the honest 1 both times while concealing that a second observation had been taken and
thrown away, and no counter moved. What is pinned here is that both facts survive and that the
figure a chemist reads is the consensus of every source rather than whichever one wrote last.
"""

import asyncio

import pytest

# The submodule form `tests/test_calc_tools.py` uses. `from ...server import tools` is what the
# neighbouring calc tests spell, and under `--strict`'s `no_implicit_reexport` it resolves only
# while some *other* file in the build has already imported the submodule by path — a dependency on
# check order rather than on this file.
import chemclaw.connectors.calc.server.tools as tools
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.calc.calibration import (
    PredictionRecord,
    calibration_for,
    consensus_for,
    record_observation,
    record_prediction,
)
from tests.pg import migrated_db_or_skip


async def _predict(calc_type: str, input_hash: str, value: float, version: str = "v1") -> None:
    """One prediction of `value`, so a later measurement has something to score."""
    await record_prediction(
        PredictionRecord(
            calc_type=calc_type,
            calc_version=version,
            input_hash=input_hash,
            subject="CCO",
            predicted_value=value,
            unit="log S",
        )
    )


async def _observe(calc_type: str, input_hash: str, value: float, source: str) -> int | None:
    """One measured value from one named source."""
    return await record_observation(
        calc_type, input_hash, value, source=source, subject="CCO", unit="log S"
    )


def test_a_second_source_does_not_destroy_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are facts about the molecule; the second is not a correction of the first."""
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> list[tuple[float, str]]:
        await migrated_db_or_skip()
        await _predict("f1-two-labs", "h-two-labs", -0.30)
        await _observe("f1-two-labs", "h-two-labs", -0.10, "lab-basel")
        await _observe("f1-two-labs", "h-two-labs", -0.95, "lab-shanghai")
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT value, source FROM measurements WHERE property = %s ORDER BY source",
                    ("f1-two-labs",),
                )
                return [(value, source) for value, source in await cur.fetchall()]

    assert asyncio.run(_run()) == [(-0.10, "lab-basel"), (-0.95, "lab-shanghai")]


def test_the_reported_bias_is_the_consensus_not_the_last_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number a chemist reads must not flip sign because a second, equally valid one arrived."""
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> list[float]:
        await migrated_db_or_skip()
        await _predict("f1-bias", "h-bias", -0.30)
        biases: list[float] = []
        for source, value in (("lab-basel", -0.10), ("lab-shanghai", -0.95)):
            await _observe("f1-bias", "h-bias", value, source)
            calibration = await calibration_for("f1-bias", "v1", unit="log S")
            assert calibration.bias is not None
            biases.append(calibration.bias)
        return biases

    first, second = asyncio.run(_run())
    # One source: the prediction is scored against it exactly, as it always was.
    assert first == pytest.approx(-0.20)
    # Two: against their mean (−0.525), which is between the two labs rather than at one of them.
    assert second == pytest.approx(0.225)


def test_a_repeat_under_one_source_replaces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`030`'s argument, kept where it is true: a lab correcting itself is one fact, not two."""
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> tuple[int, float]:
        await migrated_db_or_skip()
        await _predict("f1-correction", "h-correction", -0.30)
        await _observe("f1-correction", "h-correction", -9.99, "lab-basel")
        await _observe("f1-correction", "h-correction", -0.10, "lab-basel")
        consensus = await consensus_for("f1-correction", "h-correction")
        assert consensus is not None
        return consensus.sources, consensus.value

    sources, value = asyncio.run(_run())
    assert sources == 1, "one lab revising its own number became two facts"
    assert value == pytest.approx(-0.10)


def test_a_prediction_written_after_two_measurements_scores_against_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measure-then-predict order reconciles against the consensus, not an arbitrary row.

    `_RECONCILE_FROM_MEASUREMENT` joins `predictions` to `measurements`; with two rows for one
    molecule an unaggregated join takes whichever row the planner hands it first, so the scored
    value was not merely stale — it was not determined by anything the caller could see.
    """
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> float:
        await migrated_db_or_skip()
        await _observe("f1-reverse", "h-reverse", -0.10, "lab-basel")
        await _observe("f1-reverse", "h-reverse", -0.95, "lab-shanghai")
        await _predict("f1-reverse", "h-reverse", -0.30)
        calibration = await calibration_for("f1-reverse", "v1", unit="log S")
        assert calibration.bias is not None
        return calibration.bias

    assert asyncio.run(_run()) == pytest.approx(0.225)


def test_the_chemist_is_told_how_many_sources_stand_behind_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`report_measurement`'s reply is the only place a second source can be reported."""
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> tuple[str, str]:
        await migrated_db_or_skip()
        first: str = await tools.report_measurement(
            "solubility", "CCO", -0.10, unit="log S", source="lab-basel"
        )
        second: str = await tools.report_measurement(
            "solubility", "CCO", -0.95, unit="log S", source="lab-shanghai"
        )
        return first, second

    first, second = asyncio.run(_run())
    assert "lab-basel" in first
    # The second reply must name both sources, their spread and the consensus the ledger now
    # scores against — otherwise the chemist cannot tell an accumulation from a replacement.
    assert "lab-shanghai" in second and "lab-basel" in second
    assert "0.85" in second and "-0.52" in second


def test_two_unnamed_chemists_are_told_the_value_did_not_accumulate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collapse survives where the source is not named, so the reply has to say so.

    `report_measurement` files an unnamed measurement under `chemist-reported`, which is one source
    like any other — so two chemists who do not name their labs still write one row, and the second
    still replaces the first. That is the correction case by construction and cannot be told apart
    from a replicate without the chemist naming one. What must not happen is the old reply:
    "Recorded; it reconciled 1 prediction(s)", identical whether a second fact had been stored or
    the first had been deleted.
    """
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> str:
        await migrated_db_or_skip()
        await tools.report_measurement("pka", "CCS", 10.6, unit="pKa")
        # Annotated rather than returned straight through: `@server.tool()` erases the return type.
        reply: str = await tools.report_measurement("pka", "CCS", 10.1, unit="pKa")
        return reply

    second = asyncio.run(_run())
    assert "only pka measurement on file" in second
    assert "replaces this value rather than adding to it" in second
    assert "name a different `source`" in second
