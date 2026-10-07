"""The property registry is coherent and not quietly fragmenting.

A foreign key ensures a property is defined, not that it is the only definition of its quantity;
synonyms would make every query return a confident subset. These tests fail on unconvertible
units, on values written under a forbidden kind, and on two same-dimension properties landing on
one subject, which is what a split looks like.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.publish import project as projection
from chemclaw.publish.properties import (
    REGISTRY,
    UNIT_CONVERSIONS,
    UnknownPropertyError,
    definition_for,
    to_canonical,
)
from chemclaw.publish.record import (
    Conditions,
    PropertyFact,
    Subject,
    SubjectMember,
    TheoryLevel,
)


def test_every_canonical_unit_is_reachable_within_its_dimension() -> None:
    """Properties sharing a dimension agree on a unit or are convertible to one.

    So a query cannot add hartree to kilocalories.
    """
    by_dimension: dict[str, set[str]] = defaultdict(set)
    for definition in REGISTRY.values():
        by_dimension[definition.dimension].add(definition.canonical_unit)

    for dimension, units in sorted(by_dimension.items()):
        if len(units) == 1:
            continue
        # More than one unit under one dimension is allowed only if every pair converts.
        for source in units:
            for target in units:
                assert source == target or (source, target) in UNIT_CONVERSIONS, (
                    f"dimension {dimension!r} registers both {source!r} and {target!r}, but "
                    f"`UNIT_CONVERSIONS` has no path between them — a query over this dimension "
                    "would be comparing incommensurable numbers"
                )


def test_a_dimensionless_property_declares_no_unit() -> None:
    """A unit on a dimensionless quantity is a contradiction that would mislead a reader."""
    for definition in REGISTRY.values():
        if definition.dimension in {"dimensionless", "flag", "category", "count", "similarity"}:
            assert definition.canonical_unit == "", (
                f"{definition.property!r} is {definition.dimension} but declares unit "
                f"{definition.canonical_unit!r}"
            )


def test_an_unregistered_property_is_refused_rather_than_stored() -> None:
    """The registry refuses a name it does not know, naming the fix.

    A value stored under an unregistered name is a value no query will find, so it must be a loud
    failure at write time rather than a row that looks stored.
    """
    with pytest.raises(UnknownPropertyError) as caught:
        definition_for("pKa")
    assert "_DEFINITIONS" in str(caught.value), "the message must say where to add it"


def test_a_unit_with_no_conversion_path_is_refused_rather_than_passed_through() -> None:
    """Canonicalization refuses a unit it cannot convert.

    Passing it through is exactly how a mis-tagged row falls silently out of a range filter: the
    number is stored, the column says it is canonical, and nothing raises.
    """
    with pytest.raises(UnknownPropertyError):
        to_canonical("reaction_delta_g", 1.0, "furlongs")


def test_hartree_converts_to_kilocalories_correctly() -> None:
    """The one conversion the whole schema rests on, checked against the known constant."""
    assert to_canonical("reaction_delta_g", -0.02, "hartree") == pytest.approx(-12.5502, abs=1e-3)
    # A value already in the canonical unit passes through untouched.
    assert to_canonical("reaction_delta_g", -12.5, "kcal/mol") == -12.5


def _projected_properties_by_subject() -> dict[str, set[str]]:
    """Every property each projector emits, keyed by the subject kind it emits them for.

    Derived from the projectors themselves rather than listed here, so a new calculator is covered
    by this check the day it ships rather than the day someone remembers to add it.
    """
    from tests.test_publish_projection import _cases

    found: dict[str, set[str]] = defaultdict(set)
    for kind, calc_type, _, payload in _cases():
        record = projection.project(
            calc_ref=f"{calc_type}@v:a:b",
            calc_type=calc_type,
            payload=payload,
            payload_kind=kind,
        )
        names = {fact.property for fact in record.properties}
        names |= {fact.property for fact in record.sites}
        names |= {fact.property for fact in record.points}
        found[record.subject.kind] |= names
    return found


def test_no_two_properties_of_one_dimension_land_on_the_same_subject() -> None:
    """No two properties of one dimension land on the same subject.

    The only automatic signal that the registry has two names for one quantity. A short, reasoned
    exemption list covers dimensions with genuinely distinct quantities (enthalpy and Gibbs energy;
    reaction delta-E, delta-H and delta-G).
    """
    # Dimensions that carry several genuinely distinct quantities per subject, with why.
    exempt = {
        "energy": "an absolute energy, an enthalpy and a Gibbs energy are three quantities",
        "energy_difference": "a reaction establishes delta-E, delta-H and delta-G together",
        "orbital_energy": "HOMO, LUMO and the gap between them are three readings",
        "count": "a molecule has many independent counts (donors, acceptors, rings)",
        "molar_entropy": "total entropy and the conformational part of it are different terms",
        "fukui": "the three indices describe three different attacks on the same atom",
        "polarisability": (
            "an atom has a static polarisability and a dispersion coefficient; the two are related "
            "by the model that produces them and are not two spellings of one number"
        ),
        "surface_potential": (
            "a surface has a most-positive and a most-negative extremum, which are two readings"
        ),
        "conceptual_dft": (
            "a molecule has an ionization potential, an electron affinity, a chemical potential, "
            "a hardness and an electrophilicity index at once — they are five readings of one "
            "electronic structure, related by definition rather than spellings of one quantity"
        ),
        "softness": (
            "global softness is a molecular property and local softness is that value partitioned "
            "onto an atom; f-plus and f-minus partition it two ways, so three coexist by design"
        ),
        "category": "several independent coded facts describe one run",
        "flag": "several independent booleans describe one run",
        "log_unit": "clogp, log_d and pka are different measurements on one molecule",
        "dimensionless": "unrelated normalized quantities share this dimension by definition",
    }
    for subject_kind, names in sorted(_projected_properties_by_subject().items()):
        by_dimension: dict[str, set[str]] = defaultdict(set)
        for name in names:
            by_dimension[REGISTRY[name].dimension].add(name)
        for dimension, sharing in sorted(by_dimension.items()):
            if len(sharing) < 2 or dimension in exempt:
                continue
            pytest.fail(
                f"subject kind {subject_kind!r} carries {sorted(sharing)}, which all share "
                f"dimension {dimension!r}. Either they are one quantity under two names — a "
                "registry split, and the thing this test exists to catch — or the dimension needs "
                "an entry in `exempt` saying why several coexist."
            )


def test_every_exempted_dimension_is_actually_used() -> None:
    """An exemption that no longer applies is a hole nobody is watching.

    Same rule the deferral register follows: a reason that has outlived its subject is deleted, not
    left standing.
    """
    registered = {definition.dimension for definition in REGISTRY.values()}
    exempt = {
        "energy",
        "energy_difference",
        "orbital_energy",
        "count",
        "molar_entropy",
        "fukui",
        "category",
        "flag",
        "log_unit",
        "dimensionless",
    }
    assert exempt <= registered, (
        f"exempted dimension(s) {sorted(exempt - registered)} are no longer registered by any "
        "property; delete the exemption rather than leaving it standing"
    )


# --- what `make sink-validate` catches about a `connection:` block -------------------------------


def test_the_sink_gate_checks_the_block_against_the_driver_and_its_env_names() -> None:
    """The sink gate checks the `connection:` block against the driver and its `*_env` names.

    The block has no model by design (`D-2026-08-26-the-driver-s-signature-is-the-schema`), so the
    gate is the only check before a delivery. A `*_env` holding a value instead of a variable name
    would reach the driver as an unset credential.
    """
    from chemclaw.cli.validate_sinks import _driver_problems
    from chemclaw.publish.manifest import ResultSinkManifest

    def _manifest(**connection: object) -> ResultSinkManifest:
        return ResultSinkManifest(
            name="results",
            description="a results database this deployment runs itself",
            driver="chemclaw.publish.drivers.sql:SqlResultSink",
            config={
                "connection": {
                    "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
                    "host": "chemclaw-results",
                    "database": "chemclaw_results",
                    **connection,
                }
            },
        )

    assert _driver_problems(_manifest(password_env="RESULTS_DB_PASSWORD")) == []

    pasted = _driver_problems(_manifest(password_env="hunter2"))
    assert pasted and "NAME of an environment variable" in pasted[0], pasted

    unknown = _driver_problems(_manifest(role="READER"))
    assert unknown and "role" in unknown[0], unknown


def test_the_sink_gate_checks_every_discovered_sink_not_only_the_enabled_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink gate checks every discovered sink, not only the enabled ones.

    `CHEMCLAW_RESULT_SINKS` is empty by default and in CI, so gating only enabled sinks checks
    nothing. A sink broken while disabled is one nobody can enable.
    """
    from chemclaw.cli.validate_sinks import problems
    from chemclaw.publish.registry import discovered

    broken = tmp_path / "postgres"
    broken.mkdir()
    (broken / "sink.yaml").write_text(
        "name: postgres\n"
        "description: a sink whose driver class was renamed out from under it\n"
        "driver: chemclaw.publish.drivers.sql:NoSuchClassAtAll\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "result_sinks_dir", str(broken.parent))
    monkeypatch.setattr(settings, "result_sinks", "")  # the shipped default: nothing enabled
    discovered.cache_clear()
    try:
        found = problems()
    finally:
        discovered.cache_clear()
    assert found and "NoSuchClassAtAll" in found[0], found


def test_a_quantity_registered_for_another_table_cannot_be_projected_as_a_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quantity registered for another table cannot be projected as a scalar.

    `scope_kind` is enforced in `project`: `calculation` covers both scalar row scopes, so
    per-member reaction facts are fine. Not a `PropertyFact` validator, because that would also run
    when parsing already-queued documents and retire stored rows. Fails both with the guard removed
    and with it moved onto the model.
    """
    conformer_scoped = next(
        name for name, definition in REGISTRY.items() if definition.scope_kind == "conformer"
    )

    def _misplacing(_payload: dict[str, Any]) -> tuple[Any, Any, Any, dict[str, Any]]:
        """A projector that files a per-conformer quantity in the scalar table."""
        return (
            Subject(
                kind="molecule",
                members=[SubjectMember(ordinal=0, role="subject", smiles="CCO")],
                label="CCO",
            ),
            Conditions(),
            TheoryLevel(method="GFN2-xTB"),
            {"properties": [PropertyFact(property=conformer_scoped, value=1.0, unit="kcal/mol")]},
        )

    monkeypatch.setitem(projection.PAYLOAD_PROJECTORS, "MisplacingResult", _misplacing)
    with pytest.raises(projection.ProjectionError, match="belong in that table"):
        projection.project(
            calc_ref="misplaced@1:a:b",
            calc_type="probe",
            payload={},
            payload_kind="MisplacingResult",
        )


def test_every_projected_scalar_is_registered_for_the_scalar_table() -> None:
    """Every projected scalar is registered for the scalar table.

    Checked across every shape this system produces.
    """
    from tests.test_publish_projection import _cases

    for kind, calc_type, _model, payload in _cases():
        record = projection.project(
            calc_ref=f"{calc_type}@v:a:b", calc_type=calc_type, payload=payload, payload_kind=kind
        )
        for fact in record.properties:
            assert definition_for(fact.property).scope_kind == "calculation", (
                f"{kind} publishes {fact.property!r} as a scalar, but the registry declares it at "
                f"{definition_for(fact.property).scope_kind!r} scope"
            )
        for site in record.sites:
            assert definition_for(site.property).scope_kind == "site"
        for point in record.points:
            assert definition_for(point.property).scope_kind == "point"
        # Candidates too: `score_property` is a foreign key into the same registry, and must name a
        # property registered for this placement.
        for candidate in record.candidates:
            if not candidate.score_property:
                continue
            assert definition_for(candidate.score_property).scope_kind == "candidate", (
                f"{kind} scores a candidate with {candidate.score_property!r}, which the registry "
                f"declares at {definition_for(candidate.score_property).scope_kind!r} scope"
            )
