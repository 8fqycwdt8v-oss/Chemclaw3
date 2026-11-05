"""A source's `query:` binding, and the one statement a typed question becomes.

Every value is bound and only declared columns and operators are written, so the injection tests
run real payloads through `sqlite3` and check the table is still there and the payload matched as
data. The exact-statement tests pin what a warehouse is sent.
"""

import sqlite3
from datetime import date
from typing import Any

import pytest

from chemclaw.ingest.eln.warehouse.binding import BindingError, load_binding
from chemclaw.ingest.eln.warehouse.sql import (
    MAX_IN_VALUES,
    QueryFilter,
    QuerySpec,
    query_statement,
)


def _query(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "relation": "ELN.REACTIONS",
        "key": "RECORD_ID",
        "citation": {"path": "root.RECORD_ID"},
        "columns": {
            "project": {
                "column": "PROJECT",
                "type": "string",
                "ops": ["eq", "in", "contains"],
                "description": "The project code the run was booked to.",
            },
            "yield_percent": {
                "column": "YIELD_PCT",
                "type": "number",
                "ops": ["gte", "lte", "between"],
                "unit": "%",
                "description": "Isolated yield of the headline product.",
            },
            "recorded_on": {
                "column": "RECORDED_ON",
                "type": "date",
                "ops": ["gte", "lte", "between"],
                "description": "The day the run was recorded.",
            },
            "outcome": {
                "column": "OUTCOME",
                "type": "enum",
                "ops": ["eq", "in"],
                "enum_values": ["success", "failure"],
                "description": "Whether the run gave product.",
            },
        },
        "default_order": "recorded_on",
        "max_rows": 3,
    }
    block.update(overrides)
    return block


def _binding(**overrides: Any) -> Any:
    return load_binding(
        {
            "connection": {"driver": "sqlite3:connect", "database": ":memory:"},
            "query": _query(**overrides),
        }
    ).query


# ------------------------------------------------------------------ the binding


def test_a_query_only_binding_is_a_binding() -> None:
    assert _binding().relation == "ELN.REACTIONS"


@pytest.mark.parametrize(
    ("column", "message"),
    [
        ({"column": "NOTE", "type": "string", "ops": ["gte"], "description": "x"}, "cannot take"),
        ({"column": "Y", "type": "number", "ops": ["contains"], "description": "x"}, "cannot take"),
        ({"column": "S", "type": "smiles", "ops": ["contains"], "description": "x"}, "cannot take"),
        ({"column": "E", "type": "enum", "ops": ["eq"], "description": "x"}, "enum_values"),
        (
            {
                "column": "E",
                "type": "string",
                "ops": ["eq"],
                "enum_values": ["a"],
                "description": "x",
            },
            "enum_values",
        ),
        (
            {"column": "X; DROP TABLE T", "type": "string", "ops": ["eq"], "description": "x"},
            "identifier",
        ),
    ],
)
def test_a_column_whose_operators_do_not_fit_its_type_is_refused(
    column: dict[str, Any], message: str
) -> None:
    with pytest.raises(BindingError, match=message):
        _binding(columns={"bad": column}, default_order="bad")


def test_a_filter_name_is_a_tool_argument_name() -> None:
    columns = {"Yield": {"column": "Y", "type": "number", "ops": ["gte"], "description": "x"}}
    with pytest.raises(BindingError, match="lower_snake_case"):
        _binding(columns=columns, default_order="Yield")


def test_the_default_order_names_a_declared_column() -> None:
    with pytest.raises(BindingError, match="default_order"):
        _binding(default_order="not_declared")


def test_the_row_cap_is_bounded() -> None:
    with pytest.raises(BindingError):
        _binding(max_rows=201)


# ------------------------------------------------------------------ the statement


def test_a_typed_question_becomes_one_bounded_statement_with_every_value_bound() -> None:
    spec = QuerySpec(
        filters=[
            QueryFilter(name="project", op="eq", value="PROJ-BETA"),
            QueryFilter(name="yield_percent", op="between", value=[80, 95]),
            QueryFilter(name="outcome", op="in", value=["success"]),
        ]
    )
    sql, params, cap = query_statement(_binding(), "?", spec)
    assert sql == (
        "SELECT * FROM ELN.REACTIONS WHERE PROJECT = ? AND YIELD_PCT BETWEEN ? AND ? "
        "AND OUTCOME IN (?) ORDER BY RECORDED_ON DESC, RECORD_ID ASC LIMIT ?"
    )
    assert params == ["PROJ-BETA", 80, 95, "success", 4]
    assert cap == 3


def test_the_placeholder_is_the_driver_s() -> None:
    sql, _, _ = query_statement(
        _binding(), "%s", QuerySpec(filters=[QueryFilter(name="project", op="eq", value="A")])
    )
    assert sql.count("%s") == 2 and "?" not in sql


def test_a_caller_may_order_and_cap_within_the_binding_s_bounds() -> None:
    spec = QuerySpec(order_by="yield_percent", descending=False, limit=50)
    sql, params, cap = query_statement(_binding(), "?", spec)
    assert "ORDER BY YIELD_PCT ASC, RECORD_ID ASC" in sql
    assert cap == 3 and params == [4]


def test_the_site_s_where_is_anded_with_the_filters() -> None:
    sql, _, _ = query_statement(
        _binding(where="STATUS = 'DONE'"),
        "?",
        QuerySpec(filters=[QueryFilter(name="project", op="eq", value="A")]),
    )
    assert "WHERE PROJECT = ? AND (STATUS = 'DONE')" in sql


@pytest.mark.parametrize(
    ("wanted", "message"),
    [
        (QueryFilter(name="operator", op="eq", value="x"), "not a filter this source declares"),
        (QueryFilter(name="project", op="gte", value="x"), "does not take"),
        (QueryFilter(name="outcome", op="eq", value="exploded"), "has no value"),
        (QueryFilter(name="project", op="in", value="A"), "list of 1 to"),
        (QueryFilter(name="project", op="in", value=[]), "list of 1 to"),
        (QueryFilter(name="project", op="in", value=["a"] * (MAX_IN_VALUES + 1)), "list of 1 to"),
        (QueryFilter(name="yield_percent", op="between", value=[1]), "exactly"),
        (QueryFilter(name="project", op="eq", value=["a", "b"]), "one value"),
    ],
)
def test_what_the_binding_did_not_declare_is_refused_before_a_statement_exists(
    wanted: QueryFilter, message: str
) -> None:
    with pytest.raises(BindingError, match=message):
        query_statement(_binding(), "?", QuerySpec(filters=[wanted]))


def test_an_order_on_an_undeclared_column_is_refused() -> None:
    with pytest.raises(BindingError, match="cannot order by"):
        query_statement(_binding(), "?", QuerySpec(order_by="RECORD_ID; DROP TABLE x"))


# ------------------------------------------------------------------ against a real database


@pytest.fixture
def warehouse() -> sqlite3.Connection:
    """`ELN.REACTIONS` as sqlite spells a schema-qualified table: an attached database."""
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS ELN")
    connection.execute(
        "CREATE TABLE ELN.REACTIONS (RECORD_ID TEXT, PROJECT TEXT, YIELD_PCT REAL, "
        "RECORDED_ON TEXT, OUTCOME TEXT)"
    )
    rows = [
        ("r1", "PROJ-BETA", 92.5, "2024-01-08", "success"),
        ("r2", "PROJ-BETA", 79.5, "2024-01-09", "success"),
        ("r3", "PROJ-ALPHA", 88.0, "2024-01-10", "success"),
        ("r4", "PROJ_50%", 4.0, "2024-01-16", "failure"),
        ("r5", "PROJ-BETA", 88.5, "2024-01-13", "success"),
        ("r6", "PROJ-BETA", 70.5, "2024-01-12", "success"),
    ]
    connection.executemany("INSERT INTO ELN.REACTIONS VALUES (?, ?, ?, ?, ?)", rows)
    return connection


def _run(connection: sqlite3.Connection, spec: QuerySpec) -> tuple[list[str], bool]:
    sql, params, cap = query_statement(_binding(), "?", spec)
    rows = [row[0] for row in connection.execute(sql, params).fetchall()]
    return rows[:cap], len(rows) > cap


def test_the_answer_is_the_filtered_rows_in_order_and_says_when_it_was_cut(
    warehouse: sqlite3.Connection,
) -> None:
    beta = QueryFilter(name="project", op="eq", value="PROJ-BETA")
    assert _run(warehouse, QuerySpec(filters=[beta])) == (["r5", "r6", "r2"], True)
    high = QueryFilter(name="yield_percent", op="gte", value=85)
    assert _run(warehouse, QuerySpec(filters=[beta, high])) == (["r5", "r1"], False)
    january = QueryFilter(name="recorded_on", op="between", value=[date(2024, 1, 9), "2024-01-10"])
    assert _run(warehouse, QuerySpec(filters=[january])) == (["r3", "r2"], False)


def test_contains_matches_a_wildcard_character_literally(warehouse: sqlite3.Connection) -> None:
    """`_` and `%` in the asked text are characters, not patterns: `J_5` must not match `J-5`."""
    asked = QueryFilter(name="project", op="contains", value="j_50%")
    assert _run(warehouse, QuerySpec(filters=[asked])) == (["r4"], False)
    assert _run(
        warehouse, QuerySpec(filters=[QueryFilter(name="project", op="contains", value="j-b")])
    )[0][:1] == ["r5"]


@pytest.mark.parametrize(
    "payload",
    [
        "x' OR '1'='1",
        "x'; DROP TABLE ELN.REACTIONS; --",
        "PROJ-BETA' --",
        "!%' OR 1=1 --",
        "') UNION SELECT name, 1, 1, 1, 1 FROM sqlite_master --",
    ],
)
def test_an_injection_in_any_value_position_is_data(
    warehouse: sqlite3.Connection, payload: str
) -> None:
    for wanted in (
        QueryFilter(name="project", op="eq", value=payload),
        QueryFilter(name="project", op="contains", value=payload),
        QueryFilter(name="project", op="in", value=[payload, "PROJ-ALPHA"]),
        QueryFilter(name="recorded_on", op="between", value=[payload, payload]),
    ):
        sql, _, _ = query_statement(_binding(), "?", QuerySpec(filters=[wanted]))
        assert payload not in sql
        rows, _ = _run(warehouse, QuerySpec(filters=[wanted]))
        assert set(rows) <= {"r3"}
    assert warehouse.execute("SELECT count(*) FROM ELN.REACTIONS").fetchone()[0] == 6
