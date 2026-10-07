"""The ingest half, proven against a fake warehouse: no tenant, no driver, no credentials.

Attaching a warehouse ELN is writing a binding, not Python, so these tests assert the statement
the engine sends meets the sync's contract and that a schema change is a YAML change only.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from chemclaw.durable.eln_sync import _BoundedIngest
from chemclaw.ingest.eln.adapter import ElnMappingError
from chemclaw.ingest.eln.ord import Role
from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.ingest.eln.sync import sync_entries
from chemclaw.ingest.eln.warehouse.adapter import _MAX_TIE_PAGES, WarehouseElnAdapter
from chemclaw.ingest.eln.warehouse.binding import BindingError, load_binding
from chemclaw.ingest.eln.warehouse.connect import forget_open_warehouses
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
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


def _filed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Capture what the adapter files in the rejection ledger, with no database under it.

    A row the fetch loses is a record a chemist will later assume is in the corpus, so it must be
    filed where it can be answered for, not only logged.
    """
    filed: dict[str, str] = {}

    async def _spy(source: str, refusals: dict[str, str]) -> None:
        filed.update(refusals)

    monkeypatch.setattr("chemclaw.ingest.eln.warehouse.adapter.record_refusals", _spy)
    return filed


def test_an_unparseable_amendment_stamp_is_refused_rather_than_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present but unparseable `modified_at` is refused, not read as "never amended".

    Read as absent, `entry_window` falls back to creation, the row never re-enters the fetch window,
    and the correction is never ingested. Matches `json_adapter`'s rule.
    """
    filed = _filed(monkeypatch)
    tables = _rows()
    tables["V_REACTION"][0]["LAST_MODIFIED_TS"] = "01/09/2026"

    _, entries = _fetch(_binding(), tables)

    assert entries == [], "a row whose amendment stamp cannot be read is refused, not ingested"
    assert "LAST_MODIFIED_TS" in filed["RX-1"] and "01/09/2026" in filed["RX-1"]
    assert "declare a `transform:`" not in filed["RX-1"], (
        "the refusal named a `transform:` beside an entry column as the remedy, which the "
        "binding's `extra='forbid'` refuses — a fix the site cannot apply"
    )
    assert "NULLIF" in filed["RX-1"] and "where:" in filed["RX-1"]


def test_an_unparseable_withdrawal_stamp_is_refused_rather_than_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present but unparseable `retracted_at` is refused, not read as absent.

    Read as `None`, the source's withdrawal would never be seen and the row would stay live.
    """
    filed = _filed(monkeypatch)
    binding = _binding()
    binding["ingest"]["entry"]["retracted_at"] = "WITHDRAWN_TS"
    tables = _rows()
    tables["V_REACTION"][0]["WITHDRAWN_TS"] = "N/A"

    _, entries = _fetch(binding, tables)

    assert entries == []
    assert "WITHDRAWN_TS" in filed["RX-1"]


def test_a_blank_amendment_stamp_is_still_simply_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank amendment stamp is still simply absent.

    `NULL`, `''` and whitespace are the ordinary un-amended state; refusing them would refuse the
    whole corpus.
    """
    filed = _filed(monkeypatch)
    for blank in (None, "", "   "):
        tables = _rows()
        tables["V_REACTION"][0]["LAST_MODIFIED_TS"] = blank
        _, entries = _fetch(_binding(), tables)
        assert [entry.entry_id for entry in entries] == ["RX-1"], blank
        assert entries[0].modified_at is None
    assert filed == {}


def test_a_row_with_no_usable_key_reaches_the_rejection_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with no usable key reaches the rejection ledger; a key of `0` is kept.

    Keys are tested for presence, not truthiness, and a blank key is filed in the ledger rather than
    only logged.
    """
    filed = _filed(monkeypatch)
    tables = _rows()
    header = tables["V_REACTION"][0]
    tables["V_REACTION"] = [
        {**header, "REACTION_ID": 0},
        {**header, "REACTION_ID": "  "},
    ]
    tables["V_CHARGE"] = [{**row, "REACTION_ID": 0} for row in tables["V_CHARGE"]]

    _, entries = _fetch(_binding(), tables)

    assert [entry.entry_id for entry in entries] == ["0"], (
        "an integer key of 0 is a key; truthiness removed a real row from the fetch"
    )
    assert "REACTION_ID" in filed["<no REACTION_ID>"]


def test_two_rows_sharing_one_key_leave_a_ledger_row_naming_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rows sharing one key leave a ledger row naming the id.

    The survivor is ingested under that id, so the ledger row is what tells a citation it does not
    name one run.
    """
    filed = _filed(monkeypatch)
    tables = _rows()
    tables["V_REACTION"].append({**tables["V_REACTION"][0], "YIELD_PCT": "11.0"})

    _, entries = _fetch(_binding(), tables)

    assert [entry.entry_id for entry in entries] == ["RX-1"]
    assert "2 rows" in filed["RX-1"] and "V_REACTION" in filed["RX-1"]


def test_a_warehouse_impurity_known_only_by_its_rrt_is_named_rather_than_dropped() -> None:
    """A warehouse impurity known only by its RRT is named rather than dropped.

    `Impurity._identifiable` prescribes a name of the form "the RRT 0.94 peak"; unresolved peaks are
    often the largest in the profile.
    """
    binding = _binding()
    binding["ingest"]["impurities"] = [
        {
            "from": "peaks",
            "name": {"path": "PEAK_NAME"},
            "area_percent": {"path": "AREA_PCT", "transform": [{"number": {}}]},
            "rrt": {"path": "RRT", "transform": [{"number": {}}]},
        }
    ]
    binding["ingest"]["related"].append(
        {
            "name": "peaks",
            "relation": "V_PEAK",
            "foreign_key": "REACTION_ID",
            "order_by": "PEAK_SEQ",
        }
    )
    tables = _rows()
    tables["V_PEAK"] = [
        {"REACTION_ID": "RX-1", "PEAK_SEQ": 1, "PEAK_NAME": "des-bromo", "AREA_PCT": "0.31"},
        {"REACTION_ID": "RX-1", "PEAK_SEQ": 2, "PEAK_NAME": None, "AREA_PCT": "1.9", "RRT": "0.94"},
    ]

    reaction = _one_reaction(binding, tables)

    assert [impurity.name for impurity in reaction.impurities] == ["des-bromo", "RRT 0.94 peak"]


@pytest.mark.parametrize("rrt", [Decimal("0.94"), "0.94"], ids=["numeric-column", "text-column"])
def test_an_rrt_only_peak_is_named_whatever_type_the_driver_hands_back(rrt: object) -> None:
    """An RRT-only peak is named whether the driver returns a `Decimal` or a `str`.

    Without a `number` transform the value arrives as the driver typed it; it is coerced and carried
    as a float.
    """
    binding = _binding()
    binding["ingest"]["impurities"] = [
        {"from": "peaks", "name": {"path": "PEAK_NAME"}, "rrt": {"path": "RRT"}}
    ]
    binding["ingest"]["related"].append(
        {
            "name": "peaks",
            "relation": "V_PEAK",
            "foreign_key": "REACTION_ID",
            "order_by": "PEAK_SEQ",
        }
    )
    tables = _rows()
    tables["V_PEAK"] = [{"REACTION_ID": "RX-1", "PEAK_SEQ": 1, "PEAK_NAME": None, "RRT": rrt}]

    (impurity,) = _one_reaction(binding, tables).impurities

    assert (impurity.name, impurity.rrt) == ("RRT 0.94 peak", 0.94)


def test_the_cursor_filters_on_the_later_of_created_and_modified() -> None:
    """The cursor filters on the later of created and modified, so an amended run counts as new.

    Asserted on the emitted SQL, because filtering on creation alone would silently never ingest a
    correction.
    """
    since = datetime(2026, 4, 1, tzinfo=UTC)
    _fetch(_binding(), _rows(), since)
    fake = _primed()

    statement, params = fake.executed[0]
    assert "COALESCE(LAST_MODIFIED_TS, CREATED_TS) >= ?" in statement
    assert "ORDER BY COALESCE(LAST_MODIFIED_TS, CREATED_TS) ASC, REACTION_ID ASC" in statement
    assert params[0] == since


def test_a_declared_withdrawal_column_is_in_the_cursor_and_an_undeclared_one_is_not() -> None:
    """A declared withdrawal column is in the cursor; an undeclared one leaves the SQL unchanged.

    The fake mirrors the watermark's semantics without parsing the clause, so only the SQL pins them
    together. `COALESCE(retracted, W)` inside `GREATEST` matters because some warehouses propagate
    NULL through `GREATEST`, which would stop the source dead.
    """
    binding = _binding()
    binding["ingest"]["entry"]["retracted_at"] = "RETRACTED_TS"
    _fetch(binding, _rows())
    window = "COALESCE(LAST_MODIFIED_TS, CREATED_TS)"
    withdrawn = f"GREATEST({window}, COALESCE(RETRACTED_TS, {window}))"

    statement, _ = _primed().executed[0]
    assert f"{withdrawn} >= ?" in statement
    assert f"ORDER BY {withdrawn} ASC, REACTION_ID ASC" in statement

    # `open_warehouse` memoises per connection block, so the second fetch would otherwise be served
    # the fake the first one primed and record nothing at all.
    forget_open_warehouses()
    _fetch(_binding(), _rows())
    plain, _ = _primed().executed[0]
    assert "RETRACTED_TS" not in plain and "GREATEST" not in plain, (
        "a site with no withdrawal column had its cursor predicate rewritten anyway"
    )


def test_a_source_without_amendments_filters_on_creation_alone() -> None:
    """No `modified_at` declared means no COALESCE — the predicate degrades, it does not break."""
    binding = _binding()
    del binding["ingest"]["entry"]["modified_at"]
    _fetch(binding, _rows())

    statement, _ = _primed().executed[0]
    assert "COALESCE" not in statement
    assert "WHERE (CREATED_TS >= ?)" in statement
    # The tiebreaker is not optional here either: `LIMIT` over a non-unique order is a different
    # subset of one watermark block on every fetch.
    assert "ORDER BY CREATED_TS ASC, REACTION_ID ASC" in statement


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
    """A new child table reaches the payload with no Python change.

    The binding gains a `related:` and an `impurities:` block and nothing else changes.
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
    """Unmodelled attribute columns never change a reaction's structural identity.

    Otherwise a vessel id could make identical reactions fingerprint differently.
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


def test_a_connection_block_is_whatever_its_driver_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection block is whatever its driver takes: the driver's own keyword arguments.

    The vocabulary below belongs to no shipped driver, so attaching an unknown database is a driver
    module plus a manifest. The `*_env` suffix is the one convention: the binding names a secret's
    variable and the value is read at connect time.
    """
    monkeypatch.setenv("TEST_STORE_KEY", "sk-live-1")
    binding = _binding()
    binding["connection"] = {
        "driver": _DRIVER,
        "dsn": "acme://vectors.internal:9000",
        "api_key_env": "TEST_STORE_KEY",
        "collection": "reactions",
    }
    _fetch(binding, _rows())
    fake = _primed()

    assert fake.connect_options == {
        "dsn": "acme://vectors.internal:9000",
        "api_key": "sk-live-1",
        "collection": "reactions",
    }, "the block reached the driver verbatim, with only the named secret resolved"


def test_a_missing_credential_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator gets the variable name, not an authentication error from a vendor client."""
    monkeypatch.delenv("TEST_WH_ABSENT", raising=False)
    binding = _binding()
    binding["connection"] = {"driver": _DRIVER, "token_env": "TEST_WH_ABSENT"}
    warehouse_fake.prime(**_rows())
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")

    with pytest.raises(ElnMappingError, match="TEST_WH_ABSENT"):
        asyncio.run(adapter.fetch_new_entries(datetime(2026, 1, 1, tzinfo=UTC)))


def test_a_null_column_leaves_the_schema_default_rather_than_rejecting_the_row() -> None:
    """A NULL column leaves the schema default rather than rejecting the row.

    A silent field is omitted, not passed as `None`, so a missing `reaction_id` raises "field
    required". `outcome_class` carries silence as "no outcome stated" rather than success, while a
    present value maps as before.
    """
    binding = _binding()
    binding["ingest"]["reaction"]["outcome_class"] = {
        "path": "root.RESULT_FLAG",
        "transform": [{"value_map": {"map": {"OK": "success", "FAIL": "failure"}}}],
    }
    tables = _rows()
    tables["V_REACTION"][0]["RESULT_FLAG"] = None

    reaction = _one_reaction(binding, tables)
    assert reaction.outcome_class is None, "a NULL status column states nothing, and says so"

    tables["V_REACTION"][0]["RESULT_FLAG"] = "FAIL"
    tables["V_REACTION"][0]["FAILURE_NOTE"] = "decomposed on scale"
    binding["ingest"]["reaction"]["failure_reason"] = {"path": "root.FAILURE_NOTE"}
    mapped = _one_reaction(binding, tables).outcome_class
    assert mapped is not None and mapped.value == "failure", "and a value still maps"


def test_a_missing_reaction_id_names_the_field_rather_than_a_type_error() -> None:
    """Omitting silence must still fail loudly for the one field that is the note's identity."""
    tables = _rows()
    tables["V_REACTION"][0]["REACTION_ID"] = "RX-1"
    adapter, entries = _fetch(_binding(), tables)
    entries[0].payload["root"]["REACTION_ID"] = None

    with pytest.raises(ElnMappingError, match="reaction_id"):
        adapter.map_to_ord(entries[0])


# --- the paging contract: a page of amended rows must not stall the cursor -----------------------


def _reaction_row(
    entry_id: str, created: datetime, modified: datetime | None = None
) -> dict[str, Any]:
    """One header row, complete enough for the binding above to map it into a real reaction."""
    return {
        "REACTION_ID": entry_id,
        "CREATED_TS": created,
        "LAST_MODIFIED_TS": modified,
        "PROJECT_CODE": "PRJ-7",
        "YIELD_PCT": "82.5",
        "DURATION_MIN": "90",
        "OPERATOR": "a.chemist",
    }


def _charge_rows(entry_id: str) -> list[dict[str, Any]]:
    """The three charges that make `entry_id` a mass-balanced esterification."""
    return [dict(row, REACTION_ID=entry_id) for row in _rows()["V_CHARGE"]]


def test_a_page_of_amended_rows_does_not_stall_the_sync_forever() -> None:
    """A page of amended rows does not stall the sync forever.

    The fetch pages on the amendment watermark, so the cursor must advance on it; otherwise more
    amended rows than one page would hide a new reaction forever. Driven through `sync_entries`
    against a fake that honours WHERE/ORDER BY/LIMIT, since the defect is the seam between them.
    """
    old = datetime(2026, 1, 1, tzinfo=UTC)
    amended = datetime(2026, 6, 1, tzinfo=UTC)
    created_later = datetime(2026, 6, 2, tzinfo=UTC)
    binding = _binding()
    binding["ingest"]["entry"]["fetch_limit"] = 2
    reactions = [
        _reaction_row("OLD-1", old, amended),
        _reaction_row("OLD-2", old, amended + timedelta(minutes=1)),
        _reaction_row("OLD-3", old, amended + timedelta(minutes=2)),
        _reaction_row("NEW-1", created_later, None),
    ]
    charges = [row for entry in ("OLD-1", "OLD-2", "OLD-3", "NEW-1") for row in _charge_rows(entry)]
    warehouse_fake.prime_warehouse(
        warehouse_fake.WatermarkWarehouse(
            {"V_REACTION": reactions, "V_CHARGE": charges},
            entry_relation="V_REACTION",
            created_at="CREATED_TS",
            modified_at="LAST_MODIFIED_TS",
        )
    )
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")

    async def _run() -> set[str]:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            InMemoryReactionRecordStore(),
        )
        cursor = old
        seen: set[str] = set()
        for _ in range(4):  # four chunks is more than enough to drain four rows two at a time
            summary = await sync_entries(
                adapter,
                rxn,
                mol,
                rec,
                cursor,
                label_index=InMemoryLabelIndex(),
                source="eln-databricks",
                apply_overlap=False,
            )
            seen.update(summary.ingested)
            cursor = summary.next_cursor
        return seen

    assert "NEW-1" in asyncio.run(_run()), "the reaction created after the amendments is reachable"


# --- the paging contract: a watermark block larger than one page must still be got past ----------


def _drain(
    adapter: WarehouseElnAdapter,
    since: datetime,
    *,
    batch: int,
    chunks: int,
    records: InMemoryReactionRecordStore | None = None,
) -> tuple[set[str], list[str]]:
    """Run `ElnSyncWorkflow`'s own chunk loop against `adapter`, returning what it ingested.

    Transcribed because the loop's `has_more` decision and wedge guard are the subject. `records`
    persists between calls so a test can run a first sync and then a withdrawal on one corpus.
    """

    async def _run() -> tuple[set[str], list[str]]:
        rxn, mol, rec = (
            InMemoryFingerprintStore(),
            InMemoryFingerprintStore(),
            records or InMemoryReactionRecordStore(),
        )
        label_index = InMemoryLabelIndex()
        seen: set[str] = set()
        wedged: list[str] = []
        cursor = since
        for _ in range(chunks):
            bounded = _BoundedIngest(adapter, cursor, batch)
            summary = await sync_entries(
                bounded,
                rxn,
                mol,
                rec,
                cursor,
                label_index=label_index,
                source="eln-databricks",
                apply_overlap=False,
            )
            seen.update(summary.ingested)
            if not bounded.truncated:
                break
            if summary.next_cursor <= cursor:
                # The workflow's guard: more entries reported, and a cursor that did not move.
                wedged.append(cursor.isoformat())
                break
            cursor = summary.next_cursor
        return seen, wedged

    return asyncio.run(_run())


def _tied_warehouse(count: int, watermark: datetime, later: datetime | None = None) -> None:
    """`count` reactions all stamped `watermark`, plus one stamped `later` when asked."""
    reactions = [_reaction_row(f"TIE-{i:03d}", watermark) for i in range(count)]
    entries = [row["REACTION_ID"] for row in reactions]
    if later is not None:
        reactions.append(_reaction_row("AFTER-1", later))
        entries.append("AFTER-1")
    warehouse_fake.prime_warehouse(
        warehouse_fake.WatermarkWarehouse(
            {
                "V_REACTION": reactions,
                "V_CHARGE": [row for entry in entries for row in _charge_rows(str(entry))],
            },
            entry_relation="V_REACTION",
            created_at="CREATED_TS",
            modified_at="LAST_MODIFIED_TS",
            key="REACTION_ID",
        )
    )


def test_a_watermark_block_larger_than_one_page_is_drained_rather_than_truncated() -> None:
    """A watermark block larger than one page is drained rather than truncated.

    If more rows share one watermark than a page holds, a cursor equal to that timestamp would
    return the same page forever. A DATE-bound `created_at` or a bulk `UPDATE` produces such ties.
    """
    tie = datetime(2026, 6, 1, tzinfo=UTC)
    binding = _binding()
    binding["ingest"]["entry"]["fetch_limit"] = 2
    _tied_warehouse(5, tie, later=tie + timedelta(days=1))
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")

    seen, wedged = _drain(adapter, tie - timedelta(days=1), batch=2, chunks=6)

    assert seen == {f"TIE-{i:03d}" for i in range(5)} | {"AFTER-1"}
    assert wedged == []
    # And the child fan-out stayed inside the bound the binding declares. A batch is no longer one
    # page — crossing the block accumulates several — while every key in it is a bind parameter in
    # each `IN (...)`, which is what `fetch_limit` is capped to protect.
    charges = [params for sql, params in _primed().executed if " V_CHARGE " in sql]
    assert charges and all(len(params) <= 2 for params in charges)


def test_a_block_too_large_to_page_past_stops_the_source_out_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tie block too large to page past within the bound stops the source out loud.

    The fetch reports itself truncated, so the workflow's no-cursor-advance guard is reached, and
    the adapter names the watermark value the binding must be fixed for.
    """
    tie = datetime(2026, 6, 1, tzinfo=UTC)
    binding = _binding()
    binding["ingest"]["entry"]["fetch_limit"] = 2
    _tied_warehouse(2 * _MAX_TIE_PAGES + 4, tie)
    adapter = WarehouseElnAdapter(binding=binding, name="eln-test")

    with caplog.at_level("WARNING"):
        _, wedged = _drain(adapter, tie, batch=2, chunks=4)

    assert wedged == [tie.isoformat()], "the wedge guard must be able to fire on this case"
    assert any("2026-06-01" in record.getMessage() for record in caplog.records)


def test_one_row_with_no_creation_timestamp_costs_itself_and_not_the_source(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One row with no creation timestamp costs itself, not the source.

    `fetch_new_entries` runs before `sync_entries`' reject-and-continue, and `ElnMappingError` is
    non-retryable, so a raise there would stop the ELN permanently. Such a row arrives via
    `COALESCE(modified, created)`. Driven through the workflow's chunk loop to see the cursor move.
    """
    old = datetime(2026, 1, 1, tzinfo=UTC)
    amended = datetime(2026, 6, 1, tzinfo=UTC)
    rows = [
        _reaction_row("RX-1", _CREATED),
        dict(_reaction_row("RX-2", _CREATED, modified=amended), CREATED_TS=None),
        _reaction_row("RX-3", _CREATED),
    ]
    warehouse_fake.prime_warehouse(
        warehouse_fake.WatermarkWarehouse(
            {
                "V_REACTION": rows,
                "V_CHARGE": [
                    row for entry in ("RX-1", "RX-2", "RX-3") for row in _charge_rows(entry)
                ],
            },
            entry_relation="V_REACTION",
            created_at="CREATED_TS",
            modified_at="LAST_MODIFIED_TS",
            key="REACTION_ID",
        )
    )
    adapter = WarehouseElnAdapter(binding=_binding(), name="eln-test")

    with caplog.at_level("WARNING"):
        seen, wedged = _drain(adapter, old, batch=10, chunks=2)

    assert seen == {"RX-1", "RX-3"}, "the batch was lost to one row it could not order"
    assert wedged == [], "the source must still be able to advance its cursor"
    assert any("RX-2" in record.getMessage() for record in caplog.records), (
        "the skipped row must name itself, or it is lost in silence"
    )


def test_a_binding_may_name_the_intent_column_but_not_carve_one_out_of_prose() -> None:
    """A binding may name the intent column but not carve one out of prose.

    A hypothesis extracted by pattern-matching is indistinguishable downstream from one the chemist
    wrote, so `regex`-style transforms are refused for it, across the whole fallback chain. A plain
    column is the chemist's own field and is allowed.
    """
    # Only transforms that can put text in the field which the cell does not hold are refused;
    # `strip` is allowed.
    allowed_shapes = (
        {"path": "root.OBJECTIVE"},
        {"path": "root.OBJECTIVE", "transform": [{"strip": {}}]},
    )
    for allowed in allowed_shapes:
        plain = _binding()
        plain["ingest"]["reaction"]["hypothesis"] = allowed
        ingest = load_binding(plain).ingest
        assert ingest is not None and ingest.reaction["hypothesis"].path == "root.OBJECTIVE"

    for label, field in (
        (
            "regex carves a substring out of prose",
            {"path": "root.NOTES", "transform": [{"regex": {"pattern": "Aim:(.+)"}}]},
        ),
        (
            "default invents one outright",
            {"path": "root.X", "transform": [{"default": {"value": "routine"}}]},
        ),
        (
            "value_map substitutes one",
            {"path": "root.X", "transform": [{"value_map": {"map": {"a": "b"}}}]},
        ),
        (
            "and a fallback is part of the chain",
            {
                "path": "root.X",
                "fallback": {"path": "root.NOTES", "transform": [{"regex": {"pattern": "(.+)"}}]},
            },
        ),
    ):
        derived = _binding()
        derived["ingest"]["reaction"]["hypothesis"] = field
        with pytest.raises(BindingError, match="derive a run's stated intent"):
            load_binding(derived)
        assert label  # names which shape failed, when one of them stops failing


def test_a_bounded_chunk_asks_the_warehouse_for_the_chunk_and_not_for_the_page() -> None:
    """A bounded chunk asks the warehouse for the chunk, not the whole page.

    The sync drains in chunks of `eln_sync_batch_size`, so the `LIMIT` the engine binds must be the
    chunk size rather than `fetch_limit`. Asserted against the fake that honours WHERE/ORDER
    BY/LIMIT.
    """
    since = datetime(2026, 1, 1, tzinfo=UTC)
    binding = _binding()
    binding["ingest"]["entry"]["fetch_limit"] = 150
    reactions = [
        _reaction_row(f"RX-{i:03d}", since + timedelta(minutes=i), None) for i in range(120)
    ]
    charges = [row for entry in reactions for row in _charge_rows(str(entry["REACTION_ID"]))]

    def _entry_limits(warehouse: Any) -> list[Any]:
        return [
            params[-1]
            for statement, params in warehouse.executed
            if "FROM V_REACTION " in statement
        ]

    def _drive(limit: int | None) -> tuple[list[Any], Any]:
        # `open_warehouse` memoises per connection block, so the second drive would otherwise be
        # served the fake the first one primed and record nothing.
        forget_open_warehouses()
        warehouse = warehouse_fake.prime_warehouse(
            warehouse_fake.WatermarkWarehouse(
                {"V_REACTION": reactions, "V_CHARGE": charges},
                entry_relation="V_REACTION",
                created_at="CREATED_TS",
                modified_at="LAST_MODIFIED_TS",
                key="REACTION_ID",
            )
        )
        adapter = WarehouseElnAdapter(binding=binding, name="eln-test")
        return asyncio.run(adapter.fetch_new_entries(since, limit)), warehouse

    entries, warehouse = _drive(5)
    assert _entry_limits(warehouse) == [5], (
        "the bounded chunk still asked the warehouse for the binding's whole page; the sync would "
        f"throw all but five of them away. LIMITs bound: {_entry_limits(warehouse)}"
    )
    assert len(entries) == 5

    # Unbounded is unchanged: a caller reading a whole corpus still gets the binding's page.
    entries, warehouse = _drive(None)
    assert _entry_limits(warehouse) == [150]
    assert len(entries) == 120


def test_a_site_that_withdraws_a_row_reaches_the_record_without_touching_its_amendment_column() -> (
    None
):
    """A site's withdrawal reaches the record without touching its amendment column.

    `RawEntry.retracted_at` is the only setter of `reaction_records.retracted_at`, so the binding
    must be able to name the withdrawal column. Driven through `_drain` to show the field survives
    `_BoundedIngest`. `LAST_MODIFIED_TS` is untouched, so the watermark must include the withdrawal
    column; `RX-KEPT` makes the assertion a difference.
    """
    created = datetime(2026, 5, 1, tzinfo=UTC)
    pulled = datetime(2026, 8, 1, tzinfo=UTC)
    binding = _binding()
    binding["ingest"]["entry"]["retracted_at"] = "RETRACTED_TS"
    rows = {
        "RX-PULLED": dict(_reaction_row("RX-PULLED", created), RETRACTED_TS=None),
        "RX-KEPT": dict(_reaction_row("RX-KEPT", created), RETRACTED_TS=None),
    }
    charges = [row for entry in rows for row in _charge_rows(entry)]

    def _prime() -> None:
        # Memoised per connection block, so a second prime without this is simply ignored and the
        # run below would be served the first fake's rows.
        forget_open_warehouses()
        warehouse_fake.prime_warehouse(
            warehouse_fake.WatermarkWarehouse(
                {"V_REACTION": list(rows.values()), "V_CHARGE": charges},
                entry_relation="V_REACTION",
                created_at="CREATED_TS",
                modified_at="LAST_MODIFIED_TS",
                key="REACTION_ID",
                retracted_at="RETRACTED_TS",
            )
        )

    records = InMemoryReactionRecordStore()
    _prime()
    first, _ = _drain(
        WarehouseElnAdapter(binding=binding, name="eln-test"),
        created - timedelta(days=1),
        batch=10,
        chunks=2,
        records=records,
    )
    assert first == {"RX-PULLED", "RX-KEPT"}, "neither row was ingested, so nothing below is a test"
    assert asyncio.run(records.retracted([("eln-databricks", "RX-PULLED")])) == set()

    rows["RX-PULLED"] = dict(rows["RX-PULLED"], RETRACTED_TS=pulled)
    _prime()
    # The cursor now sits past the creation of both rows and past any amendment, which is exactly
    # where a scheduled sync is when a site withdraws something a month later.
    second, _ = _drain(
        WarehouseElnAdapter(binding=binding, name="eln-test"),
        created + timedelta(days=1),
        batch=10,
        chunks=2,
        records=records,
    )

    assert second == {"RX-PULLED"}, (
        "the withdrawn row was not re-fetched, so its tombstone is written at the site and read "
        "by nobody — and the row that was not withdrawn must not come back either"
    )
    stored = asyncio.run(records.read("RX-PULLED"))
    assert stored is not None and stored.retracted_at == pulled
    assert asyncio.run(
        records.retracted([("eln-databricks", "RX-PULLED"), ("eln-databricks", "RX-KEPT")])
    ) == {("eln-databricks", "RX-PULLED")}
    assert asyncio.run(records.eligible(["RX-PULLED", "RX-KEPT"], {})) == {"RX-KEPT"}
