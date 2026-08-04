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

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "chemclaw"
    / "ingest"
    / "sources"
    / "eln-snowflake"
    / "datasource.yaml"
)


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
    with pytest.raises(BindingError, match="must declare an 'ingest' or a 'vector' section"):
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


def test_the_shipped_manifest_binding_is_valid() -> None:
    """The example this repository ships parses under the same rules a real one will."""
    manifest = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))
    binding = load_binding(manifest["config"]["binding"])

    assert binding.ingest is not None and binding.vector is not None
    assert manifest["name"] == _MANIFEST.parent.name
    assert manifest["ingest"].endswith(":WarehouseElnAdapter")
    assert manifest["retrieve"].endswith(":WarehouseVectorRetriever")


def test_every_path_in_the_shipped_manifest_resolves_against_a_realistic_row() -> None:
    """The worked example is worked — not a plausible-looking file nobody ever ran.

    A shipped example whose paths do not resolve is worse than none: it is the thing a binding
    author copies, and it would teach a shape that silently yields nothing.
    """
    manifest = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))
    binding = load_binding(manifest["config"]["binding"])
    assert binding.ingest is not None

    row = {
        "REACTION_ID": "RX-1",
        "PROJECT_CODE": "PRJ-7",
        "OBJECTIVE": "drop to 60 C",
        "PROTOCOL_TEXT": "charge, reflux, work up",
        "EXPERIMENT_DATE": "2026-05-01",
        "TEMP_C": "60",
        "DURATION_MIN": "90",
        "YIELD_PCT": "82.5",
        "ASSAY_PCT": "99.1",
        "RESULT_FLAG": "OK",
        "FAILURE_NOTE": "",
        "OPERATOR": "a.chemist",
    }
    payload: dict[str, Any] = {"root": row, "charges": [], "analytics": []}

    unresolved = [
        name
        for name, field in binding.ingest.reaction.items()
        if resolve_path(field.path, payload) is None and name != "failure_reason"
    ]
    assert unresolved == [], f"shipped example paths that resolve to nothing: {unresolved}"


def test_the_snowflake_source_is_discovered_but_not_enabled() -> None:
    """Shipping a source is not attaching it: a deployment enables what it has validated (D-018)."""
    from chemclaw.ingest.sources.registry import discovered

    assert "eln-snowflake" in discovered()
    assert "eln-snowflake" not in settings.data_source_list


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
