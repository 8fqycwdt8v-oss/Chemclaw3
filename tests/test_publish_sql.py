"""The acceptance check: the six chemistry questions, as SQL, against a real database.

**A schema that cannot answer these is not "highly structured", whatever its shape.** So this file
is written as the questions rather than as unit tests of the writer: it loads projected records
into a Postgres running the *shipped* DDL (`schema/result-store/`) and asks what a chemist would.

Two of the six carry a specific trap and each has a negative case here, because without one the
test would pass on a partial answer:

- The THF question must return a run submitted as `tetrahydrofuran`. The calculation layer accepts
  both spellings and passes the name through verbatim, so a schema storing the given name answers
  with a confident subset and raises nothing. The alias table is what makes it pass.
- The cross-solvent question must return solvents that were never compared in one call — which is
  the more common case, and the reason a solvent screen publishes its parts as well as its
  aggregate.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from chemclaw.core.config import settings
from chemclaw.publish.dialect import TABLE_ORDER, rows_for, upsert_statement
from chemclaw.publish.project import project, records_from_solvent_screen
from chemclaw.publish.record import ResultRecord
from chemclaw.science.calc.models import (
    Conformer,
    ConformerEnsemble,
    PkaResult,
    ReactionEnergyResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesEnergy,
    Structure,
)
from tests.pg import migrated_db_or_skip

_DDL = Path(__file__).resolve().parents[1] / "schema" / "result-store" / "001_core.sql"
_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _structure(z: float, smiles: str = "CCO") -> Structure:
    """A small, valid geometry — enough to have a `structure_id`, not meant to be chemistry."""
    return Structure(
        elements=[6, 1, 1, 1, 1],
        positions=[[0, 0, 0], [1, 0, 0], [-1, 0, 0], [0, 1, z], [0, -1, 0]],
        smiles=smiles,
    )


def _reaction(
    *, solvent: str | None, delta_g: float | None, ref: str, method: str = "GFN2-xTB"
) -> ResultRecord:
    """One reaction energy record, so a question can vary exactly one thing across several."""
    result = ReactionEnergyResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method=method,
        solvent=solvent,
        temperature_k=298.15,
        level="standard",
        delta_e_kcal=-38.2,
        delta_h_kcal=-36.1,
        delta_g_kcal=delta_g,
        species=[
            SpeciesEnergy(
                smiles="C=C",
                role="reactant",
                multiplicity=1,
                symmetry_number=4,
                electronic_energy_hartree=-13.2,
                enthalpy_hartree=-13.1,
                gibbs_free_energy_hartree=-13.15,
                is_minimum=True,
                was_cached=True,
                method=method,
            ),
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
                method=method,
            ),
        ],
        cache_hits=1,
        uncertainty_kcal=3.0,
        is_strongly_exothermic=True,
        exotherm_threshold_kcal=-20.0,
        conformer_treatment="single",
    )
    return project(
        calc_ref=ref,
        calc_type="reaction.energy",
        payload=result.model_dump(mode="json"),
        payload_kind="ReactionEnergyResult",
        calc_version=method,
        computed_at=_NOW,
    )


def _ensemble(*, populations: list[float], ref: str, smiles: str = "CCO") -> ResultRecord:
    """A conformer ensemble whose members carry the populations a question filters on."""
    conformers = [
        Conformer(
            relative_kcal=index * 0.4,
            population=population,
            degeneracy=1,
            structure=_structure(1.0 + index * 0.1, smiles),
        )
        for index, population in enumerate(populations)
    ]
    ensemble = ConformerEnsemble(
        smiles=smiles,
        method="GFN2-xTB",
        search="conformers",
        effort="quick",
        solvent="thf",
        temperature_k=298.15,
        conformers=conformers,
        total_found=len(conformers),
        conformational_entropy_cal_per_mol_k=1.4,
        ensemble_correction_kcal=-0.4,
    )
    payload = ensemble.model_dump(mode="json")
    for dumped, member in zip(payload["conformers"], conformers, strict=True):
        dumped["structure"]["structure_id"] = member.structure.structure_id
    return project(
        calc_ref=ref,
        calc_type="xtb.conformers",
        payload=payload,
        payload_kind="ConformerEnsemble",
        computed_at=_NOW,
    )


def _pka(*, value: float, ref: str, smiles: str) -> ResultRecord:
    """One predicted pKa, carrying the uncertainty the question asks to see beside it."""
    result = PkaResult(
        smiles=smiles,
        method="GFN2-xTB",
        pka=value,
        deprotonation_energy_kcal=340.0,
        uncertainty=1.2,
        site="acid",
    )
    return project(
        calc_ref=ref,
        calc_type="pka",
        payload=result.model_dump(mode="json"),
        payload_kind="PkaResult",
        computed_at=_NOW,
    )


async def _load(conn: psycopg.AsyncConnection[Any], records: list[ResultRecord]) -> None:
    """Write records through the same row builder and upserts the SQL driver uses.

    Deliberately not a hand-written INSERT: a test that loaded rows its own way would be asserting
    that a schema *could* answer these questions, not that this system's writer produces rows that
    do. The registry seed goes in first because every fact row references it.
    """
    from chemclaw.publish.properties import REGISTRY
    from chemclaw.publish.solvents import display_name, known_solvents

    for definition in REGISTRY.values():
        await conn.execute(
            "INSERT INTO property_definition (property, dimension, canonical_unit, value_kind, "
            "scope_kind, definition) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                definition.property,
                definition.dimension,
                definition.canonical_unit,
                definition.value_kind,
                definition.scope_kind,
                definition.definition,
            ),
        )
    for canonical, aliases in known_solvents().items():
        await conn.execute(
            "INSERT INTO solvent (solvent_id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (canonical, display_name(canonical)),
        )
        for alias in aliases:
            await conn.execute(
                "INSERT INTO solvent_alias (alias, solvent_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (alias, canonical),
            )
    for record in records:
        rows_by_table = rows_for(record, tenant_id="test", writer_version="test")
        for table in TABLE_ORDER:
            for row in rows_by_table.get(table) or []:
                await conn.execute(
                    upsert_statement(table, tuple(row)),
                    [Jsonb(value) if isinstance(value, dict) else value for value in row.values()],
                )
    await conn.commit()


async def _open_loaded() -> psycopg.AsyncConnection[Any]:
    """A database running the shipped DDL, holding a small corpus of published results.

    The corpus is chosen so every question below has both a match and a near-miss: a reaction that
    is downhill but in the wrong solvent, an ensemble with too few populated conformers, a pKa just
    outside the window. A question that cannot tell those apart is not answering.
    """
    await migrated_db_or_skip()
    conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
    await conn.execute(_DDL.read_text(encoding="utf-8"))
    await conn.commit()

    records: list[ResultRecord] = [
        # The match: downhill, in THF — but submitted under the *other* accepted spelling, which is
        # the trap. Without the alias table this row is invisible to the headline question.
        _reaction(solvent="tetrahydrofuran", delta_g=-22.4, ref="rxn-thf-spelled-long"),
        # Downhill, in THF, spelled the short way. Both must come back as one solvent.
        _reaction(solvent="thf", delta_g=-15.1, ref="rxn-thf-short"),
        # In THF but not downhill enough: excluded by the value predicate, not by the solvent.
        _reaction(solvent="thf", delta_g=-4.2, ref="rxn-thf-shallow"),
        # Downhill, but in the wrong solvent: excluded by the solvent, not by the value.
        _reaction(solvent="acetonitrile", delta_g=-30.0, ref="rxn-mecn"),
        # Downhill in THF, but a different method: excluded by the level of theory.
        _reaction(solvent="thf", delta_g=-25.0, ref="rxn-thf-dft", method="B3LYP"),
        # Ensembles: one with six populated conformers, one with two.
        # Seven members, but the last is below 1% -- so the population filter has something to
        # exclude and the query is not passing trivially on "all of them".
        _ensemble(populations=[0.3, 0.2, 0.15, 0.15, 0.1, 0.098, 0.002], ref="ens-flexible"),
        _ensemble(populations=[0.9, 0.09, 0.005, 0.005], ref="ens-rigid", smiles="C"),
        # pKa: one inside the window, one below it, one above it.
        _pka(value=4.76, ref="pka-acetic", smiles="CC(=O)O"),
        _pka(value=0.7, ref="pka-tfa", smiles="OC(=O)C(F)(F)F"),
        _pka(value=9.9, ref="pka-phenol", smiles="Oc1ccccc1"),
    ]
    # A solvent screen, which publishes its comparison *and* one reaction per solvent it compared.
    screen = SolventComparisonResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="dmso", delta_e_kcal=-38.0, delta_h_kcal=-36.0, delta_g_kcal=-19.9
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-37.5, delta_h_kcal=-35.5, delta_g_kcal=-24.8
            ),
        ],
        best_solvent="toluene",
        spread_kcal=4.9,
        uncertainty_kcal=3.0,
    )
    records += records_from_solvent_screen(
        calc_ref="screen-1",
        payload=screen.model_dump(mode="json"),
        calc_type="reaction.solvent_screen",
        computed_at=_NOW,
    )
    await _load(conn, records)
    return conn


async def _rows(conn: psycopg.AsyncConnection[Any], sql: str, params: Any = ()) -> list[Any]:
    """Run one question and return its rows."""
    cursor = await conn.execute(sql, params)
    return list(await cursor.fetchall())


def test_q1_reactions_below_a_free_energy_in_one_solvent() -> None:
    """Answer: every reaction with delta-G below -10 kcal/mol run in THF at GFN2.

    One index, one table — and the alias table is what makes it complete. `rxn-thf-spelled-long`
    was submitted as `tetrahydrofuran`; a schema that stored the name as given would return three
    rows here, look entirely correct, and be missing a run.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                SELECT pv.calc_ref, pv.value_canonical, pv.uncertainty
                FROM property_value pv
                WHERE pv.property = 'reaction_delta_g'
                  AND pv.value_canonical < -10
                  AND pv.solvent_id = 'thf'
                  AND pv.method = 'GFN2-xTB'
                ORDER BY pv.value_canonical
                """,
            )
            found = {row[0] for row in rows}
            assert found == {"rxn-thf-spelled-long", "rxn-thf-short"}, (
                "the THF question must find the run submitted as 'tetrahydrofuran'; if it does "
                "not, the alias table is not applied and this answers with a subset"
            )
            # The near-misses are excluded for the right reason, each by a different predicate.
            assert "rxn-thf-shallow" not in found  # in THF, not downhill enough
            assert "rxn-mecn" not in found  # downhill, wrong solvent
            assert "rxn-thf-dft" not in found  # downhill in THF, different method
            # The uncertainty is on the value's own row, so a caller cannot read one without it.
            assert all(row[2] is not None for row in rows)

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_q2_ensembles_with_several_populated_conformers() -> None:
    """Answer: every conformer ensemble with more than 5 conformers above 1% population.

    The `count(*) FILTER` is the shape a series table makes possible and an attribute-value store
    does not: the members are rows with an ordering and a population, not a blob to be parsed.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                SELECT c.calc_ref, count(*) FILTER (WHERE c.population > 0.01) AS populated
                FROM conformer c
                GROUP BY c.calc_ref
                HAVING count(*) FILTER (WHERE c.population > 0.01) > 5
                """,
            )
            assert {row[0] for row in rows} == {"ens-flexible"}
            assert rows[0][1] == 6, "six of the seven members are above 1%"

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_q3_predicted_pka_in_a_window_with_its_uncertainty() -> None:
    """Answer: every compound whose predicted pKa is between 4 and 6, with its uncertainty.

    The uncertainty is a column on the value's own row, deliberately. Were it a second property
    row, this query would be a self-join that silently returns the value without its error bar
    whenever the second row is missing — and a semiempirical pKa quoted bare is precisely what the
    result model's own docstring warns against.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                SELECT s.label, pv.value_canonical, pv.uncertainty, pv.value_id
                FROM property_value pv
                JOIN subject s ON s.subject_id = pv.subject_id
                WHERE pv.property = 'pka' AND pv.value_canonical BETWEEN 4 AND 6
                """,
            )
            assert len(rows) == 1, "only acetic acid is inside the window"
            label, value, uncertainty, _ = rows[0]
            assert label == "CC(=O)O"
            assert value == pytest.approx(4.76)
            assert uncertainty == pytest.approx(1.2)

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_q4_everything_resting_on_one_calculation() -> None:
    """Answer: everything that rests on this calculation - asked when one is found wrong.

    A recursive walk over an edge table. This is why lineage is not an array column: the walk goes
    in the *reverse* direction, and no array type indexes that way.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                WITH RECURSIVE dependents (calc_ref, depth) AS (
                    SELECT calc_ref, 1 FROM calculation_input WHERE depends_on_calc_ref = %s
                  UNION ALL
                    SELECT ci.calc_ref, d.depth + 1
                    FROM calculation_input ci
                    JOIN dependents d ON ci.depends_on_calc_ref = d.calc_ref
                    WHERE d.depth < 32
                )
                SELECT DISTINCT calc_ref FROM dependents
                """,
                ("screen-1",),
            )
            assert {row[0] for row in rows} == {"screen-1#solvent0", "screen-1#solvent1"}, (
                "a solvent screen's per-solvent parts must be traceable back to the comparison"
            )

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_q5_every_geometry_for_a_compound_with_its_energy() -> None:
    """Answer: every geometry we hold for this compound, with the energy of each.

    Joins the ensemble's members back to the compound they belong to — which works because a
    conformer's `structure_id` is a real column and the subject's member carries the compound.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                SELECT c.structure_id, c.relative_kcal, c.population
                FROM conformer c
                JOIN calculation cal ON cal.calc_ref = c.calc_ref
                JOIN subject_member sm ON sm.subject_id = cal.subject_id
                JOIN compound cmp ON cmp.compound_id = sm.compound_id
                WHERE cmp.canonical_smiles = 'CCO'
                ORDER BY c.ordinal
                """,
            )
            assert len(rows) == 7, "every member of the ethanol ensemble is addressable"
            assert all(row[0].startswith("st_") for row in rows), "each is a resolvable address"
            assert rows[0][1] == pytest.approx(0.0), "ordinal 0 is the lowest"

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_q6_one_reaction_across_every_solvent_it_was_run_in() -> None:
    """Answer: compare delta-G for this reaction across every solvent we ran it in.

    The payoff of a subject identity that excludes solvent, temperature and method: this is a
    `GROUP BY` on one column rather than a fuzzy join over two text arrays.

    **And it must span solvents that were never compared in one call.** `dmso` and `toluene` come
    from a solvent screen; `thf` and `acetonitrile` from standalone runs. A screen that published
    only its aggregate would leave the first two unanswerable here — which is why publishing an
    aggregate's parts is a rule and not an optimization.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            subject = (
                await _rows(
                    loaded,
                    "SELECT subject_id FROM subject WHERE label = %s",
                    ("C=C.C=CC=C>>C1CCCCC1",),
                )
            )[0][0]
            rows = await _rows(
                loaded,
                """
                SELECT coalesce(sv.display_name, 'gas phase') AS solvent,
                       pv.value_canonical, pv.calc_ref
                FROM property_value pv
                LEFT JOIN solvent sv ON sv.solvent_id = pv.solvent_id
                WHERE pv.subject_id = %s
                  AND pv.property = 'reaction_delta_g'
                  AND pv.method = 'GFN2-xTB'
                ORDER BY pv.value_canonical
                """,
                (subject,),
            )
            solvents = {row[0] for row in rows}
            assert {
                "tetrahydrofuran",
                "acetonitrile",
                "dimethyl sulfoxide",
                "toluene",
            } <= solvents, (
                "the comparison must span both the screened solvents and the standalone runs"
            )
            # All three THF runs land under one solvent despite two different spellings on the
            # way in -- which is the point of canonicalizing at write time rather than at read.
            thf = {row[2] for row in rows if row[0] == "tetrahydrofuran"}
            assert thf == {"rxn-thf-spelled-long", "rxn-thf-short", "rxn-thf-shallow"}

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_a_quick_level_reaction_publishes_no_free_energy() -> None:
    """An absent number stays absent — there is no fallback anywhere in the projector.

    `delta_g_kcal` is None at `quick` level and whenever a species' symmetry number was unstated.
    A projector that substituted `delta_e_kcal` would publish an electronic energy under the name
    of a free energy, and every query above would then be quietly wrong rather than incomplete.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            await _load(loaded, [_reaction(solvent="thf", delta_g=None, ref="rxn-quick")])
            rows = await _rows(
                loaded,
                "SELECT property FROM property_value "
                "WHERE calc_ref = %s AND scope_kind = 'calculation'",
                ("rxn-quick",),
            )
            published = {row[0] for row in rows}
            assert "reaction_delta_e" in published, (
                "the electronic energy was established and is published"
            )
            assert "reaction_delta_g" not in published, (
                "no free energy was established, so none may be published under that name"
            )

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_republishing_the_same_record_is_a_no_op() -> None:
    """Delivery is at-least-once, so writing a record twice must converge rather than duplicate.

    Every primary key in the shipped schema is a content hash precisely so this holds; it is what
    makes the outbox's retry safe.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            before = (await _rows(loaded, "SELECT count(*) FROM property_value"))[0][0]
            await _load(loaded, [_reaction(solvent="thf", delta_g=-15.1, ref="rxn-thf-short")])
            after = (await _rows(loaded, "SELECT count(*) FROM property_value"))[0][0]
            assert after == before, "a redelivered record must not add rows"

        finally:
            await loaded.close()

    asyncio.run(_run())


def test_a_reaction_carries_its_per_species_breakdown() -> None:
    """The breakdown `job_records.result` holds today and nothing can query.

    A reaction's delta-G is a fact about the run (`member_ordinal IS NULL`); each species' absolute
    Gibbs energy is a fact about one member. One table answers both, which is what
    `member_ordinal` being nullable buys.
    """

    async def _run() -> None:
        loaded = await _open_loaded()
        try:
            rows = await _rows(
                loaded,
                """
                SELECT sm.role, sm.smiles, pv.value_canonical
                FROM property_value pv
                JOIN calculation cal ON cal.calc_ref = pv.calc_ref
                JOIN subject_member sm
                  ON sm.subject_id = cal.subject_id AND sm.ordinal = pv.member_ordinal
                WHERE pv.calc_ref = %s AND pv.property = 'gibbs_free_energy'
                ORDER BY sm.ordinal
                """,
                ("rxn-thf-short",),
            )
            assert [(row[0], row[1]) for row in rows] == [
                ("reactant", "C=C"),
                ("product", "C1CCCCC1"),
            ]
            assert rows[0][2] == pytest.approx(-13.15)

        finally:
            await loaded.close()

    asyncio.run(_run())
