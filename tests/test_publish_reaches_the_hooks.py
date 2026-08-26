"""What the *production* call sites publish — not what the projectors can project.

**Why this file exists at all.** `test_publish_projection.py` proves every result shape projects
correctly, and it proved that while the composite half of the system published nothing: it calls
`project()` directly with a `payload_kind` it supplies by hand, which no production call site did.
`test_publish_sql.py` calls `records_from_solvent_screen()` by hand, which nothing called either.
Both suites were green across a seam whose headline claim — "every composite reaches the results
store" — was false for all four shipped jobs.

The error is one level up from `tasks/lessons.md`'s "measure the mechanism, not the outcome":
a projector *is* a mechanism, and testing it is still testing a mechanism I chose rather than the
one something else calls. So every test here starts at a real hook — the envelope a connector job
returns, the row the backfill reads, the payload the cache writes — and asserts what comes out the
far end. A projector that no path can reach fails here and passes there, which is the whole point.
"""

import asyncio
import copy
from typing import Any

import pytest

from chemclaw.durable.connector_job import ConnectorJobResult, job_record_for
from chemclaw.publish.project import PAYLOAD_PROJECTORS, projector_for, records_for
from chemclaw.science.calc.models import (
    Conformer,
    ConformerEnsemble,
    ReactionEnergyResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesEnergy,
    Structure,
    ThermochemistryResult,
    VibrationalMode,
)

# The four connector jobs this repository ships, each with the model its workflow returns. The
# `calc_type` is what the hook builds — `<connector>.<job>` — and is deliberately included so the
# assertion below is about the pair, which is what `projector_for` actually receives.
_SHIPPED_JOBS: tuple[tuple[str, str], ...] = (
    ("calc.compute_reaction_energy", "ReactionEnergyResult"),
    ("calc.compare_solvents", "SolventComparisonResult"),
    ("calc.compute_thermochemistry", "ThermochemistryResult"),
    ("qm.compute_dft_energy", "QMJobResult"),
)


def _structure(z: float = 1.0) -> Structure:
    """A small valid geometry, enough to carry a `structure_id`."""
    return Structure(
        elements=[6, 1, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, z], [0, -1, 0]],
        smiles="CCO",
    )


def _reaction() -> ReactionEnergyResult:
    """A reaction result with a per-species breakdown, as `standard` level produces."""
    return ReactionEnergyResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        level="standard",
        delta_e_kcal=-38.2,
        delta_h_kcal=-36.1,
        delta_g_kcal=-22.4,
        species=[
            SpeciesEnergy(
                smiles="C1CCCCC1",
                role="product",
                multiplicity=1,
                symmetry_number=12,
                electronic_energy_hartree=-38.7,
                enthalpy_hartree=-38.4,
                gibbs_free_energy_hartree=-38.5,
                is_minimum=True,
                was_cached=False,
            )
        ],
        cache_hits=0,
        uncertainty_kcal=3.0,
        is_strongly_exothermic=True,
        exotherm_threshold_kcal=-20.0,
        conformer_treatment="single",
    )


def _thermochemistry() -> ThermochemistryResult:
    """A thermochemistry result carrying vibrational modes.

    Present because `_thermochemistry` is one of the four projectors that reads *list-element*
    fields (`modes[].wavenumber_cm`) and so raised a bare `KeyError` on a partial payload. A
    mutation sweep over reaction shapes alone would have passed against the old narrow guard.
    """
    return ThermochemistryResult(
        smiles="CCO",
        structure_id=_structure().structure_id,
        method="GFN2-xTB",
        solvent="thf",
        temperature_k=298.15,
        pressure_pa=101325.0,
        symmetry_number=1,
        is_minimum=True,
        imaginary_frequencies_cm=[],
        modes=[
            VibrationalMode(wavenumber_cm=412.0, ir_intensity_km_per_mol=1.2),
            VibrationalMode(wavenumber_cm=1050.0, ir_intensity_km_per_mol=8.4),
        ],
        mode_count=2,
        lowest_wavenumbers_cm=[412.0, 1050.0],
        electronic_energy_hartree=-154.2,
        zero_point_energy_kcal=31.2,
        thermal_enthalpy_correction_kcal=2.9,
        entropy_cal_per_mol_k=67.4,
        gibbs_correction_kcal=14.1,
        enthalpy_hartree=-154.1,
        gibbs_free_energy_hartree=-154.15,
        uncertainty_kcal=1.0,
    )


@pytest.mark.parametrize(("calc_type", "payload_kind"), _SHIPPED_JOBS)
def test_every_shipped_job_routes_to_a_projector(calc_type: str, payload_kind: str) -> None:
    """A composite's `calc_type` names a route, so only `payload_kind` can identify its shape.

    This is the assertion whose absence let the seam ship inert. `<connector>.<job>` matches none
    of the projector prefixes — they are `xtb.*`, `dft`, `pka`, `logd`, `solubility`,
    `descriptors` — so before the envelope carried `payload_kind` every one of these resolved to
    `None` and every composite was dropped with a debug line.
    """
    assert projector_for(calc_type) is None, (
        f"{calc_type!r} resolved a projector from its route alone, which means a prefix now "
        "collides with a connector name — the pair below would then be routed by accident"
    )
    assert projector_for(calc_type, payload_kind) is not None, (
        f"{calc_type!r} carrying {payload_kind!r} routes to no projector; its results would be "
        "silently dropped at the enqueue"
    )


def test_the_envelope_carries_the_shape_its_data_came_from() -> None:
    """The hook reads `payload_kind` off the envelope, so the envelope must be able to hold it.

    `data` is `dict[str, Any]` by the time it crosses the Temporal wire, which destroys the model
    identity. This asserts the field exists, defaults to "not said" for histories written before it,
    and survives a round trip through the envelope's own validation.
    """
    assert ConnectorJobResult(summary="x").payload_kind == "", (
        "payload_kind must default empty — every history in flight decodes without it"
    )
    result = _reaction()
    envelope = ConnectorJobResult(
        summary="done",
        data=result.model_dump(mode="json"),
        payload_kind=type(result).__name__,
    )
    assert envelope.payload_kind == "ReactionEnergyResult"
    assert ConnectorJobResult.model_validate(envelope.model_dump()).payload_kind == (
        "ReactionEnergyResult"
    )


def test_the_durable_record_keeps_the_shape_for_the_backfill() -> None:
    """The backfill reads `job_records`, not the envelope, so the row has to carry it too.

    Without this the backfill inferred a projector from `<connector>.<job>` and skipped every
    composite row in the table — reporting them as "unprojectable by this release", which reads
    like a deployment holding results from a retired calculator rather than a bug.
    """
    from chemclaw.durable.connector_job import ConnectorJobInput

    job = ConnectorJobInput(
        connector="calc",
        job="compute_reaction_energy",
        workflow="ReactionEnergyWorkflow",
        task_queue="connector-calc",
        rationale="checking the Diels-Alder driving force",
        requested_by="chemist@example.com",
    )
    result = _reaction()
    envelope = ConnectorJobResult(
        summary="done",
        data=result.model_dump(mode="json"),
        payload_kind=type(result).__name__,
    )
    record = job_record_for("job-1", job, envelope)
    assert record.payload_kind == "ReactionEnergyResult"
    assert projector_for(f"{record.connector}.{record.job}", record.payload_kind) is not None


def test_a_solvent_screen_publishes_its_parts_and_not_only_its_verdict() -> None:
    """`records_for` is what puts the decomposition on the live path.

    "Never store an aggregate whose parts are not also stored" was stated in a docstring, asserted
    in two tests, and reachable from neither hook: all three production call sites went to
    `project()`, which returns the comparison alone. A chemist would then have found
    `best_solvent='toluene'` with no way to ask what ΔG actually was in toluene.
    """
    screen = SolventComparisonResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="dmso", delta_e_kcal=-38.0, delta_h_kcal=-36.0, delta_g_kcal=-24.0
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-40.0, delta_h_kcal=-38.0, delta_g_kcal=-28.9
            ),
        ],
        best_solvent="toluene",
        spread_kcal=4.9,
        uncertainty_kcal=3.0,
    )
    records = records_for(
        calc_ref="screen-1",
        calc_type="calc.compare_solvents",
        payload=screen.model_dump(mode="json"),
        payload_kind="SolventComparisonResult",
    )
    assert len(records) == 3, "the comparison plus one record per solvent it compared"
    parts = records[1:]
    assert [record.conditions.solvent for record in parts] == ["dmso", "toluene"]
    assert all(record.depends_on == ["screen-1"] for record in parts), (
        "every part must edge back to the aggregate, or the verdict cannot be traced to its numbers"
    )
    # And each part is answerable on its own, which is what makes the cross-solvent question work
    # over solvents that were never compared in one call.
    for part in parts:
        assert any(fact.property == "reaction_delta_g" for fact in part.properties)


def test_a_shape_that_does_not_decompose_still_yields_exactly_one_record() -> None:
    """`records_for` is the only entry point, so the ordinary case must go through it unchanged."""
    records = records_for(
        calc_ref="rxn-1",
        calc_type="calc.compute_reaction_energy",
        payload=_reaction().model_dump(mode="json"),
        payload_kind="ReactionEnergyResult",
    )
    assert len(records) == 1


def test_a_repeated_species_gets_its_own_member_and_its_own_row_id() -> None:
    """Listing a species once per equivalent is the tools' convention; the projection honours it.

    Matching each `SpeciesEnergy` to the *first* member with that identity looked harmless: both
    copies carried the same numbers, which is what the equation says. It was not. Member 1 received
    no facts at all, and the two facts for member 0 collided on `value_id` — a content hash over
    `(calc_ref, scope, ordinal, property)` — so the far end's upsert kept one and discarded the
    other. The two energies here differ deliberately, so a collision loses a distinguishable value.
    """
    from chemclaw.publish.dialect import rows_for

    payload: dict[str, Any] = {
        "reactants": ["O", "O"],
        "products": ["OO"],
        "method": "gfn2",
        "temperature_k": 298.15,
        "level": "full",
        "solvent": "water",
        "delta_e_kcal": -5.0,
        "delta_h_kcal": -5.0,
        "delta_g_kcal": -4.0,
        "species": [
            {"smiles": "O", "role": "reactant", "gibbs_free_energy_hartree": -76.4},
            {"smiles": "O", "role": "reactant", "gibbs_free_energy_hartree": -76.5},
        ],
        "warnings": [],
    }
    record = records_for(
        calc_ref="c1",
        calc_type="calc.compute_reaction_energy",
        payload=payload,
        payload_kind="ReactionEnergyResult",
    )[0]
    per_member = [
        (fact.member_ordinal, fact.value)
        for fact in record.properties
        if fact.property == "gibbs_free_energy"
    ]
    assert sorted(per_member) == [(0, -76.4), (1, -76.5)], (
        "each stoichiometric equivalent must claim its own member; both values must survive"
    )
    rows = rows_for(record, tenant_id="t", writer_version="w")["property_value"]
    ids = [row["value_id"] for row in rows]
    assert len(ids) == len(set(ids)), (
        "two facts sharing a value_id means the far end's upsert silently keeps one of them"
    )


def test_an_ensemble_publishes_populations_through_the_same_entry_point() -> None:
    """The conformer case, driven through `records_for` rather than through `project`."""
    members = [
        Conformer(relative_kcal=0.0, population=0.7, degeneracy=1, structure=_structure(1.0)),
        Conformer(relative_kcal=0.9, population=0.3, degeneracy=2, structure=_structure(1.1)),
    ]
    ensemble = ConformerEnsemble(
        smiles="CCO",
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent="thf",
        temperature_k=298.15,
        conformers=members,
        total_found=12,
        conformational_entropy_cal_per_mol_k=1.4,
        ensemble_correction_kcal=-0.4,
    )
    payload = ensemble.model_dump(mode="json")
    # `structure_id` is a derived property, so it is not dumped — the live path injects it in
    # `science/calc/geometry.py` and this mirrors that.
    for dumped, member in zip(payload["conformers"], members, strict=True):
        dumped["structure_id"] = member.structure.structure_id
    records = records_for(
        calc_ref="ens-1",
        calc_type="xtb.conformers",
        payload=payload,
        payload_kind="ConformerEnsemble",
    )
    assert len(records) == 1
    populations = [conformer.population for conformer in records[0].conformers]
    assert populations == [0.7, 0.3], (
        "the populations are the whole reason an ensemble is published"
    )


def test_every_payload_projector_is_reachable_by_some_declared_kind() -> None:
    """A projector nobody can name is dead code that reads like coverage.

    The 17-entry table was entirely unreachable for a release: `payload_kind` won over the prefix
    inference and no production site set it. This asserts the table's keys are exactly what
    `projector_for` will honour, so the *route* stays real even as shapes are added.
    """
    for kind in PAYLOAD_PROJECTORS:
        assert projector_for("nothing.matches.this.prefix", kind) is not None, (
            f"{kind!r} is registered but does not route"
        )


def test_a_partial_payload_never_escapes_the_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every single-field deletion must be absorbed, not raised.

    `enqueue_payload`'s contract is "never raises", and its guard caught `(ProjectionError,
    ValueError)` — which is what a projector raises *deliberately*. Measured by mutating each of
    the shipped shapes, four projectors raise a bare `KeyError` when a field is missing from a list
    element (`modes[].wavenumber_cm`, `atom_charges[].charge`, `sites[].index`,
    `points[].energy_hartree`) and those escaped into the caller.

    A live calculation never hit it, because pydantic had just produced the payload.
    `backfill_cached` walks rows a *different calculator version* wrote, and one aborted the walk —
    breaking the exact property `backfill.py`'s docstring promises.
    """
    from chemclaw.publish import outbox

    # Enabled, but with the write stubbed: this test is about the guard, not the queue.
    monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
    monkeypatch.setattr(outbox, "enqueue", _never_written)

    shapes = {
        "ReactionEnergyResult": _reaction().model_dump(mode="json"),
        "ThermochemistryResult": _thermochemistry().model_dump(mode="json"),
    }
    mutations: list[tuple[str, str, dict[str, Any]]] = []
    for kind, full in shapes.items():
        for key, value in full.items():
            partial = copy.deepcopy(full)
            del partial[key]
            mutations.append((kind, f"{key} removed", partial))
            # Nested removal is the case that mattered: a top-level key vanishing raised
            # `ValueError`, which the old guard caught. A field missing from a *list element* did
            # not, and that is what an older calculator version's rows look like.
            if isinstance(value, list) and value and isinstance(value[0], dict):
                for nested in list(value[0]):
                    deep = copy.deepcopy(full)
                    for item in deep[key]:
                        item.pop(nested, None)
                    mutations.append((kind, f"{key}[].{nested} removed", deep))

    async def _run() -> None:
        for kind, label, partial in mutations:
            written = await outbox.enqueue_payload(
                calc_ref="c1",
                calc_type="calc.compute_thermochemistry",
                payload=partial,
                payload_kind=kind,
            )
            assert written in (0, 1), f"{kind} [{label}]: unexpected write count {written}"

    assert len(mutations) > 30, "the sweep must actually exercise the nested-field case"
    asyncio.run(_run())


async def _never_written(records: Any) -> int:
    """Stand-in for the queue write, so the mutation sweep touches no database."""
    return len(records)


def test_the_shipped_driver_satisfies_the_shipped_sink() -> None:
    """`SqlResultSink` type-checks its driver at runtime, and the one we ship must pass.

    `Warehouse` is `@runtime_checkable`, and such a check tests for the *presence of every member* —
    so a driver missing one is rejected wholesale. `PostgresWarehouse` had no `vector_dialect`
    (it searches nothing, so there was nothing to write) and the sink refused it with "did not
    build a Warehouse". Every delivery failed at the connect, and the 72 green publish tests said
    nothing about it because not one of them built a sink and a driver together.

    Asserted with `isinstance` rather than by listing members, because `isinstance` is literally
    what production runs.
    """
    from chemclaw.ingest.eln.warehouse.driver import Warehouse
    from chemclaw.publish.drivers.postgres import PostgresWarehouse

    driver = PostgresWarehouse(dsn="postgresql://unused/never-connected")
    assert isinstance(driver, Warehouse), (
        "the shipped Postgres driver fails the shipped sink's own runtime check; "
        f"missing: {sorted(set(dir(Warehouse)) - set(dir(driver)) - {'_is_runtime_protocol'})}"
    )
