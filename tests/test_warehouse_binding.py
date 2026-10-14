"""What a binding is checked for before a single row is read, and that the shipped one passes.

A site's schema is configuration, so a mistake in it must fail at load, offline, naming the line —
not on row 40,000. Most tests here are about rejection; the last group checks the shipped manifest
parses, resolves against a realistic row, and is discovered without being enabled.
"""

import ast
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from chemclaw.core.config import settings
from chemclaw.ingest.eln.warehouse import expr
from chemclaw.ingest.eln.warehouse.binding import BindingError, load_binding
from chemclaw.ingest.eln.warehouse.expr import (
    PatternBudgetError,
    TransformError,
    _cell_budget,
    apply_transforms,
    pattern_budget,
    resolve_path,
)

_SOURCES = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "ingest" / "sources"
_MANIFEST = _SOURCES / "eln-databricks" / "datasource.yaml"

# Every shipped warehouse manifest, with a row shaped the way that site's schema is. Casing matters:
# every path is an exact-case lookup on the row the driver returned (Spark keeps the schema's case;
# a warehouse that folds unquoted identifiers up wants capitals).
_SHIPPED_WAREHOUSES: list[tuple[str, dict[str, Any]]] = [
    (
        "eln-databricks",
        {
            "reaction_id": "RX-1",
            "project_code": "PRJ-7",
            "objective": "drop to 60 C",
            "protocol_text": "charge, reflux, work up",
            "experiment_date": "2026-05-01",
            "temp_c": "60",
            "duration_min": "90",
            "yield_pct": "82.5",
            "assay_pct": "99.1",
            "result_flag": "OK",
            "failure_note": "",
            "operator": "a.chemist",
        },
    ),
]


def _ingest(**overrides: Any) -> dict[str, Any]:
    """A minimal valid binding, with `overrides` applied to its `ingest:` section."""
    ingest: dict[str, Any] = {
        "entry": {"relation": "V_RX", "key": "ID", "created_at": "TS"},
        "related": [{"name": "charges", "relation": "V_CHG", "foreign_key": "ID"}],
        "reaction": {"reaction_id": {"path": "root.ID"}},
        "components": [
            {
                "from": "charges",
                "smiles": {"path": "SMILES"},
                "role": {"path": "TYPE", "transform": [{"value_map": {"map": {"S": "reactant"}}}]},
            }
        ],
        "provenance": "test:${root.ID}",
    }
    ingest.update(overrides)
    return {"connection": {"driver": "tests.warehouse_fake:open_fake"}, "ingest": ingest}


def test_a_binding_that_maps_a_field_ordreaction_does_not_have_is_rejected() -> None:
    """Checked against the real model's fields, so a typo cannot become a silently dropped value."""
    binding = _ingest()
    binding["ingest"]["reaction"]["yeild_percent"] = {"path": "root.Y"}

    with pytest.raises(BindingError, match="not mappable fields of OrdReaction"):
        load_binding(binding)


def test_a_binding_that_maps_an_engine_owned_field_says_why() -> None:
    """`inputs` comes from `components:`; mapping it would be two answers to the same question."""
    binding = _ingest()
    binding["ingest"]["reaction"]["inputs"] = {"path": "root.X"}

    with pytest.raises(BindingError, match="built by the engine"):
        load_binding(binding)


def test_a_binding_with_no_reaction_id_is_rejected() -> None:
    """The note's identity; without it every row would collide onto one note."""
    binding = _ingest()
    binding["ingest"]["reaction"] = {"project": {"path": "root.P"}}

    with pytest.raises(BindingError, match="must map 'reaction_id'"):
        load_binding(binding)


def test_a_components_block_reading_an_undeclared_table_is_rejected() -> None:
    """The mistake that would otherwise produce a reaction with no components and no explanation."""
    binding = _ingest()
    binding["ingest"]["components"][0]["from"] = "chargez"

    with pytest.raises(BindingError, match="not a related block"):
        load_binding(binding)


def test_a_role_vocabulary_that_does_not_produce_roles_is_rejected() -> None:
    """The likeliest binding typo, caught before it rejects every row the site ever recorded."""
    binding = _ingest()
    binding["ingest"]["components"][0]["role"]["transform"] = [
        {"value_map": {"map": {"S": "solvant"}}}
    ]

    with pytest.raises(BindingError, match="not roles"):
        load_binding(binding)


def test_an_unknown_transform_is_rejected_and_the_known_ones_are_listed() -> None:
    """The vocabulary is closed — what keeps a config file from being an execution surface."""
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"exec": {}}]

    with pytest.raises(BindingError, match="unknown transform 'exec'"):
        load_binding(binding)


def test_a_transform_missing_a_required_option_is_rejected() -> None:
    """`scale` with no factor would silently be an identity."""
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"scale": {}}]

    with pytest.raises(BindingError, match=r"transform 'scale' needs \['factor'\]"):
        load_binding(binding)


def test_an_identifier_that_is_not_an_identifier_is_rejected() -> None:
    """Relations and columns are written into SQL, so they are checked rather than trusted."""
    binding = _ingest()
    binding["ingest"]["entry"]["relation"] = "V_RX; DROP TABLE V_RX"

    with pytest.raises(BindingError, match="not a plain SQL identifier"):
        load_binding(binding)


def test_a_credential_value_pasted_where_a_variable_name_belongs_is_rejected() -> None:
    """Catches the realistic mistake: the field sits where a password goes in every other tool."""
    binding = _ingest()
    binding["connection"]["password_env"] = "hunter2-actual-secret"

    with pytest.raises(BindingError, match="NAME of an environment variable"):
        load_binding(binding)


def test_a_related_block_may_not_be_called_root() -> None:
    """`root` is the entry row's own key; shadowing it makes every `root.COL` path ambiguous."""
    binding = _ingest()
    binding["ingest"]["related"] = [{"name": "root", "relation": "V_X", "foreign_key": "ID"}]

    with pytest.raises(BindingError, match="cannot name a related block"):
        load_binding(binding)


def test_a_binding_with_neither_half_is_rejected() -> None:
    """A connection nothing would ever open is a configuration nobody meant to write."""
    with pytest.raises(
        BindingError, match="must declare an 'ingest', a 'corpus' or a 'vector' section"
    ):
        load_binding({"connection": {"driver": "tests.warehouse_fake:open_fake"}})


def test_extra_keys_are_refused_rather_than_ignored() -> None:
    """A misspelled key must fail, not silently disable the thing it was meant to configure."""
    binding = _ingest()
    binding["ingest"]["entry"]["modifed_at"] = "TS2"

    with pytest.raises(BindingError, match="invalid warehouse binding"):
        load_binding(binding)


def test_a_value_map_miss_without_a_default_raises_rather_than_yielding_nothing() -> None:
    """A vocabulary the site extended must be loud, not a quietly missing field.

    A `TransformError` rather than a `BindingError`: the binding is well-formed, the *row* carried a
    value it does not cover — so this is one rejected row in `sync_entries`, not a refusal to start.
    """
    with pytest.raises(TransformError, match="no entry for 'NEW'"):
        apply_transforms("NEW", [{"value_map": {"map": {"OLD": "reactant"}}}])


def test_a_value_map_with_a_default_absorbs_the_unknown_value() -> None:
    """`default:` is how a binding says 'and everything else is this'."""
    assert apply_transforms("NEW", [{"value_map": {"map": {"OLD": "a"}, "default": "b"}}]) == "b"


def test_a_path_that_does_not_resolve_is_silence_not_an_error() -> None:
    """A NULL column, an absent child table and a dropped column all mean 'the source is silent'."""
    payload = {"root": {"A": 1}, "charges": [{"B": 2}]}

    assert resolve_path("root.A", payload) == 1
    assert resolve_path("charges[0].B", payload) == 2
    assert resolve_path("root.MISSING", payload) is None
    assert resolve_path("charges[9].B", payload) is None
    assert resolve_path("analytics[0].C", payload) is None


@pytest.mark.parametrize("source", [name for name, _ in _SHIPPED_WAREHOUSES])
def test_the_shipped_manifest_binding_is_valid(source: str) -> None:
    """Every example this repository ships parses under the same rules a real one will."""
    path = _SOURCES / source / "datasource.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    binding = load_binding(manifest["config"]["binding"])

    assert binding.ingest is not None and binding.vector is not None
    assert manifest["name"] == path.parent.name
    assert manifest["ingest"].endswith(":WarehouseElnAdapter")
    assert manifest["retrieve"].endswith(":WarehouseVectorRetriever")


@pytest.mark.parametrize(("source", "row"), _SHIPPED_WAREHOUSES)
def test_every_path_in_the_shipped_manifest_resolves_against_a_realistic_row(
    source: str, row: dict[str, Any]
) -> None:
    """The worked example is worked — not a plausible-looking file nobody ever ran.

    The row is written out per source rather than derived from the binding, because a derived row
    resolves by construction and would assert nothing.
    """
    manifest = yaml.safe_load((_SOURCES / source / "datasource.yaml").read_text(encoding="utf-8"))
    binding = load_binding(manifest["config"]["binding"])
    assert binding.ingest is not None

    payload: dict[str, Any] = {"root": row, "charges": [], "analytics": []}

    unresolved = [
        name
        for name, field in binding.ingest.reaction.items()
        if resolve_path(field.path, payload) is None and name != "failure_reason"
    ]
    assert unresolved == [], f"shipped example paths that resolve to nothing: {unresolved}"


def test_a_connection_block_may_name_any_driver_s_own_keywords() -> None:
    """The model declares `driver:` and nothing else, so a vendor's words are the driver's business.

    Three databases with unrelated vocabularies load here; what checks a key is real is the driver's
    own signature, bound offline by `make datasource-validate`.
    """
    for connection in (
        {"driver": "acme.pg:Postgres", "host": "db", "port": 5432, "sslmode": "require"},
        {"driver": "acme.lake:Lakehouse", "server_hostname_env": "HOST", "warehouse_id": "w"},
        {"driver": "acme.vec:Milvus", "uri": "acme://v:9000", "api_key_env": "K", "dim": 1536},
    ):
        binding = _ingest()
        binding["connection"] = connection
        assert load_binding(binding).connection.driver == connection["driver"]


def test_a_pasted_secret_in_an_env_key_is_refused_whatever_the_key_is_called() -> None:
    """A pasted secret is refused for a keyword this repository has never seen.

    A `*_env` key holds the NAME of an environment variable. The credential words are the driver's,
    so the suffix triggers the check rather than a list of known fields.
    """
    binding = _ingest()
    binding["connection"] = {"driver": "acme.vec:Milvus", "service_account_key_env": "sk-live-1"}

    with pytest.raises(BindingError, match="NAME of an environment variable"):
        load_binding(binding)


@pytest.mark.parametrize("written_as", ["", None])
def test_an_env_key_left_blank_is_refused_rather_than_dropped(written_as: str | None) -> None:
    """A key present and empty is a credential the author meant to supply, not one they omitted.

    Dropping it would construct the driver without the keyword: a defaulted credential attaches
    anonymously, and a required one raises a bare `TypeError` that `durable/publish` would retry
    forever. The signature check cannot see this case, so the binding refuses it.
    """
    binding = _ingest()
    binding["connection"] = {"driver": "acme.vec:Milvus", "access_token_env": written_as}

    with pytest.raises(BindingError, match="left blank|NAME of an environment variable"):
        load_binding(binding)


def test_a_blank_env_key_fails_where_the_options_are_built_too() -> None:
    """The same rule at the second gate, for a mounted manifest no CI run ever bound.

    Credentials are re-checked in `connect_options`, where they are actually resolved, not only
    where a manifest loads.
    """
    from chemclaw.core.connect import connect_options

    block = {
        "driver": "acme.vec:Milvus",
        "uri": "acme://v:9000",
        "api_key_env": "",
    }
    with pytest.raises(BindingError, match="left blank"):
        connect_options(block, error=BindingError, what="warehouse connection")


def test_a_connection_key_the_driver_will_not_take_is_caught_offline() -> None:
    """A connection key the driver will not take fails offline, bound against the callable.

    `ConnectionBinding` cannot know what a driver accepts; `make datasource-validate` binds the
    block against the driver's signature, so a key copied from another vendor fails in CI, not in a
    worker.
    """
    from chemclaw.cli.validate_datasources import _check_connection
    from chemclaw.ingest.sources.manifest import DataSourceManifest

    manifest = DataSourceManifest(
        name="eln-elsewhere",
        description="a warehouse ELN whose binding was copied from another vendor's",
        ingest="chemclaw.ingest.eln.warehouse.adapter:WarehouseElnAdapter",
        config={
            "binding": {
                "connection": {
                    "driver": "chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse",
                    "server_hostname_env": "HOST",
                    "access_token_env": "TOKEN",
                    "warehouse_id": "w",
                    "role": "CHEMCLAW_READER",
                }
            }
        },
    )
    problems = _check_connection("eln-elsewhere", manifest)
    assert problems and "role" in problems[0], problems


def test_a_key_the_driver_will_not_take_fails_as_this_seams_error_at_connect_time() -> None:
    """A deployment's mounted manifest gets the signature check again where the driver is built.

    The error class is the point: `BindingError` is non-retryable by class name in
    `durable/publish`, while a constructor's bare `TypeError` would be retried by every job touching
    the manifest.
    """
    from chemclaw.core.connect import open_connection

    block = {
        "driver": "chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse",
        "server_hostname": "adb.example.net",
        "access_token": "dapi-token",
        "warehouse_id": "abc123",
        "role": "CHEMCLAW_READER",
    }
    with pytest.raises(BindingError, match="role"):
        open_connection(block, error=BindingError, what="warehouse connection")


def test_a_driver_whose_signature_cannot_be_read_still_fails_as_this_seams_error() -> None:
    """A C `connect` with no introspectable signature still fails as this seam's error.

    `inspect.signature` raises `ValueError` for such callables (`sqlite3.connect`,
    `duckdb.connect`), which must not escape `open_connection` unnamed. Both halves are asserted: it
    opens, and a keyword the driver refuses is still refused, by the constructor if not offline.
    """
    from chemclaw.core.connect import open_connection, signature_mismatch

    block = {"driver": "sqlite3:connect", "database": ":memory:"}
    assert signature_mismatch(sqlite3.connect, block) == ""
    connection = open_connection(block, error=BindingError, what="warehouse connection")
    assert isinstance(connection, sqlite3.Connection)
    connection.close()

    with pytest.raises(TypeError):
        open_connection(
            {**block, "role": "CHEMCLAW_READER"}, error=BindingError, what="warehouse connection"
        )


@pytest.mark.parametrize("source", ["eln-databricks", "pistachio"])
def test_a_warehouse_source_is_discovered_but_not_enabled(source: str) -> None:
    """Shipping a source is not attaching it: a deployment enables what it has validated (D-018)."""
    from chemclaw.ingest.sources.registry import discovered

    assert source in discovered()
    assert source not in settings.data_source_list


def test_construct_validation_catches_a_binding_that_binding_alone_cannot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--construct` closes the gap between "the kwargs fit" and "the config makes sense".

    A whole binding arrives under one `binding=` keyword, so signature binding alone would pass a
    mistyped column path.
    """
    from chemclaw.cli.validate_datasources import validate_datasources
    from chemclaw.ingest.sources import registry

    broken = _ingest()
    broken["ingest"]["reaction"]["reaction_id"]["transform"] = [{"exek": {}}]
    source = tmp_path / "eln-broken"
    source.mkdir()
    (source / "datasource.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "eln-broken",
                "description": "A source whose binding will not build.",
                "ingest": "chemclaw.ingest.eln.warehouse.adapter:WarehouseElnAdapter",
                "config": {"binding": broken},
            }
        ),
        encoding="utf-8",
    )
    # Prepended rather than substituted, exactly as a deployment mounts its own manifests: the
    # shipped sources stay discovered, so the only new problem is the one under test.
    monkeypatch.setattr(
        settings,
        "data_sources_dir",
        f"{source.parent}{os.pathsep}{settings.data_sources_dir}",
    )
    registry.forget_discovered()
    try:
        assert validate_datasources() == [], "binding the kwargs alone cannot see the typo"
        problems = validate_datasources(construct=True)
    finally:
        registry.forget_discovered()

    assert any("eln-broken" in problem and "exek" in problem for problem in problems), problems


def test_both_halves_accept_the_same_config_keywords() -> None:
    """The registry splats one `config:` into whichever half it builds — they must agree.

    A signature drift between the two would pass every test that built one half and fail
    `make datasource-validate`, which binds the config against both.
    """
    import inspect

    from chemclaw.ingest.eln.warehouse.adapter import WarehouseElnAdapter
    from chemclaw.ingest.eln.warehouse.retriever import WarehouseVectorRetriever

    ingest = inspect.signature(WarehouseElnAdapter).parameters
    retrieve = inspect.signature(WarehouseVectorRetriever).parameters
    assert list(ingest) == list(retrieve) == ["binding", "name"]


def test_a_template_renders_a_falsy_value_rather_than_dropping_it() -> None:
    """`0` is a value the source recorded, not an absent one.

    In a provenance string an id of `0` rendered as empty would cite nothing.
    """
    from chemclaw.ingest.eln.warehouse.expr import render_template

    scope = {"root": {"ID": 0, "OPERATOR": "", "PAGE": 12}}
    assert render_template("eln:${root.ID}:${root.PAGE}", scope) == "eln:0:12"
    assert render_template("eln:${root.MISSING}", scope) == "eln:"


def test_a_numeric_site_vocabulary_maps_rather_than_rejecting_every_row() -> None:
    """A numeric site vocabulary maps rather than rejecting every row.

    YAML makes `map: {1: reactant}` an integer key, so both sides are compared as text.
    """
    numeric = [{"value_map": {"map": {1: "reactant", 2: "solvent"}}}]

    assert apply_transforms(1, numeric) == "reactant", "an integer row value"
    assert apply_transforms("2", numeric) == "solvent", "and its string spelling"


def test_a_yaml_boolean_map_key_is_refused_with_the_fix_named() -> None:
    """`ON`/`OFF`/`YES`/`NO`/`Y`/`N` are YAML booleans, and the spelling is gone before we see it.

    `True` and `1` are also the same dict key, so the map may already have lost an entry. Refused at
    load, naming the line to quote.
    """
    with pytest.raises(BindingError, match="boolean key"):
        load_binding(
            _ingest(
                components=[
                    {
                        "from": "charges",
                        "smiles": {"path": "SMILES"},
                        "role": {
                            "path": "TYPE",
                            # `Y:` in the source YAML — a boolean by the time pydantic sees it.
                            "transform": [{"value_map": {"map": {True: "reactant"}}}],
                        },
                    }
                ]
            )
        )


def test_a_regex_transform_is_compiled_when_the_binding_loads() -> None:
    """A regex transform is compiled, and its `group:` checked, when the binding loads.

    Otherwise an unbalanced bracket or missing group surfaces only on the first matching row.
    """
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"regex": {"pattern": "["}}]
    with pytest.raises(BindingError, match="invalid pattern"):
        load_binding(binding)

    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [
        {"regex": {"pattern": "L-(\\d+)", "group": 5}}
    ]
    with pytest.raises(BindingError, match="asks for group 5"):
        load_binding(binding)


def test_a_pattern_that_cannot_finish_stops_on_a_wall_clock_instead_of_on_the_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catastrophic pattern stops on the per-cell wall clock instead of the activity deadline.

    The site writes the pattern and the subject is free text, so the engine's timeout is the only
    bound. The assertion separates "returns at the budget" from "never returns", so the bound is a
    generous multiple of the budget to stay stable on a loaded box.
    """
    monkeypatch.setattr(settings, "eln_regex_timeout_seconds", 0.05)
    catastrophic = [{"regex": {"pattern": "(a+)+$"}}]

    started = time.perf_counter()
    with pytest.raises(PatternBudgetError, match="did not finish within"):
        apply_transforms("a" * 3_000 + "b", catastrophic)
    spent = time.perf_counter() - started

    assert spent < 5.0, (
        f"the match ran {spent:.2f}s against a 0.05s budget, so the deadline is not being checked "
        "inside the matching loop and the only real bound is still the activity's"
    )


def test_no_handler_on_the_ingest_path_catches_a_pattern_that_cannot_finish() -> None:
    """No exception handler on the ingest path catches `PatternBudgetError`.

    Every `except` clause under `ingest/eln/` is resolved to the classes it catches, and none may be
    a base of `PatternBudgetError`; otherwise a page would book each slow row as a data refusal and
    cost `rows x budget`. Walking all handlers catches one added or widened later.
    """
    import importlib

    package = Path(__file__).resolve().parents[1] / "src" / "chemclaw" / "ingest" / "eln"
    catchers: list[str] = []
    for path in sorted(package.rglob("*.py")):
        module_name = (
            "chemclaw.ingest.eln"
            + "."
            + str(path.relative_to(package).with_suffix("")).replace("/", ".")
        ).removesuffix(".__init__")
        namespace = vars(importlib.import_module(module_name))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            named = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            for element in named:
                if not isinstance(element, ast.Name):
                    continue
                caught = namespace.get(element.id)
                if isinstance(caught, type) and issubclass(PatternBudgetError, caught):
                    catchers.append(f"{module_name}:{node.lineno} catches {element.id}")

    assert not catchers, (
        "a pattern that cannot finish is swallowed on the ingest path and the row it was reading "
        f"is booked as a data refusal, so the same unfinishable match is re-run on every remaining "
        f"row: {catchers}. The cost belongs to the pattern, not to the row"
    )


def test_a_pattern_that_cannot_finish_is_still_refused_once_rather_than_retried() -> None:
    """A pattern that cannot finish is refused once rather than retried.

    `durable/publish._BAD_DATA_TYPES` matches the failure's class name, and a retry would run the
    same pattern over the same page.
    """
    from chemclaw.durable.publish import _BAD_DATA_TYPES

    assert PatternBudgetError.__name__ in _BAD_DATA_TYPES, (
        "the pattern and the page are both the same on the next attempt, so a retry is the stall "
        "again"
    )


def test_a_repeat_count_too_large_to_expand_is_refused_before_it_is_compiled() -> None:
    """A repeat count too large to expand is refused before it is compiled.

    `regex` expands bounded repeats at compile time (unlike `re`), so a huge `{n}` in a manifest
    could exhaust an ingest worker at binding load, outside every match timeout and Temporal
    deadline. The guard is a scan, because compiling is the thing being guarded.
    """
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [
        {"regex": {"pattern": "a{200000}"}}
    ]

    started = time.perf_counter()
    with pytest.raises(BindingError, match="over the 10000 this engine will expand"):
        load_binding(binding)
    spent = time.perf_counter() - started

    assert spent < 1.0, (
        f"the refusal itself took {spent:.2f}s, which is the pattern being compiled before it was "
        "refused — the guard has to run first or it is not a guard"
    )


@pytest.mark.parametrize(
    ("pattern", "because"),
    [
        ("(?#[)a{200000}", "a `[` inside an inline comment opens no class"),
        ("(?x)#[\na{200000}", "verbose mode can comment a `[` out of the scan's sight"),
        ("(?:a{1000}){1000}", "nested repeats multiply: a million atoms, each count legal"),
        ("(?:(?:(?:a{100}){100}){100}){100}", "the case that never finishes, every count 100"),
        ("a{9000}" * 12, "siblings add up: the whole pattern's expansion is bounded too"),
        ("(?P<n>ab){5001}", "a named group's body is what a repeat multiplies"),
    ],
)
def test_the_expansion_guard_sees_what_a_per_quantifier_scan_did_not(
    pattern: str, because: str
) -> None:
    """The expansion guard sees nested repeats and patterns that hide a `[` in a comment.

    `regex` expands nested bounded repeats multiplicatively, so per-quantifier limits are not
    enough. Timed, because a refusal that compiled first is not a guard.
    """
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"regex": {"pattern": pattern}}]

    started = time.perf_counter()
    with pytest.raises(BindingError, match="this engine will expand|verbose mode"):
        load_binding(binding)

    assert time.perf_counter() - started < 1.0, because


@pytest.mark.parametrize(
    ("pattern", "because"),
    [
        (r"\d{3,6}", "a field width, which is what a real binding writes"),
        (r"[A-Z]{2,4}-\d+", "a bounded class repeat"),
        (r"a{2,}", "unbounded: the other remedy's subject, and free to compile"),
        (r"\{100000\}", "an escaped brace is a literal, not a quantifier"),
        (r"[{]{1}", "a brace inside a character class, repeated once"),
        (r"x{not a number}", "a brace group that is not a quantifier at all"),
        (r"(?:\d{3}){2}", "a nested bounded repeat whose product is small"),
        (r"\p{L}+", "`regex` syntax `re` would refuse, which is why this is a scan"),
        (r"(?i-x:ab)", "a flag group turning verbose mode off hides nothing"),
        (r"(?#note)L-(\d+)", "an inline comment is skipped, not refused"),
        (r"(?P<name>a){2000}", "a group's name is syntax, not atoms the repeat multiplies"),
        (r"(?P<abcdefgh>a){9999}", "however long the name, under the old per-count limit"),
        (r"(?<n>a)(?P=n){9999}", "a backreference is one atom"),
        (r"(?<=a)b{10000}(?i:c)", "lookarounds and scoped flags weigh nothing either"),
        (r"^[^,]{0,10000},", "a repeat at the per-count limit followed by a literal"),
        (r"(?:,[^,]{0,10000}){0,1}", "`{0,1}` duplicates nothing, exactly like `?`"),
        (r"(?:,[^,]{0,10000}){1}", "`{1}` duplicates nothing either"),
        ("a{9000}" * 3, "siblings under the whole-pattern bound, each under the per-count one"),
    ],
)
def test_the_expansion_guard_does_not_refuse_a_pattern_a_binding_would_write(
    pattern: str, because: str
) -> None:
    """The expansion guard does not refuse a pattern a binding would write.

    An escaped brace and a brace in a character class are literals, not quantifiers; the scan tracks
    exactly those two cases.
    """
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"regex": {"pattern": pattern}}]

    load_binding(binding)

    assert because, "every row states why it is a pattern a binding would legitimately write"


def test_an_ordinary_pattern_still_reads_its_group_under_the_bounded_engine() -> None:
    """An ordinary pattern still reads its group under the bounded engine.

    Covers the shapes bindings write — a group, an alternation, a bounded quantifier and a character
    class.
    """
    assert apply_transforms("L-40127 batch", [{"regex": {"pattern": r"L-(\d+)", "group": 1}}]) == (
        "40127"
    )
    assert (
        apply_transforms(
            "sample XYZ-12345", [{"regex": {"pattern": r"([A-Z]{2,4}-\d{3,6})", "group": 1}}]
        )
        == "XYZ-12345"
    )
    assert (
        apply_transforms(
            "isolated 87.4 % after", [{"regex": {"pattern": r"(\d+(?:\.\d+)?)\s*%", "group": 1}}]
        )
        == "87.4"
    )
    assert apply_transforms("no digits here", [{"regex": {"pattern": r"L-(\d+)"}}]) is None, (
        "no match is silence, not an error"
    )


def test_the_server_embed_function_is_checked_like_every_other_interpolated_name() -> None:
    """The server embed function is checked like every other interpolated name.

    `vector_statement` renders it into the SQL text as `fn(...)`, so a non-identifier could inject
    SQL. A dotted name still passes, because the real Cortex embedder is one.
    """

    def _with(function: str) -> dict[str, Any]:
        return {
            "connection": {"driver": "tests.warehouse_fake:open_fake"},
            "vector": {
                "relation": "V_EMBEDDING",
                "key": "REACTION_ID",
                "vector_column": "REACTION_VECTOR",
                "content_columns": ["PROTOCOL_TEXT"],
                "embedding": "server",
                "server_embed_function": function,
            },
        }

    with pytest.raises(BindingError, match="server embed function"):
        load_binding(_with("CAST(1 AS INT))=1 OR (1"))

    assert load_binding(_with("main.ml.embed_text")).vector is not None, (
        "a qualified function name is still accepted"
    )


@pytest.mark.parametrize("written_as", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_a_non_finite_number_is_bad_data_rather_than_a_measurement(written_as: Any) -> None:
    """`'number'` refuses NaN and ±Infinity, whichever side of the column they arrive on.

    A NaN is not a measurement; it must reach the rejection ledger with a reason rather than reach
    `jsonb`, whose `psycopg` error would abort the whole sync pass.
    """
    with pytest.raises(TransformError, match="not a measurement"):
        apply_transforms(written_as, [{"number": {}}])


def test_clamp_refuses_the_one_number_it_cannot_hold_in_a_range() -> None:
    """`max(nan, 0.0)` is `nan`, so the explicit guard was the thing that failed to guard.

    Every comparison against NaN is false, which is why clamping silently passed it through while
    `{min: 0, max: 100}` was written precisely to guarantee a value inside those bounds.
    """
    with pytest.raises(TransformError, match="not a measurement"):
        apply_transforms(float("nan"), [{"clamp": {"min": 0.0, "max": 100.0}}])
    assert apply_transforms(101.3, [{"clamp": {"min": 0.0, "max": 100.0}}]) == 100.0, (
        "an out-of-range but finite number is still held, which is what clamp is for"
    )


# A pattern whose cost is *polynomial* rather than exponential, so it lands inside the per-cell
# budget instead of blowing through it. This is the case the per-cell bound cannot see, and the
# reason the page bound exists: it returns a match rather than raising.
_SLOW_BUT_COMPLETING = {"regex": {"pattern": r"a*a*a*$"}}


def _a_cell_this_machine_finishes() -> tuple[str, float]:
    """The longest of a fixed ladder of cells whose warm cost is well inside the per-cell budget.

    Sized against this machine rather than fixed, so a slow runner does not fire the per-cell arm
    where the page tests need a slow-but-completing cell. `_MARGIN` is the fraction of the per-cell
    budget the chosen cell may cost; the ladder's floor refuses a cell that is merely fast.
    """
    budget = settings.eln_regex_timeout_seconds
    for length in (6000, 4000, 2500, 1500, 1000):
        cell = "a" * length + "b"
        try:
            apply_transforms(cell, [_SLOW_BUT_COMPLETING])  # warm the compile cache
            start = time.perf_counter()
            apply_transforms(cell, [_SLOW_BUT_COMPLETING])
        except PatternBudgetError:
            continue  # this rung is past the per-cell budget on this machine; try a shorter one
        cost = time.perf_counter() - start
        if cost <= _MARGIN * budget:
            return cell, cost
    raise AssertionError(
        f"no cell in the ladder costs under {_MARGIN:.0%} of the {budget}s per-cell budget on this "
        "machine, so the page tests below cannot separate the page arm from the per-cell one. "
        "Either the machine is extraordinarily slow or `a*a*a*$` has stopped being polynomial"
    )


#: How much of the per-cell budget the sized cell above may cost. The inverse is the slowdown a
#: machine needs before the per-cell arm fires where a page refusal is expected.
_MARGIN = 0.3
_SLOW_CELL, _SLOW_CELL_COST = _a_cell_this_machine_finishes()

#: What a real binding's pattern costs, for the ratio the budget's generosity rests on: 0.0024 ms
#: warm. An earlier comment said 0.472 ms, which was the first call including the `lru_cache`
#: compile miss — so every test below warms the cache before it times anything.
_HONEST = {"regex": {"pattern": r"(\d{3,6})"}}


def test_a_page_of_slow_but_completing_cells_is_refused_before_the_activity_deadline() -> None:
    """A page of slow but completing cells is refused before the activity deadline.

    Per-cell timeouts do not add up (the first ends the page), but many slow completing cells can
    exceed `eln_sync_timeout_seconds`, and synchronous mapping is not interruptible. Driven at a
    small budget because the property is the ratio, not the number.
    """
    apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])  # warm the compile cache
    spent = time.perf_counter()
    read = 0
    with pytest.raises(PatternBudgetError, match="matching budget") as refused:
        with pattern_budget(0.5):
            for _ in range(500):
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])
                read += 1
    elapsed = time.perf_counter() - spent

    assert read, "nothing was read, so this measured the first cell rather than a page"
    assert "transform(s) ran" in str(refused.value) or "transform(s), the last" in str(
        refused.value
    ), (
        "the refusal must say how far the page got — that number is what separates 'this binding "
        "is too expensive for this page size' from 'one pattern is pathological'; it said "
        f"{refused.value}"
    )
    # The clamp is the point: each search may run for the *lesser* of the per-cell budget and what
    # the page has left, so the last one cannot carry the page a whole cell-budget past its bound.
    assert elapsed < 0.5 + settings.eln_regex_timeout_seconds, (
        f"the page overshot its 0.5s budget by {elapsed - 0.5:.3f}s, which is more than the clamp "
        "should allow"
    )


def test_an_honest_page_is_nowhere_near_the_budget() -> None:
    """An honest page stays far cheaper than one pathological cell.

    The bad cell is the longest this machine runs inside the per-cell budget, not `_SLOW_CELL`:
    shrinking the pathological side would invert the ratio being asserted. Below 2,500 characters
    the test skips rather than measure something else.
    """
    cells = 2000
    apply_transforms("batch 4471 of 12", [_HONEST])  # warm the compile cache; see `_HONEST`
    pathological = ""
    for length in (6000, 4000, 2500):
        candidate = "a" * length + "b"
        try:
            apply_transforms(candidate, [_SLOW_BUT_COMPLETING])  # warm, and prove it completes
        except PatternBudgetError:
            continue
        pathological = candidate
        break
    if not pathological:
        pytest.skip(
            "no pathological cell of 2,500 characters or more completes inside the "
            f"{settings.eln_regex_timeout_seconds}s per-cell budget on this machine, so the ratio "
            "this test is about would be measured against a cell too small to mean it"
        )
    started = time.perf_counter()
    apply_transforms(pathological, [_SLOW_BUT_COMPLETING])
    one_bad_cell = time.perf_counter() - started

    started = time.perf_counter()
    with pattern_budget():
        for _ in range(cells):
            apply_transforms("batch 4471 of 12", [_HONEST])
    spent = time.perf_counter() - started

    assert spent < one_bad_cell, (
        f"{cells} honest cells cost {spent:.4f}s, which is no longer cheaper than the single "
        f"{len(pathological)}-character pathological cell this bound exists for "
        f"({one_bad_cell:.4f}s); the ratio the budget's generosity rests on is gone"
    )


def test_the_page_budget_does_not_charge_what_happens_between_matches() -> None:
    """The page budget charges matching time, not the wall clock between matches.

    The page loop awaits stores and source fetches; billing those to a regex budget would refuse
    cheap patterns and, being non-retryable, fail a page permanently that should have been retried.
    """
    apply_transforms("batch 4471 of 12", [_HONEST])
    matched = 0.0

    with pattern_budget(0.25):
        for _ in range(20):
            started = time.perf_counter()
            apply_transforms("batch 4471 of 12", [_HONEST])
            matched += time.perf_counter() - started
            time.sleep(0.02)

    assert matched < 0.01, f"the matching itself cost {matched:.4f}s, so this arm proves little"


class _BilledClock:
    """A clock on which every regex search costs exactly `cost`, whatever the host is doing.

    It advances by `cost` at each reading and a search reads it twice (before and after), so the
    page is billed `cost` per search. The page's accounting is under test, not the host's speed.
    """

    def __init__(self, cost: float) -> None:
        """Start at zero; `cost` is the seconds each search is billed."""
        self._cost = cost
        self._now = 0.0

    def __call__(self) -> float:
        """The next reading."""
        self._now += self._cost
        return self._now


def test_a_pattern_cut_short_by_the_page_is_not_reported_as_innocent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pattern cut short by the page is not reported as innocent.

    A clamped search times out exactly when the page runs dry, so the refusal must still name the
    pattern when it was the one running. The page is billed by a fake clock, three searches of one
    cost against a budget of three and a half, so the catastrophic pattern is always given the
    remaining half cost: the host's speed cannot spend the page before it runs, nor leave it enough
    for the pattern to finish.
    """
    cost = settings.eln_regex_timeout_seconds
    monkeypatch.setattr(expr, "monotonic", _BilledClock(cost))
    catastrophic = {"regex": {"pattern": r"(a+)+$"}}

    searches = -1
    with pytest.raises(PatternBudgetError) as refused:
        with pattern_budget(3.5 * cost):
            try:
                for _ in range(3):
                    apply_transforms("batch 4471 of 12", [_HONEST])
                apply_transforms("a" * 4000 + "b", [catastrophic])
            finally:
                page = expr._page_budget.get()
                searches = page.searches if page is not None else -1

    message = str(refused.value)
    assert searches == 4, "three searches billed whole and the fourth cut short"
    assert "not established here" in message, message
    assert "No single one exceeded" not in message, (
        "the page refusal claimed every transform stayed inside its ceiling, about a pattern that "
        f"was never given its full allowance: {message}"
    )


def test_a_spent_page_never_offers_the_engine_a_negative_timeout() -> None:
    """`regex` reads a negative `timeout` as *no* timeout, which would disable the bound entirely.

    `_cell_budget`'s `remaining <= 0.0` arm keeps a negative out; this pins it.
    """
    assert _cell_budget()[0] > 0.0, "no page open should give the per-cell budget, not a negative"

    with pattern_budget(0.05):
        for _ in range(500):
            try:
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])
            except PatternBudgetError:
                break
        budget, page_bound = _cell_budget()

    assert budget >= 0.0, (
        f"a spent page offered {budget}s to the engine, which disables the timeout"
    )
    assert page_bound, "a spent page must report that it is the binding constraint"


def test_the_two_refusals_name_different_causes() -> None:
    """One refusal names a pattern to rewrite; the other names a costly page.

    This drives the unclamped per-cell arm; the clamped case is
    `test_a_pattern_cut_short_by_the_page_is_not_reported_as_innocent`.
    """
    with pytest.raises(PatternBudgetError, match="did not finish within") as cell:
        with pattern_budget(30.0):
            apply_transforms("a" * 4000 + "b", [{"regex": {"pattern": r"(a+)+$"}}])
    assert "matching budget" not in str(cell.value)

    with pytest.raises(PatternBudgetError, match="matching budget") as page:
        with pattern_budget(0.3):
            for _ in range(500):
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])
    assert "did not finish within" not in str(page.value)


def test_a_page_refusal_quotes_the_budget_actually_in_force() -> None:
    """A page refusal quotes the budget actually in force, not the configured default."""
    with pytest.raises(PatternBudgetError, match="whole 0.4s matching budget"):
        with pattern_budget(0.4):
            for _ in range(500):
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])


def test_a_nested_budget_keeps_the_outer_deadline() -> None:
    """A nested budget keeps the outer deadline.

    `sync_entries` opens one and calls `_replay_record_ids`, which maps entries too; restarting
    would make the bound `pages x budget`.
    """
    with pattern_budget(0.3):
        with pytest.raises(PatternBudgetError, match="spent their whole 0.3s"):
            with pattern_budget(600.0):
                for _ in range(500):
                    apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])


def test_no_budget_open_leaves_the_per_cell_bound_exactly_as_it_was() -> None:
    """With no budget open, the per-cell bound is the only one reachable.

    The page bound is additive, so a one-off mapping is no stricter than before.
    """
    with pytest.raises(PatternBudgetError, match="did not finish within"):
        apply_transforms("a" * 4000 + "b", [{"regex": {"pattern": r"(a+)+$"}}])
    assert apply_transforms("batch 4471 of 12", [_HONEST]) == "4471"


# The two entry points a site's transforms run through: a whole ELN entry, and one bound field.
_MAPPERS = frozenset({"map_to_ord", "apply_transforms"})


def _called_names(node: ast.AST) -> set[str]:
    """Every name a call under `node` reaches, as `f(...)` or `obj.f(...)`."""
    names = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _modules_mapping_entries_in_a_loop() -> list[str]:
    """Every first-party module with a loop that **reaches a mapper**, directly or via a helper.

    A loop counts when it calls `map_to_ord`/`apply_transforms` or a module-local function that
    reaches one (closed to a fixpoint). Modules that *define* a mapper hold per-entry work, not a
    page loop, and are skipped; per-entry wrappers run under their caller's budget.
    """
    source_root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    found = []
    for path in sorted(source_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not any(mapper in text for mapper in _MAPPERS):
            continue
        tree = ast.parse(text)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if _MAPPERS & functions.keys():
            continue
        reaching = set(_MAPPERS)
        while True:
            grown = reaching | {
                name for name, node in functions.items() if _called_names(node) & reaching
            }
            if grown == reaching:
                break
            reaching = grown
        for node in ast.walk(tree):
            if isinstance(node, ast.For | ast.AsyncFor | ast.While) and (
                _called_names(node) & reaching
            ):
                found.append(str(path.relative_to(source_root)))
                break
    return found


def test_every_module_that_maps_entries_in_a_loop_enters_the_page_budget() -> None:
    """Every module that maps entries in a loop enters the page budget.

    Derived rather than one behavioural test per caller, which covers only the callers somebody
    thought of. It does not check placement (one budget per entry would pass), but a new page loop
    with no budget goes red.
    """
    source_root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    missing = [
        name
        for name in _modules_mapping_entries_in_a_loop()
        if "pattern_budget" not in (source_root / name).read_text(encoding="utf-8")
    ]

    assert not missing, (
        "these modules map ELN entries in a loop and open no regex page budget, so the per-cell "
        f"ceiling multiplies by the page with nothing bounding the product: {missing}"
    )


def test_that_guard_is_measuring_the_callers_and_not_the_definitions() -> None:
    """The guard above would pass vacuously on an empty set, so this pins what it found.

    Adapters that define `map_to_ord` are not callers; requiring the known callers makes the guard a
    measurement rather than a tautology.
    """
    callers = _modules_mapping_entries_in_a_loop()

    assert len(callers) >= 5, (
        "fewer page-mapping callers than the five measured (ingest/eln/sync.py, "
        "durable/memory_jobs.py, cli/live_data.py, ingest/eln/validate.py, "
        f"ingest/labels/corpus.py), so the guard above asserts little: {callers}"
    )
    assert "ingest/labels/corpus.py" in callers, (
        "the reaction-corpus drain reaches `apply_transforms` through its own helpers, and the "
        f"guard no longer sees it: {callers}"
    )
