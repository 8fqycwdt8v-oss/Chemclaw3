"""What a binding is checked for before a single row is read, and that the shipped one passes.

The engine's promise is that a site's schema is configuration. That is only worth having if a
mistake in the configuration is caught the way a mistake in code would be — at startup, naming the
line, offline. So these tests are mostly about *rejection*: the binding that would have failed on
row 40,000 must fail on load instead.

The last group closes the loop the other way, on the manifest this repository actually ships: its
binding parses, every path in it resolves against a realistic row, and it is discovered without
being enabled.
"""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from chemclaw.core.config import settings
from chemclaw.ingest.eln.warehouse.binding import BindingError, load_binding
from chemclaw.ingest.eln.warehouse.expr import TransformError, apply_transforms, resolve_path

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


def test_a_key_the_driver_will_not_take_fails_as_this_seams_error_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate sees the manifests this repository ships; a deployment mounts its own.

    So the signature check runs again where the driver is actually built. The error class is the
    point rather than the message: `BindingError` is a `ChemclawError`, which `durable/publish`
    lists as non-retryable *by exact class name*, while the bare `TypeError` a constructor raises is
    not on that list — a permanently broken mounted manifest would have been retried by every job
    that touched it. The model this block replaced failed such a key as a `ValidationError`, so
    keeping it non-retryable is what makes the trade like-for-like.
    """
    from chemclaw.core.connect import open_connection

    # `access_token_env`, not `access_token`: this fixture wrote the credential inline, which the
    # seam now refuses outright (`check_no_inline_credential`) — before it ever gets to the
    # signature check this test is about.
    monkeypatch.setenv("TEST_DATABRICKS_TOKEN", "dapi-token")
    block = {
        "driver": "chemclaw.ingest.eln.warehouse.databricks:DatabricksWarehouse",
        "server_hostname": "adb.example.net",
        "access_token_env": "TEST_DATABRICKS_TOKEN",
        "warehouse_id": "abc123",
        "role": "CHEMCLAW_READER",
    }
    with pytest.raises(BindingError, match="role"):
        open_connection(block, error=BindingError, what="warehouse connection")


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
    registry.discovered.cache_clear()
    try:
        assert validate_datasources() == [], "binding the kwargs alone cannot see the typo"
        problems = validate_datasources(construct=True)
    finally:
        registry.discovered.cache_clear()

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
