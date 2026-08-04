"""The ingest half, proven against a fake warehouse — no tenant, no driver, no credentials.

These tests are the evidence for the claim the package is built on: that attaching a warehouse ELN
is writing a binding rather than writing Python. So they assert the two things that claim reduces
to — that the *statement* the engine sends is the one the sync's contract requires, and that a
schema change is a change to YAML and to nothing else.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from chemclaw.ingest.eln.adapter import ElnMappingError
from chemclaw.ingest.eln.ord import Role
from chemclaw.ingest.eln.warehouse.adapter import WarehouseElnAdapter
from tests import warehouse_fake

_DRIVER = "tests.warehouse_fake:open_fake"

_CREATED = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def _binding(**overrides: Any) -> dict[str, Any]:
    """A minimal, valid binding over a two-table ELN; `overrides` replace whole sections."""
    binding: dict[str, Any] = {
        "connection": {"driver": _DRIVER},
        "ingest": {
            "entry": {
                "relation": "V_REACTION",
                "key": "REACTION_ID",
                "created_at": "CREATED_TS",
                "modified_at": "LAST_MODIFIED_TS",
            },
            "related": [
                {
                    "name": "charges",
                    "relation": "V_CHARGE",
                    "foreign_key": "REACTION_ID",
                    "order_by": "CHARGE_SEQ",
                }
            ],
            "reaction": {
                "reaction_id": {"path": "root.REACTION_ID"},
                "project": {"path": "root.PROJECT_CODE"},
                "yield_percent": {"path": "root.YIELD_PCT", "transform": [{"number": {}}]},
                "time_h": {
                    "path": "root.DURATION_MIN",
                    "transform": [{"number": {}}, {"scale": {"factor": 1 / 60}}],
                },
            },
            "components": [
                {
                    "from": "charges",
                    "smiles": {"path": "SMILES_STRUCTURE"},
                    "role": {
                        "path": "MATERIAL_TYPE",
                        "transform": [
                            {
                                "value_map": {
                                    "map": {
                                        "SM": "reactant",
                                        "SOLV": "solvent",
                                        "PROD": "product",
                                    }
                                }
                            }
                        ],
                    },
                    "mass_mg": {
                        "path": "AMOUNT_G",
                        "transform": [{"number": {}}, {"scale": {"factor": 1000}}],
                    },
                    "attributes": ["LOT_NUMBER"],
                }
            ],
            "provenance": "eln-test:${root.REACTION_ID}:${root.OPERATOR}",
        },
    }
    binding.update(overrides)
    return binding


def _rows() -> dict[str, list[dict[str, Any]]]:
    """One complete reaction: a header row and three charge rows."""
    return {
        "V_REACTION": [
            {
                "REACTION_ID": "RX-1",
                "CREATED_TS": _CREATED,
                "LAST_MODIFIED_TS": None,
                "PROJECT_CODE": "PRJ-7",
                "YIELD_PCT": "82.5",
                "DURATION_MIN": "90",
                "OPERATOR": "a.chemist",
                "VESSEL_ID": "V-12",
                "NOTEBOOK_PAGE": "44",
            }
        ],
        "V_CHARGE": [
            {
                "REACTION_ID": "RX-1",
                "CHARGE_SEQ": 1,
                "SMILES_STRUCTURE": "CC(=O)O",
                "MATERIAL_TYPE": "SM",
                "AMOUNT_G": "1.25",
                "LOT_NUMBER": "L-991",
            },
            {
                "REACTION_ID": "RX-1",
                "CHARGE_SEQ": 2,
                "SMILES_STRUCTURE": "CCO",
                "MATERIAL_TYPE": "SOLV",
                "AMOUNT_G": "20",
                "LOT_NUMBER": None,
            },
            {
                "REACTION_ID": "RX-1",
                "CHARGE_SEQ": 3,
                "SMILES_STRUCTURE": "CCOC(C)=O",
                "MATERIAL_TYPE": "PROD",
                "AMOUNT_G": "1.1",
                "LOT_NUMBER": None,
            },
        ],
    }


def _fetch(
    binding: dict[str, Any], tables: dict[str, list[dict[str, Any]]], since: datetime | None = None
) -> tuple[Any, Any]:
    """Prime the fake, build the adapter and drain one fetch. Returns (adapter, entries)."""
    warehouse_fake.prime(**tables)
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")
    entries = asyncio.run(adapter.fetch_new_entries(since or datetime(2026, 1, 1, tzinfo=UTC)))
    return adapter, entries


def _primed() -> warehouse_fake.FakeWarehouse:
    """The warehouse the last `_fetch` actually built, for asserting what it received."""
    assert warehouse_fake.NEXT is not None
    return warehouse_fake.NEXT


def _one_reaction(binding: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> Any:
    """Run a full fetch+map cycle and return the single mapped reaction."""
    adapter, entries = _fetch(binding, tables)
    assert len(entries) == 1
    return adapter.map_to_ord(entries[0])


def test_the_cursor_filters_on_the_later_of_created_and_modified() -> None:
    """An amended run counts as new, which is the ELN sync's contract and not a nicety.

    Asserted on the emitted SQL because the failure is silent: filtering on creation alone ingests
    a run once and never sees the correction a chemist makes to it the following week. There is no
    exception and no rejected row — the amendment simply never arrives.
    """
    since = datetime(2026, 4, 1, tzinfo=UTC)
    _fetch(_binding(), _rows(), since)
    fake = _primed()

    statement, params = fake.executed[0]
    assert "COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ?" in statement
    assert "ORDER BY COALESCE(LAST_MODIFIED_TS, CREATED_TS) ASC" in statement
    assert params[0] == since


def test_a_source_without_amendments_filters_on_creation_alone() -> None:
    """No `modified_at` declared means no COALESCE — the predicate degrades, it does not break."""
    binding = _binding()
    del binding["ingest"]["entry"]["modified_at"]
    _fetch(binding, _rows())

    statement, _ = _primed().executed[0]
    assert "COALESCE" not in statement
    assert "WHERE CREATED_TS >= ?" in statement


def test_every_value_is_bound_and_only_identifiers_are_written() -> None:
    """The cursor and the row limit are parameters; nothing from a row reaches the statement."""
    _fetch(_binding(), _rows())

    statement, params = _primed().executed[0]
    assert statement.count("?") == len(params) == 2
    assert "2026" not in statement


def test_child_rows_are_fetched_once_for_the_whole_batch() -> None:
    """One query per child table per chunk, not per reaction — the cost that scales.

    Per-row fan-out would issue a query per reaction per table: a hundred reactions across four
    child tables is four hundred round trips to a warehouse that bills for them.
    """
    tables = _rows()
    tables["V_REACTION"].append(
        {
            "REACTION_ID": "RX-2",
            "CREATED_TS": _CREATED,
            "LAST_MODIFIED_TS": None,
            "OPERATOR": "b.chemist",
        }
    )
    _fetch(_binding(), tables)
    fake = _primed()

    assert len(fake.executed) == 2, "one entry query and one child query, whatever the row count"
    child_sql, child_params = fake.executed[1]
    assert "WHERE REACTION_ID IN (?, ?)" in child_sql
    assert child_params == ["RX-1", "RX-2"]


def test_the_site_vocabulary_and_units_are_mapped_by_the_binding() -> None:
    """`SM`/`SOLV`/`PROD` become roles, grams become milligrams, minutes become hours."""
    reaction = _one_reaction(_binding(), _rows())

    assert reaction.reaction_id == "RX-1"
    assert reaction.project == "PRJ-7"
    assert reaction.yield_percent == pytest.approx(82.5)
    assert reaction.time_h == pytest.approx(1.5)
    assert [c.role for c in reaction.inputs] == [Role.REACTANT, Role.SOLVENT]
    assert [c.role for c in reaction.outcomes] == [Role.PRODUCT]
    assert reaction.inputs[0].mass_mg == pytest.approx(1250.0)
    assert reaction.provenance == "eln-test:RX-1:a.chemist"


def test_a_new_child_table_reaches_the_payload_with_no_python_change() -> None:
    """The claim the package exists for, stated as a test.

    A site adds an analytics table; the binding gains a `related:` block and an `impurities:` block
    and nothing else changes anywhere. If this ever needs an edit outside the binding, the promise
    has been broken.
    """
    binding = _binding()
    binding["ingest"]["related"].append(
        {"name": "analytics", "relation": "V_PURITY", "foreign_key": "REACTION_ID"}
    )
    binding["ingest"]["impurities"] = [
        {
            "from": "analytics",
            "name": {"path": "PEAK_NAME"},
            "area_percent": {"path": "AREA_PCT", "transform": [{"number": {}}]},
        }
    ]
    binding["ingest"]["reaction"]["purity_percent"] = {
        "path": "analytics[0].ASSAY_PCT",
        "transform": [{"number": {}}],
    }
    tables = _rows()
    tables["V_PURITY"] = [
        {"REACTION_ID": "RX-1", "PEAK_NAME": "des-bromo", "AREA_PCT": "0.8", "ASSAY_PCT": "99.1"}
    ]

    reaction = _one_reaction(binding, tables)

    assert reaction.purity_percent == pytest.approx(99.1)
    assert [(i.name, i.area_percent) for i in reaction.impurities] == [("des-bromo", 0.8)]


def test_unmapped_columns_survive_into_the_attribute_bag() -> None:
    """Columns nobody has decided are worth a field are carried, not dropped.

    And columns a mapped field already consumed are *not* repeated — restating the yield beside the
    yield bullet would be noise in every note body this source ever produces.
    """
    binding = _binding()
    binding["ingest"]["attributes"] = {"include": ["*"], "exclude": ["NOTEBOOK_PAGE"]}
    reaction = _one_reaction(binding, _rows())

    assert reaction.attributes["VESSEL_ID"] == "V-12"
    assert reaction.attributes["OPERATOR"] == "a.chemist"
    assert "NOTEBOOK_PAGE" not in reaction.attributes, "excluded"
    assert "YIELD_PCT" not in reaction.attributes, "already consumed by yield_percent"
    assert "CREATED_TS" not in reaction.attributes, "the cursor column is not a recorded field"
    assert reaction.inputs[0].attributes == {"LOT_NUMBER": "L-991"}
    assert reaction.inputs[1].attributes == {}, "a blank lot number is silence, not an empty label"


def test_the_attribute_bag_is_bounded() -> None:
    """A wide view cannot put a hundred unmodelled lines into every note body."""
    binding = _binding()
    binding["ingest"]["attributes"] = {"include": ["*"], "max_fields": 2}
    tables = _rows()
    tables["V_REACTION"][0].update({f"EXTRA_{n}": f"v{n}" for n in range(20)})

    reaction = _one_reaction(binding, tables)
    assert len(reaction.attributes) == 2


def test_attributes_never_reach_the_chemistry() -> None:
    """The structural identity of a reaction is unchanged by its unmodelled columns.

    The specific failure this forbids is a structure arriving through an unvalidated bag of strings
    and changing a fingerprint — which would make two identical reactions look different, or two
    different ones look the same, on the strength of a vessel id.
    """
    bare = _binding()
    bare["ingest"]["attributes"] = {"include": []}
    wide = _binding()
    wide["ingest"]["attributes"] = {"include": ["*"]}

    without = _one_reaction(bare, _rows())
    with_bag = _one_reaction(wide, _rows())

    assert without.attributes == {}
    assert with_bag.attributes != {}
    assert without.transformation_smiles() == with_bag.transformation_smiles()
    assert without.reaction_smiles() == with_bag.reaction_smiles()


def test_an_unmapped_vocabulary_value_rejects_the_row_rather_than_dropping_the_field() -> None:
    """A material type the binding never heard of is an error, not a silently missing role.

    Yielding `None` would ingest the reaction with a species quietly absent — the corpus would gain
    a run whose charge sheet is wrong, and nothing would say so.
    """
    tables = _rows()
    tables["V_CHARGE"][0]["MATERIAL_TYPE"] = "BASE"
    adapter, entries = _fetch(_binding(), tables)

    with pytest.raises(ElnMappingError, match="no entry for 'BASE'"):
        adapter.map_to_ord(entries[0])


def test_a_charge_row_with_no_structure_is_skipped_not_fatal() -> None:
    """A charge table carries bookkeeping lines; one must not lose an otherwise good reaction."""
    tables = _rows()
    tables["V_CHARGE"].insert(0, {"REACTION_ID": "RX-1", "CHARGE_SEQ": 0, "MATERIAL_TYPE": "SM"})

    reaction = _one_reaction(_binding(), tables)
    assert len(reaction.inputs) == 2


def test_a_reaction_with_no_product_is_rejected_with_a_usable_reason() -> None:
    """The most likely binding mistake — a role map that never produces `product`."""
    tables = _rows()
    tables["V_CHARGE"] = [row for row in tables["V_CHARGE"] if row["MATERIAL_TYPE"] != "PROD"]
    adapter, entries = _fetch(_binding(), tables)

    with pytest.raises(ElnMappingError, match="0 product"):
        adapter.map_to_ord(entries[0])


def test_a_fallback_column_keeps_the_older_half_of_the_history() -> None:
    """A site that changed where it stores structures is one `fallback:` away from mappable."""
    binding = _binding()
    binding["ingest"]["components"][0]["smiles"] = {
        "path": "SMILES_STRUCTURE",
        "fallback": {"path": "LEGACY_SMILES"},
    }
    tables = _rows()
    tables["V_CHARGE"][0] = {
        "REACTION_ID": "RX-1",
        "CHARGE_SEQ": 1,
        "LEGACY_SMILES": "CC(=O)O",
        "MATERIAL_TYPE": "SM",
    }

    reaction = _one_reaction(binding, tables)
    assert reaction.inputs[0].smiles == "CC(=O)O"


def test_credentials_are_read_from_the_named_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The binding names variables; the values come from the environment, at connect time."""
    monkeypatch.setenv("TEST_WH_ACCOUNT", "acct-123")
    monkeypatch.setenv("TEST_WH_USER", "svc_chemclaw")
    binding = _binding()
    binding["connection"] = {
        "driver": _DRIVER,
        "account_env": "TEST_WH_ACCOUNT",
        "user_env": "TEST_WH_USER",
        "warehouse": "RND_WH",
    }
    _fetch(binding, _rows())
    fake = _primed()

    assert fake.connect_options["account"] == "acct-123"
    assert fake.connect_options["user"] == "svc_chemclaw"
    assert fake.connect_options["warehouse"] == "RND_WH"


def test_a_missing_credential_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator gets the variable name, not an authentication error from a vendor client."""
    monkeypatch.delenv("TEST_WH_ABSENT", raising=False)
    binding = _binding()
    binding["connection"] = {"driver": _DRIVER, "account_env": "TEST_WH_ABSENT"}
    warehouse_fake.prime(**_rows())
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")

    with pytest.raises(ElnMappingError, match="TEST_WH_ABSENT"):
        asyncio.run(adapter.fetch_new_entries(datetime(2026, 1, 1, tzinfo=UTC)))
