"""The property registry is coherent, and it is not quietly fragmenting.

**The registry is the one thing standing between this design and EAV.** A foreign key guarantees a
property is *defined*; it does not guarantee it is the *only* definition of that quantity. Three
teams shipping `pka`, `pka_acid` and `pka_conjugate_acid` would each pass the constraint, and every
query would then return a confident subset with nothing raising — which is exactly the failure the
registry was chosen to avoid.

Only review can prevent a synonym being registered. What a test can do is narrow the gap, and these
do: they fail on a registered unit that cannot be converted, on a value written under a kind its
definition forbids, and — the one that matters most — on two properties that share a dimension and
land on the same subject, which is what a split looks like from the outside.
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
    """Properties sharing a dimension must agree on a unit, or be convertible to one.

    The check that catches a row shipped with `kcal/mol` under `molar_entropy`. Without it, two
    energies could be registered in different units under one dimension and a query summing them
    would be adding hartree to kilocalories — a mistake that produces a plausible number.
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
    """The fragmentation check: what a split property looks like from the outside.

    If `pka` and `pka_acid` both existed and both described a molecule, this fails — which is the
    only automatic signal available that the registry has grown two names for one quantity.

    **Dimensions where several distinct quantities legitimately coexist are exempted by name**, and
    the exemption list is deliberately short and reasoned: a thermochemistry genuinely establishes
    an enthalpy *and* a Gibbs energy, and a reaction genuinely has a delta-E, a delta-H and a
    delta-G. Those are different quantities, not two spellings of one.
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
    """Both halves of "the driver's signature is the schema", on the outbound seam.

    A sink's `connection:` block has no model behind it — by design
    (`D-2026-08-26-the-driver-s-signature-is-the-schema`) — so the gate is the only place either
    mistake is caught before a publish attempt makes it. The `*_env` check matters as much as the
    signature one and used to run on neither side: a key holding a *value* rather than a variable
    name, or a lower-case variable, reaches the driver as an unset credential and fails on the first
    delivery, hours after the deploy.
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
    """The gate ran on the enabled list, which is empty by default — so it checked nothing at all.

    `CHEMCLAW_RESULT_SINKS` is empty on the shipped configuration and in CI (the baseline run says
    "1 discovered, 0 enabled"), and `problems()` reached `_driver_problems` only for enabled names.
    Zero drivers were resolved, zero config blocks bound, zero `*_env` names checked, on every
    release — a rename in `publish/drivers/sql.py` would have been green through all of them and
    failed on the first deployment that turned publishing on, in a worker, against a database a DBA
    had already provisioned.

    Discovery is what the two sibling seams validate, and for the reason they both write down: a
    sink that is broken while disabled is a sink nobody can enable, and CI is where that surfaces.
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
    """`scope_kind` is a control, not a comment — the claim its own definition makes.

    Nothing compared a fact's property against its declaration: not the projectors, not the row
    builder, not the SQL driver. So the `property_definition` rows a site ships — the table a
    consumer joins to decide *where to look* for a quantity — asserted a placement nothing kept.
    Measured, one shipped projection disagreed with it: every species distribution wrote
    `relative_energy`, registered per-conformer, as a calculation-scope scalar.

    `calculation` names the scalar table and covers both of its row scopes, which is why the seven
    per-species facts a reaction publishes at `member` scope are not violations — a species' own
    Gibbs energy is a `property_value` row, and `FactScope` on the row says which kind.

    **Driven through `project`, because the check is the projection's and not the model's.** As a
    `PropertyFact` validator it also ran on the parse of a document already queued — see
    `tests/test_publish_outbox.py::test_a_document_this_system_already_queued_stays_readable` —
    which turned a write-side control into a filter that retired stored rows. This test fails in
    both directions: with the guard gone the fabricated projector's record is built, and with the
    guard back on the model the fact cannot be constructed and the raise is the wrong type.
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
    """The same rule over every shape this system produces, not only the one that broke it.

    The validator above cannot be reached by a projector that never runs in a unit test, so this
    drives all of them — a new calculator writing a per-atom quantity into the scalar table fails
    here the day it ships.
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
