"""The one bounded reader every manifest seam goes through (`chemclaw.core.manifest_io`).

Tests live in one file because the reader is shared; where a fix belongs to one seam (the sink's
`ValidationError` wrapping, the channel's protocol check) the test drives that seam directly.
Fixtures are built here rather than checked in because two are hostile (an alias bomb and a
deeply nested document) and must not sit on a discovery path.
"""

from pathlib import Path

import pytest

from chemclaw.connectors.jobs import ConnectorJobError, resolve_params_model
from chemclaw.connectors.registry import ConnectorError
from chemclaw.connectors.registry import discovered as connectors_discovered
from chemclaw.connectors.registry import forget_discovered as forget_connectors_discovered
from chemclaw.core.config import settings
from chemclaw.core.manifest_io import (
    MAX_MANIFEST_NODES,
    MAX_MANIFEST_TEXT_CHARS,
    read_manifest,
)
from chemclaw.deliver.manifest import DeliveryChannelManifest
from chemclaw.deliver.registry import DeliveryChannelError
from chemclaw.deliver.registry import build as build_channel
from chemclaw.ingest.sources.registry import DataSourceError
from chemclaw.ingest.sources.registry import discovered as sources_discovered
from chemclaw.ingest.sources.registry import forget_discovered as forget_sources_discovered
from chemclaw.publish.registry import ResultSinkError
from chemclaw.publish.registry import discovered as sinks_discovered
from chemclaw.templates.registry import TemplateError
from chemclaw.templates.registry import discovered as templates_discovered
from chemclaw.templates.registry import forget_discovered as forget_templates_discovered

_HTTP_ENDPOINT = "endpoint:\n  transport: http\n  url: http://127.0.0.1:9/mcp\n  tools: [a, b]\n"


def _bundle(root: Path, name: str, body: str) -> Path:
    """Write one `connector.yaml` bundle under `root` and hand back the discovery root."""
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "connector.yaml").write_text(body, encoding="utf-8")
    return root


def _alias_bomb(depth: int, fan: int) -> str:
    """The classic billion-laughs, as a `connector.yaml`.

    375 bytes at depth 7 / fan 9, and 43,046,721 nodes once expanded. Written as a function so the
    numbers in the assertion below are the numbers that produced the file.
    """
    lines = ["name: fix", "description: d", "a0: &a0 [" + ",".join(["x"] * fan) + "]"]
    for level in range(1, depth + 1):
        lines.append(f"a{level}: &a{level} [" + ",".join([f"*a{level - 1}"] * fan) + "]")
    return "\n".join(lines) + "\n"


def test_an_alias_bomb_is_refused_by_arithmetic_rather_than_by_running_out_of_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alias bomb is refused by counting its expansion, before memory is spent.

    `yaml.safe_load` shares aliased nodes, so a bound on parsed nodes would pass; the refusal must
    name the expansion. Otherwise one mounted ConfigMap can stop a fleet starting for lack of
    memory.
    """
    text = _alias_bomb(depth=7, fan=9)
    assert len(text) < 400, "the fixture's whole point is that it is tiny on disk"
    root = _bundle(tmp_path, "fix", text)
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    forget_connectors_discovered()
    with pytest.raises(ConnectorError, match=f"over the {MAX_MANIFEST_NODES}-node ceiling"):
        connectors_discovered()
    forget_connectors_discovered()


def test_deep_nesting_is_the_seams_own_error_and_not_a_bare_recursion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deep nesting raises the seam's own error naming the file, not a bare `RecursionError`.

    `RecursionError` is not a `YAMLError` or `ChemclawError`, so neither startup handlers nor
    Temporal's non-retryable set would recognise it as a manifest problem.
    """
    root = _bundle(tmp_path, "deep", "a: " + "[" * 2000 + "]" * 2000 + "\n")
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    forget_connectors_discovered()
    with pytest.raises(ConnectorError):
        connectors_discovered()
    forget_connectors_discovered()


def test_a_duplicated_classification_key_is_refused_rather_than_silently_last_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicated classification key is refused rather than silently last-wins.

    PyYAML keeps the last repeated key, so a copy-paste could reclassify write tools as reads,
    making the plan gate fail open.
    """
    root = _bundle(
        tmp_path,
        "dup",
        "name: dup\ndescription: d\n"
        + _HTTP_ENDPOINT
        + "  state_changing: [a, b]\n  read_only: []\n"
        + "  state_changing: []\n  read_only: [a, b]\n",
    )
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    forget_connectors_discovered()
    with pytest.raises(ConnectorError, match="appears more than once"):
        connectors_discovered()
    forget_connectors_discovered()


def test_an_oversized_prose_field_cannot_become_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An oversized prose field cannot become the prompt.

    A job's `summary` is the model-facing tool docstring, so it is bounded. The fixture is small on
    disk so the field bound, not the file-size ceiling, is what refuses it.
    """
    root = _bundle(
        tmp_path,
        "big",
        "name: big\ndescription: d\njobs:\n  - name: b\n    workflow: W\n    summary: "
        + "s" * (MAX_MANIFEST_TEXT_CHARS + 1)
        + "\n",
    )
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    forget_connectors_discovered()
    with pytest.raises(ConnectorError, match="summary"):
        connectors_discovered()
    forget_connectors_discovered()


def test_a_templates_summary_is_bounded_by_the_same_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hole, in the other file that generates a tool docstring."""
    (tmp_path / "t.yaml").write_text(
        "summary: "
        + "s" * (MAX_MANIFEST_TEXT_CHARS + 1)
        + "\nsteps:\n  - id: s1\n    kind: tool\n    purpose: p\n"
        "    tool: enumerate_bond_cleavages\n    arguments: {smiles: C, mode: homolytic}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "templates_dir", str(tmp_path))
    forget_templates_discovered()
    with pytest.raises(TemplateError, match="summary"):
        templates_discovered()
    forget_templates_discovered()


@pytest.mark.parametrize("folder", ["UPPER", "with space", "dot.dot", "-leading"])
def test_a_data_source_name_is_held_to_the_shape_its_three_siblings_require(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    """A data source name is held to the same pattern as its sibling manifests.

    Defence in depth: the name is the document-index partition key, the sweep's delete predicate,
    the citation label and a `retrieval_source_weights` key.
    """
    directory = tmp_path / folder
    directory.mkdir()
    (directory / "datasource.yaml").write_text(
        f"name: {folder}\ndescription: d\n"
        "retrieve: chemclaw.ingest.documents.retriever:ShareDocumentRetriever\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    forget_sources_discovered()
    with pytest.raises(DataSourceError):
        sources_discovered()
    forget_sources_discovered()


def test_a_sink_manifest_failure_is_a_result_sink_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sink manifest failure is a `ResultSinkError` naming the file.

    `sink-validate` catches `ResultSinkError`, so an unwrapped `ValidationError` would surface as a
    traceback naming no path.
    """
    directory = tmp_path / "Upper"
    directory.mkdir()
    (directory / "sink.yaml").write_text(
        "name: Upper\ndriver: chemclaw.publish.drivers.sql:SqlResultSink\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "result_sinks_dir", str(tmp_path))
    sinks_discovered.cache_clear()
    with pytest.raises(ResultSinkError, match="Upper"):
        sinks_discovered()
    sinks_discovered.cache_clear()


def test_a_channel_factory_that_builds_the_wrong_thing_fails_at_build_not_at_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel factory that builds the wrong type fails at build time, not at send.

    Delivery leaves the building, so the failure must land while the manifest is read, not while a
    message is being dropped.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "builtins")
    manifest = DeliveryChannelManifest(name="x", description="d", driver="builtins:dict")
    with pytest.raises(DeliveryChannelError, match="did not build a DeliveryDriver"):
        build_channel(manifest)


def test_a_manifest_may_not_name_a_package_no_operator_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manifest may not name a package no operator allowed.

    A module named by `params_model` would run its top-level code on the per-turn agent-build path;
    the refusal happens before the import, because the import is the execution.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "")
    with pytest.raises(ConnectorJobError, match="not on CHEMCLAW_MANIFEST_DRIVER_PACKAGES"):
        resolve_params_model("json:JSONDecoder")


def test_an_operator_can_still_allow_a_third_party_driver_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator can still allow a third-party driver package with one env var.

    A bundle stays zero core edits: a driver in this tree needs nothing, and one outside it needs an
    operator setting, like `connector_stdio_enabled`.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "pydantic")
    assert "pydantic" in settings.manifest_driver_package_list
    assert "chemclaw" in settings.manifest_driver_package_list
    assert resolve_params_model("pydantic:BaseModel") is not None


def test_a_bundle_symlinked_out_of_its_discovery_root_is_not_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle symlinked out of its discovery root is not discovered.

    `iterdir()`/`is_file()` follow symlinks, so a link could load a manifest from outside the
    reviewed mount. The rule is resolution, not "no symlinks", because a ConfigMap presents its keys
    as symlinks into `..data` inside the mount.
    """
    outside = tmp_path / "outside" / "linked"
    outside.mkdir(parents=True)
    (outside / "connector.yaml").write_text(
        "name: linked\ndescription: d\n" + _HTTP_ENDPOINT + "  read_only: [a, b]\n",
        encoding="utf-8",
    )
    root = tmp_path / "connectors"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    forget_connectors_discovered()
    assert connectors_discovered() == {}
    forget_connectors_discovered()


def test_the_reader_still_refuses_python_tags_and_still_takes_a_mapping(tmp_path: Path) -> None:
    """The reader still refuses Python tags and still requires a mapping.

    `_ManifestLoader` subclasses `SafeLoader`, and a subclass is how `!!python/` could come back.
    """
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("a: !!python/object/apply:os.system ['true']\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="malformed YAML"):
        read_manifest(unsafe, ConnectorError)

    sequence = tmp_path / "seq.yaml"
    sequence.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="must contain a YAML mapping"):
        read_manifest(sequence, ConnectorError)


def test_every_shipped_manifest_still_loads_through_the_bounded_reader() -> None:
    """Every shipped manifest still loads through the bounded reader.

    The ceilings are headroom over this tree's manifests, so they are checked against the corpus.
    """
    forget_connectors_discovered()
    forget_sources_discovered()
    sinks_discovered.cache_clear()
    forget_templates_discovered()
    assert connectors_discovered()
    assert sources_discovered()
    assert sinks_discovered()
    assert templates_discovered()
