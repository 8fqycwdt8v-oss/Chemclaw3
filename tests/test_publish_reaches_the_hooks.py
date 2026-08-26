"""What the *production* call sites publish — not what the projectors can project.

**Why this file exists at all.** `test_publish_projection.py` proves every result shape projects
correctly, and it proved that while the composite half of the system published nothing: it calls
`project()` directly with a `payload_kind` it supplies by hand, which no production call site did.
`test_publish_sql.py` calls `records_from_solvent_screen()` by hand, which nothing called either.
Both suites were green across a seam whose headline claim — "every composite reaches the results
store" — was false for every shipped job.

The error is one level up from `tasks/lessons.md`'s "measure the mechanism, not the outcome":
a projector *is* a mechanism, and testing it is still testing a mechanism I chose rather than the
one something else calls. So every test here starts at a real hook — the envelope a connector job
returns, the row the backfill reads, the payload the cache writes — and asserts what comes out the
far end. A projector that no path can reach fails here and passes there, which is the whole point.

**And this file made the same mistake one level down, which is why it now derives its own inputs.**
Its first version hardcoded four `(calc_type, payload_kind)` pairs "each with the model its
workflow returns". None of the three claims held: the bundles ship eleven jobs, one of the four
named a tool, and the two `calc` pairs named the inner models while the workflow named the
envelope. So it started at a hook it had written down rather than at the hook — and stayed green
while all nine calc jobs published nothing. What it parametrises over now is read from the things
that decide the answer: `XtbJobResult`'s own member fields, the connector manifests, and the fake
that states the calculation server's key contract. A new job, result shape or cache type reaches
these assertions with no edit here, and the two things that genuinely cannot route yet are named
in `_NOT_YET_PUBLISHED` and `_PRIMITIVES_NOT_PUBLISHED` rather than quietly omitted.
"""

import asyncio
import copy
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.workflows import job_envelope
from chemclaw.connectors.registry import discovered
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
from tests.calc_server_fake import _KEYED

# The shapes a durable job can publish, **derived rather than listed**.
#
# This was a hand-written tuple of four `(calc_type, payload_kind)` pairs, and every one of the
# three things it said was wrong. It named four jobs where the bundles ship eleven. One of its
# four, `calc.compute_thermochemistry`, is a *tool* and not a job at all. And it paired the two
# `calc` routes with the inner domain models, while `CalcJobWorkflow` set `payload_kind` from
# `type(result).__name__` on the **envelope** — so the file whose whole premise is "start at a
# real hook" asserted a pairing no hook produced, and all nine calc jobs published nothing while
# it stayed green.
#
# So the list is now read off the two things that decide it: the member fields of `XtbJobResult`
# (a tenth result shape is one field there and appears here with no edit) and `qm`'s own return
# type. A route is not asserted alongside them because a route never identifies a shape — that
# was the original error — and `test_a_route_never_routes_on_its_own` keeps that honest.
_ENVELOPE_MEMBERS: tuple[str, ...] = tuple(
    sorted(
        annotation.__name__
        for field in XtbJobResult.model_fields.values()
        for annotation in get_args(field.annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel)
    )
)

# Shapes that reach a hook and have no projector yet, so this file stays green while saying so out
# loud. Every one is a multi-step result from `D-2026-08-25-the-loop-is-a-composite-not-a-template`
# whose job runs and publishes nothing; PR 2 adds their projectors and empties this set. It is an
# *exclusion list with a deadline*, not a permanent exemption: a shape not named here must route,
# so a tenth member field fails immediately rather than joining the silent four.
_NOT_YET_PUBLISHED: frozenset[str] = frozenset(
    {"RefinedEnsemble", "EnsembleProperty", "SpeciesDistribution", "BondDissociationSurvey"}
)

# What the `qm` bundle returns. Listed rather than derived because it has no envelope to derive
# from — the workflow returns its domain model directly, which is precisely why `qm` was the one
# bundle publishing before this change.
_QM_RESULT = "QMJobResult"

# Every `calc_type` the calculation server stamps on a cache row, read off the fake that states
# its key contract rather than re-listed here. A calculator that gains a cache type appears in this
# parametrisation with no edit — which is the half that was missing when `developability` shipped
# unroutable behind a projector table that said `descriptors`.
_STAMPED_CALC_TYPES: tuple[str, ...] = tuple(
    sorted({calc_type for calc_type, _ in _KEYED.values()})
)

# The one stamped type with no projector, declared for the same reason as `_NOT_YET_PUBLISHED`
# above. A Hessian's scientific value is realised in `ThermochemistryResult`, which is a *tool*
# composite — neither cached nor a job — so publishing frequencies needs a third hook and a
# decision, not a projector. Tracked in `docs/planning/BACKLOG.md`.
_PRIMITIVES_NOT_PUBLISHED: frozenset[str] = frozenset({"xtb.hess"})

# The routes the hooks build, `<connector>.<job>`, read off the manifests so a new job cannot be
# added without this file seeing it.
_JOB_ROUTES: tuple[str, ...] = tuple(
    f"{name}.{job.name}"
    for name, (_directory, manifest) in sorted(discovered().items())
    for job in manifest.jobs
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


@pytest.mark.parametrize("route", _JOB_ROUTES)
def test_a_route_never_routes_on_its_own(route: str) -> None:
    """`<connector>.<job>` names where a result came from, never what shape it is.

    Asserted for every job in every manifest rather than for a chosen few, because the failure it
    guards is a prefix growing until it collides with a connector name — at which point a
    composite would be projected by accident, as whatever calculator owns that prefix.
    """
    assert projector_for(route) is None, (
        f"{route!r} resolved a projector from its route alone: a `_CALC_TYPE_PROJECTORS` prefix "
        "now collides with a connector name, so this job's results would be projected as the "
        "wrong shape rather than by their `payload_kind`"
    )


@pytest.mark.parametrize("payload_kind", _ENVELOPE_MEMBERS)
def test_every_shape_a_calc_job_can_return_routes_to_a_projector(payload_kind: str) -> None:
    """Every member `XtbJobResult` can carry is a shape some job publishes — or admits it cannot.

    This is the assertion whose absence let the seam ship inert, and the reason it is written
    against the envelope's *fields* rather than against a list of jobs: nine jobs share one
    workflow and one envelope, so the envelope's members are the complete set of shapes this
    bundle can hand the publish path, and they cannot drift from it.
    """
    routed = projector_for("calc.any_job", payload_kind) is not None
    if payload_kind in _NOT_YET_PUBLISHED:
        assert not routed, (
            f"{payload_kind!r} now routes to a projector — delete it from `_NOT_YET_PUBLISHED`. "
            "That set is an exclusion with a deadline, and a stale entry is a claim that a shape "
            "is unpublished when it is not"
        )
        return
    assert routed, (
        f"a calc job returning {payload_kind!r} routes to no projector; its results would be "
        "silently dropped at the enqueue. Add one to `PAYLOAD_PROJECTORS`, or — if it genuinely "
        "cannot be published yet — name it in `_NOT_YET_PUBLISHED` so the gap is declared"
    )


@pytest.mark.parametrize("calc_type", _STAMPED_CALC_TYPES)
def test_every_calc_type_the_server_stamps_routes_to_a_projector(calc_type: str) -> None:
    """The primitive twin of the test above, and it failed for the same kind of reason.

    The cache hook (`science/calc/store.py::publish_stored_result`) passes no `payload_kind` — it
    holds an untyped dict from an MCP call and has nothing to name — so a primitive is routed by
    its `calc_type` prefix alone, and that prefix is stamped by the *server*, not chosen here.
    `_CALC_TYPE_PROJECTORS` said `descriptors`; the server has always stamped `developability`, so
    every descriptor panel was dropped at the enqueue while `test_publish_projection.py` exercised
    the spelling nothing emits.

    Parametrised over `calc_server_fake._KEYED` because that map is this repository's statement of
    the server's key contract — the same one `test_calc_remote.py` and the composite suites are
    driven against. If it drifts from the real server, more than this test is wrong.
    """
    if calc_type in _PRIMITIVES_NOT_PUBLISHED:
        assert projector_for(calc_type) is None, (
            f"{calc_type!r} now routes — delete it from `_PRIMITIVES_NOT_PUBLISHED`"
        )
        return
    assert projector_for(calc_type) is not None, (
        f"the server stamps {calc_type!r} and no projector prefix matches it, so every one of "
        "those results is dropped at the enqueue with a debug line"
    )


def test_the_qm_bundle_returns_its_domain_model_and_routes() -> None:
    """`qm` has no envelope: its workflow returns `QMJobResult`, which is why it always published.

    Kept as its own test rather than folded into the parametrisation above, because the thing
    worth asserting is the *difference* — one bundle wraps its result and one does not, and the
    publish path has to work for both.
    """
    assert projector_for("qm.compute_dft_energy", _QM_RESULT) is not None


def test_what_the_calc_workflow_returns_projects_into_records() -> None:
    """The whole hook, through the function `CalcJobWorkflow.run` itself calls.

    Every other test in this file starts one step *after* the workflow — it builds the envelope by
    hand — and that is exactly the gap the seam shipped through: a hand-built envelope carried
    `ReactionEnergyResult` while the workflow's own `type(result).__name__` carried `XtbJobResult`,
    which routes nowhere. Measured on the shipped code, the production pair queued 0 rows where the
    hand-built one queued 1.

    So this calls `job_envelope`, which is what the workflow calls — not a copy of its body, which
    is the mistake one level up. Asserting it without a worker is sound because the function is
    pure: the workflow applies it in workflow code, where a replay must produce byte-identical
    output from an activity result already in history.
    """
    envelope = job_envelope(
        XtbJobResult(kind="reaction", summary="ΔE = -38.2 kcal/mol", reaction=_reaction())
    )

    assert envelope.payload_kind == "ReactionEnergyResult", (
        "the workflow must name the shape it computed, not the envelope it wrapped it in"
    )
    assert "reaction" not in envelope.data, (
        "`data` must be the domain result itself; a wrapper key here means the science is a level "
        "down and `payload_kind` is naming the wrapper"
    )
    records = records_for(
        calc_ref="calc-job-1",
        calc_type="calc.compute_reaction_energy",
        payload=envelope.data,
        payload_kind=envelope.payload_kind,
    )
    assert records, "what the production hook returns projected nothing"
    assert records[0].subject.members, "the projected record names no species"


def test_an_envelope_carrying_no_result_is_a_loud_failure() -> None:
    """A job that produced nothing must not report success with a `kind` describing an absence.

    The alternative — returning the bookkeeping fields alone — is what the publish path used to
    receive, and it is indistinguishable at the far end from a result this release cannot read.
    """
    with pytest.raises(ValueError, match="carried 0"):
        XtbJobResult(kind="reaction", summary="nothing ran").outcome()


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
