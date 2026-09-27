"""What a binding is checked for before a single row is read, and that the shipped one passes.

The engine's promise is that a site's schema is configuration. That is only worth having if a
mistake in the configuration is caught the way a mistake in code would be — at startup, naming the
line, offline. So these tests are mostly about *rejection*: the binding that would have failed on
row 40,000 must fail on load instead.

The last group closes the loop the other way, on the manifest this repository actually ships: its
binding parses, every path in it resolves against a realistic row, and it is discovered without
being enabled.
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

# Every shipped warehouse manifest, with a row shaped the way that site's schema is. A worked
# example a binding author copies is held to the same two checks as a hand-written one, and the
# *casing* matters, because every path here is an exact-case lookup on the row the driver
# returned: Spark gives back the schema's own case, while a warehouse that folds unquoted
# identifiers up wants the binding written in capitals.
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

    A shipped example whose paths do not resolve is worse than none: it is the thing a binding
    author copies, and it would teach a shape that silently yields nothing. The row is written out
    per source rather than derived from the binding, because a derived row resolves by construction
    and would assert nothing at all.
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

    This used to be a model enumerating one warehouse's connection fields, which meant the *second*
    driver had to redefine three of them and refuse two more, and the result-sink seam refused to
    reuse the model at all. Three databases with three unrelated vocabularies load here — that is
    the whole claim of `D-2026-08-26-the-driver-s-signature-is-the-schema`, and what checks a key is
    real is the driver's own signature, bound offline by `make datasource-validate`.
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
    """The realistic mistake, caught for a keyword this repository has never seen.

    A `*_env` key holds the NAME of an environment variable. The check cannot be a list of known
    credential fields — the whole point is that the credential words are the driver's — so it is the
    suffix that triggers it, and a driver inventing `service_account_key_env` is covered on the day
    it is written.
    """
    binding = _ingest()
    binding["connection"] = {"driver": "acme.vec:Milvus", "service_account_key_env": "sk-live-1"}

    with pytest.raises(BindingError, match="NAME of an environment variable"):
        load_binding(binding)


@pytest.mark.parametrize("written_as", ["", None])
def test_an_env_key_left_blank_is_refused_rather_than_dropped(written_as: str | None) -> None:
    """A key present and empty is a credential the author meant to supply, not one they omitted.

    `access_token_env:` with nothing after it is YAML `None`, and `access_token_env: ""` is the
    empty string; both were accepted by every validator and then made the credential *vanish* —
    `connect_options` omitted the keyword entirely, so the driver was constructed without it. What
    that reaches depends only on the driver's signature, and both outcomes are worse than a refused
    manifest: a client whose credential has a default (`api_key: str = ""`, the ordinary vendor
    shape) attaches **anonymously**, and one whose credential is required raises a bare `TypeError`
    out of the constructor — which is not in `durable/publish`'s non-retryable list, so a
    permanently broken manifest is retried by every job that touches it. That is the exact failure
    the signature check beside it was added to prevent, and the signature check cannot see this one:
    it binds the stripped name as `""` before the omission happens.
    """
    binding = _ingest()
    binding["connection"] = {"driver": "acme.vec:Milvus", "access_token_env": written_as}

    with pytest.raises(BindingError, match="left blank|NAME of an environment variable"):
        load_binding(binding)


def test_a_blank_env_key_fails_where_the_options_are_built_too() -> None:
    """The same rule at the second gate, for the manifest no CI run ever bound.

    A deployment mounts its own source directory, so the credentials are re-checked where they are
    actually resolved rather than only where a manifest loads. Asserted against `connect_options`
    because that is the function the credential used to disappear inside: it returned the address
    keys and nothing else, and every later step — the signature check included — then saw a block
    that looked complete.
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
    """The gate that replaced `extra="forbid"`: bound against the callable, with nothing connected.

    `ConnectionBinding` cannot reject an unknown key, because it does not know what any driver
    accepts. `make datasource-validate` does know — it resolves the driver and binds the block
    against its signature — so a `role:` copied over from another vendor's manifest fails in CI
    rather than as a `TypeError` in a worker on the first sync.
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
    """The gate sees the manifests this repository ships; a deployment mounts its own.

    So the signature check runs again where the driver is actually built. The error class is the
    point rather than the message: `BindingError` is a `ChemclawError`, which `durable/publish`
    lists as non-retryable *by exact class name*, while the bare `TypeError` a constructor raises is
    not on that list — a permanently broken mounted manifest would have been retried by every job
    that touched it. The model this block replaced failed such a key as a `ValidationError`, so
    keeping it non-retryable is what makes the trade like-for-like.
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
    """A C `connect` has no introspectable signature, and the check for one raised past the seam.

    `inspect.signature` answers a callable it cannot read with `ValueError`, not `TypeError` —
    `sqlite3.connect` and `duckdb.connect` both do it, and a DuckDB export is one of the databases
    `core/connect.py`'s generality claim names. `signature_mismatch` caught only `TypeError`, so
    that `ValueError` left `open_connection` unnamed: past the `error` parameter that exists so a
    broken binding fails this seam as `BindingError` and the publish seam as `SinkConnectionError`,
    and out of `make datasource-validate` as a traceback naming neither the manifest nor the driver.

    Both halves are asserted, because "it opens" alone would pass on a check that had been deleted:
    a keyword the driver *does* refuse must still be refused, by the constructor if not offline.
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

    A whole binding document arrives under one `binding=` keyword, so `signature().bind()` sees a
    keyword it accepts and nothing more. Without this, a mistyped column path in a mounted manifest
    would pass every gate and fail in a worker.
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

    The distinction matters in a provenance string, which is the line a reviewer follows back to the
    original record: an id of `0` rendering as empty would produce a citation pointing at nothing,
    and it would do it only for the rows whose ids happen to be falsy.
    """
    from chemclaw.ingest.eln.warehouse.expr import render_template

    scope = {"root": {"ID": 0, "OPERATOR": "", "PAGE": 12}}
    assert render_template("eln:${root.ID}:${root.PAGE}", scope) == "eln:0:12"
    assert render_template("eln:${root.MISSING}", scope) == "eln:"


def test_a_numeric_site_vocabulary_maps_rather_than_rejecting_every_row() -> None:
    """A transform's options are untyped, so YAML's scalar rules decide what a map key becomes.

    A site with numeric material-type codes writes `map: {1: reactant}` and gets an *integer* key.
    Comparing the row's text against that matched nothing, so every row was rejected — and the
    message said `no entry for '1'; known: [1, 2]`, showing the key apparently present. Both sides
    are compared as text now, which is what makes a numeric vocabulary work at all.
    """
    numeric = [{"value_map": {"map": {1: "reactant", 2: "solvent"}}}]

    assert apply_transforms(1, numeric) == "reactant", "an integer row value"
    assert apply_transforms("2", numeric) == "solvent", "and its string spelling"


def test_a_yaml_boolean_map_key_is_refused_with_the_fix_named() -> None:
    """`ON`/`OFF`/`YES`/`NO`/`Y`/`N` are YAML booleans, and the spelling is gone before we see it.

    Unrecoverable rather than merely wrong: `True` and `1` are also the same dict key in Python, so
    a map carrying both loses an entry before any of this code runs. Refused at load, naming the
    line to quote, instead of failing every row against a file that reads correctly.
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
    """An unbalanced bracket must not wait for the first row of the first sync to be discovered.

    The `group:` check is the same argument one step further out: a group the pattern does not have
    only raises on the first row that *matches*, which can be days later and on a subset of the
    corpus.
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
    """The one transform whose cost is a function of nothing this repository chose.

    A site writes the pattern; the subject is a free-text warehouse column. `re` has no timeout at
    any layer, so before this the only bound on `(a+)+$` against a long cell was the ingest
    activity's `start_to_close` — after which the retry ran the identical pattern over the
    identical page.

    **The assertion separates two outcomes rather than two speeds** (`tasks/lessons.md` rule 59).
    The passing case returns at the budget; the defect does not return at all — driven,
    `re.search("(a+)+$", "a" * 3000 + "b")` was still running when a 120 s alarm killed it, so its
    duration is not a number anybody has. The bound is therefore a generous multiple of the budget
    and still at least three orders of magnitude from the defect, which is what keeps it from
    reddening the gate on a loaded box.
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
    """The assertion the first version of this fix needed and did not have.

    `PatternBudgetError` descended from `ChemclawError` and its docstring claimed to escape the
    per-entry handler because it was not an `ElnMappingError`. That claim was checked against the
    `ElnMappingError` arm in `src/chemclaw/ingest/eln/warehouse/adapter.py` — the wrong handler.
    A transform runs under
    `ingest/eln/sync.py`'s `except (ChemclawError, ValidationError)`, one layer further out, which
    caught it by construction. Driven on the real `sync_entries` with a `(a+)+$` transform over ten
    entries at a 0.05 s budget: nothing escaped, all ten were booked as data refusals, and the page
    cost `rows x budget` — the exact outcome the class exists to prevent, shipped under a green
    test that asserted the wrong non-membership.

    **So this walks the handlers instead of naming one.** Every `except` clause under
    `ingest/eln/` is resolved to the classes it actually catches, and none of them may be a base of
    `PatternBudgetError`. A handler added or widened later reds this, which naming a single module
    never could.
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
    """The retry half, which leaving the error hierarchy does not cover on its own.

    `durable/publish._BAD_DATA_TYPES` is what Temporal reads as `non_retryable_error_types`, and it
    matches the outermost failure's class **name** — so a type's ancestry buys it nothing either
    way. Listed although it is no longer a `ChemclawError`, because the pattern is the same string
    in the manifest on the next attempt and the page is the same page: a retry is the stall again.
    """
    from chemclaw.durable.publish import _BAD_DATA_TYPES

    assert PatternBudgetError.__name__ in _BAD_DATA_TYPES, (
        "the pattern and the page are both the same on the next attempt, so a retry is the stall "
        "again"
    )


def test_a_repeat_count_too_large_to_expand_is_refused_before_it_is_compiled() -> None:
    """The cost the engine swap brought with it, which the first version of this change did not see.

    `re.compile` is O(1) on a bounded repeat; `regex` **expands** one. Measured: `re.compile` is
    ~0.1 ms flat for every count, while `regex.compile` is 2.9 ms at `a{10000}`, 34 ms at
    `a{100000}`, **431 ms and 290 MB** at `a{1000000}`, and did not finish in two minutes at
    `a{100000000}`. That runs at binding load, on manifest text nobody here wrote, outside
    `eln_regex_timeout_seconds` (which bounds a *match*) and outside every Temporal deadline — so a
    `datasource.yaml` could take an ingest worker down before a single row was read. Under `re`
    that pattern was free, which is why this guard arrived with the second engine and not before.

    The scan is the guard rather than a compile, because compiling is the thing being guarded.
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
    """Each shape was accepted by the first scan and each reaches `regex.compile` unbounded.

    The first scan checked one `{n,m}` at a time and treated every `[` as opening a class, so a
    `[` in a comment hid everything after it and nested bounded repeats — which `regex` expands
    multiplicatively — passed with every individual count under the limit. Timed, because a
    refusal that compiled first is not a guard.
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
    """A guard that fires on correct input reads as a working one (`tasks/lessons.md` rule 96).

    The two cases worth paying for are the two ways a `{` is not a quantifier — a backslash escape
    and a character class — because a bare scan over the whole pattern refuses `[{]{1}` and an
    escaped brace pair, both of which every engine reads as literals. The scan tracks exactly
    those two and nothing else.
    """
    binding = _ingest()
    binding["ingest"]["reaction"]["reaction_id"]["transform"] = [{"regex": {"pattern": pattern}}]

    load_binding(binding)

    assert because, "every row states why it is a pattern a binding would legitimately write"


def test_an_ordinary_pattern_still_reads_its_group_under_the_bounded_engine() -> None:
    """The engine swap is not allowed to cost the feature, which is the other half of the trade.

    `regex` is a superset of `re` in its default version, and this is the assertion that says so
    for the shapes a binding actually writes — a group, an alternation, a bounded quantifier and a
    character class — rather than leaving it to the library's own claim.
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
    """The one field `sql.py` writes into the statement text that this validator used to skip.

    `vector_statement` renders it as `f"{fn}({placeholder}, {placeholder})"`, so a value that is not
    an identifier closes the call and continues the query — and unlike a relation or a column, this
    is a field a site author fills in rather than a reviewer. A dotted name still passes, because
    the real Cortex embedder is one.
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

    A Spark `DOUBLE` holds NaN as a value (missingness arrives as `None`), so this is the source
    saying something that is not a measurement — the same case as the boolean this transform
    already refuses, and it must reach the rejection ledger with a reason rather than the row.

    It reached neither before: `float("NaN")` is a float, so a NaN travelled to
    `reaction_records.conditions` and `jsonb` refused it as `InvalidTextRepresentation` — a
    `psycopg` error that is neither `ChemclawError` nor `ValidationError`, so it aborted the sync
    pass instead of rejecting the entry.
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

    **Sized against the machine rather than written down, because the written-down size was a
    coin-flip on a shared runner.** The constant was `"a" * 6000`, measured at 165 ms against the
    shipped 0.25 s per-cell timeout — a margin of 1.5x. Every test below needs this cell to be
    *slow and completing*: slow enough that a few of them spend a page budget, and completing so
    the per-cell arm does not fire first. At 1.5x, a runner 1.6x slower than the box that measured
    it fires the per-cell arm instead, which is what CI did on 2026-09-22: the page test read
    `PatternBudgetError("did not finish within 0.25s on one 6001-character cell")` where it wanted
    the page refusal, on a commit whose diff touches nothing in `ingest/eln`.

    The cost of `a*a*a*$` is superlinear in the cell length, so one step down the ladder buys a
    large factor. `_MARGIN` is the fraction of the per-cell budget the chosen cell may cost; at
    0.3 a machine has to be more than three times slower than the one that sized the cell before
    the wrong arm can fire, and the ladder's floor keeps the page tests meaningful by refusing to
    pick a cell that is merely fast.
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
    """The bound the per-cell one does not compose into.

    **The row behind this described the accumulation as timeouts adding up, and they cannot.**
    `PatternBudgetError` is in `durable/publish._BAD_DATA_TYPES`, so a cell that *exceeds* the
    per-cell budget ends the page after one cell, non-retryably. The reachable case is a pattern
    that
    is slow and completes: measured, `a*a*a*$` over a 6,000-character cell is 165 ms with no
    refusal,
    and twenty such cells across the shipped 100-entry batch is 330 s — past
    `eln_sync_timeout_seconds`, past the heartbeat, and `map_to_ord` is synchronous CPU work no
    asyncio timer interrupts, so the retry runs the identical page.

    Driven at a small budget rather than the shipped one, because the property is the ratio and not
    the number: a test that spent 150 s proving a 150 s bound would be the slowest in the suite.
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
    """The trade the row named — refusing an honest slow pattern against bounding total work.

    It is settled by a ratio rather than argued. An honest cell measures 0.0024 ms warm against a
    0.25 s per-cell ceiling, so a whole honest page of 2,000 cells is 0.0048 s where the shipped
    page budget is 150 s — ~31,000x of headroom, and the pathological pattern is ~68,000x an honest
    one.

    **The bar was `budget / 20` and a review pointed out it cannot fail**: 7.5 s against a measured
    0.0048 s needs a 1,500x regression before it reds, so it held nothing. It is now stated against
    the *pathological* cell, which is the comparison the argument actually rests on — an honest page
    must stay cheaper than one bad cell — and which moves with the machine rather than with a
    setting.

    **The bad cell here is the *longest* this machine can run, not the one `_SLOW_CELL` sizes down
    for the arm-ordering tests.** Those shrink their cell so the page arm fires before the per-cell
    one; this test is about the ratio between a real binding's pattern and a pathological one, and
    shrinking the pathological side is what quietly inverts it — driven, a 1,000-character bad cell
    costs 4.8 ms against 5.2 ms for 2,000 honest ones, and the assertion then reads as a regression
    in the honest path when nothing about it moved. So it takes the longest rung that completes
    inside the per-cell budget, and skips below 2,500 characters rather than measuring something
    else: at 2,500 the bad cell is 27.5 ms against the honest page's 5.2 ms here, which is still a
    ratio worth asserting.
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
    """It bounds *matching* time, and the first version bounded a wall clock instead.

    `pattern_budget` is opened around the page loop — a loop whose body awaits five stores per
    entry, and in `durable/memory_jobs.read_corpus` every `fetch_new_entries` of every page of every
    source.
    A `monotonic()` deadline there bills Postgres and the source to a budget named for the regex
    engine: driven, **1.13 ms** of actual matching exhausted a 500 ms budget, and the refusal then
    told the site to simplify patterns costing microseconds.

    Worse than a wrong message. `PatternBudgetError` is non-retryable by name, so a page that used
    to reach `eln_sync_timeout_seconds` and be *retried* would fail permanently at half of it,
    with no cursor advanced. This is the arm that keeps the accumulator an accumulator.
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


def test_a_pattern_cut_short_by_the_page_is_not_reported_as_innocent() -> None:
    """The refusal must not claim no transform exceeded its ceiling when one was never let try.

    A clamped search is given exactly what the page had left, so it times out at the instant the
    page runs dry — which made the "pattern is at fault" arm unreachable. Driven over 39 clamped
    remainings against `(a+)+$`, it fired **0** times, so every catastrophic pattern was reported
    under a sentence asserting the page's aggregate cost was the whole story.
    """
    catastrophic = {"regex": {"pattern": r"(a+)+$"}}
    apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])

    # The page must be *nearly* spent when the catastrophic pattern runs, or it is handed a clamp
    # big enough to blow its own ceiling and the unclamped arm fires instead — which is the other
    # test. Sized from the measured cell cost rather than from a fixed budget and a fixed count,
    # because those two encode a machine speed: three cells at 0.45s only drains the page on a box
    # where a cell costs ~150ms.
    with pytest.raises(PatternBudgetError) as refused:
        with pattern_budget(3.5 * _SLOW_CELL_COST):
            for _ in range(3):
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])
            apply_transforms("a" * 4000 + "b", [catastrophic])

    message = str(refused.value)
    assert "not established here" in message, message
    assert "No single one exceeded" not in message, (
        "the page refusal claimed every transform stayed inside its ceiling, about a pattern that "
        f"was never given its full allowance: {message}"
    )


def test_a_spent_page_never_offers_the_engine_a_negative_timeout() -> None:
    """`regex` reads a negative `timeout` as *no* timeout, which would disable the bound entirely.

    Measured: `regex.search(text, timeout=-1.0)` completes in 0.17 s with no `TimeoutError`, where
    `timeout=0.0` raises immediately. `_cell_budget`'s `remaining <= 0.0` arm is the only thing
    keeping a negative out, and nothing pinned it — a later simplification to a bare
    `min(cell, remaining)` would pass every other test in this file with the page bound gone.
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
    """One names a pattern to rewrite; the other names a page that cost too much in aggregate.

    They are not interchangeable: the per-cell refusal tells a site its pattern is catastrophic,
    which
    is false of a binding whose every transform stayed inside the ceiling.

    **The claim this docstring made about its own arms was false, and a review caught it.** It said
    the pattern arm was "driven with both budgets open, so the clamped-cell path is the one under
    test" — but at a 30 s page budget `_cell_budget` returns the per-cell 0.25 s and reports
    `page_bound=False`, so it drove exactly the *unclamped* path it claimed to avoid. The clamped
    case has its own test now
    (`test_a_pattern_cut_short_by_the_page_is_not_reported_as_innocent`), and this one asserts the
    unclamped arm while saying so.
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
    """It read `settings.eln_regex_page_budget_seconds` first and said 150 s at a 2 s budget.

    A refusal carrying a number that is not the one that bound it is the defect class this
    repository
    keeps finding in its own prose, arriving in a message a site will act on.
    """
    with pytest.raises(PatternBudgetError, match="whole 0.4s matching budget"):
        with pattern_budget(0.4):
            for _ in range(500):
                apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])


def test_a_nested_budget_keeps_the_outer_deadline() -> None:
    """`sync_entries` opens one and calls `_replay_record_ids`, which maps entries of its own.

    A nested budget that started over would make the page bound `pages x budget` — the same
    multiplying failure the per-cell bound has, one layer out. Re-entrancy is asserted rather than
    assumed because it is the difference between a bound and a suggestion.
    """
    with pattern_budget(0.3):
        with pytest.raises(PatternBudgetError, match="spent their whole 0.3s"):
            with pattern_budget(600.0):
                for _ in range(500):
                    apply_transforms(_SLOW_CELL, [_SLOW_BUT_COMPLETING])


def test_no_budget_open_leaves_the_per_cell_bound_exactly_as_it_was() -> None:
    """Every caller that maps a single entry outside a page is unchanged.

    The page bound is additive: with no page open, `_cell_budget` returns the per-cell setting and
    the
    per-cell refusal is the only one reachable. This is the arm that says the change cannot make a
    one-off mapping stricter than it was.
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

    "Inside" matters, and the first version of this guard got it wrong: pairing any `map_to_ord`
    call
    with any loop in the same file also caught `durable/eln_sync.py` (a per-entry heartbeating
    *wrapper*, which runs under its caller's budget) and `ingest/eln/adapter.py` (which declares the
    protocol) — two modules that map one entry at a time and need no page bound at all. A guard that
    over-matches is a guard somebody silences.

    **`ingest/eln/validate.py` was in that list and should not have been.** It was written down as a
    third over-match and is a genuine page-mapping caller: it opens a `pattern_budget()` and the
    guard's live set names it. The sentence outlived the fix, so for a while this file said
    `validate.py` "needs no page bound at all" two functions away from an assertion requiring four
    callers including it.

    **Lexical-only was blind to `ingest/labels/corpus.py`**, whose page loop calls `_record`, which
    reaches `apply_transforms` two helpers down — so the reaction-corpus drain ran up to a thousand
    rows of site patterns with no page bound while this guard was green. So a loop now counts when
    it calls a module-local function that reaches a mapper (closed to a fixpoint), and
    `apply_transforms` is a mapper beside `map_to_ord`. A module that *defines* a mapper is where
    the per-entry work lives, not a page caller, and is skipped.
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
    """Derived, because the alternative was measured to fail on two drivers out of three.

    `agent/turn_ambient.turn_caps` exists because four per-turn ambients opened by hand in three
    drivers were opened correctly in one of them, and the test that "proved" it called the opener in
    its own helper. This is the same shape: a page bound is only a bound where the page opens it,
    and
    a behavioural test per caller covers the callers somebody thought of.

    It does not check *placement* — a module could still open one budget per entry, which is why
    `cli/live_data.py` carries a comment saying why it does not — but it makes a new page loop with
    no
    budget a red test rather than a gap nobody measures.
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

    A `def map_to_ord` is not a caller, and the adapters that define it must not need a budget — the
    budget belongs to whoever maps a *page* of entries. Requiring the five measured callers is what
    makes the assertion above a measurement rather than a tautology — "three", then "four", here
    was the same stale count the docstring above carried.
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
