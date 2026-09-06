"""The one reader every manifest seam goes through, driven with the corpus that broke all six.

Every assertion here was watched failing against the source as it stood before
`chemclaw.core.manifest_io` existed, using these same fixtures. They are grouped in one file
rather than spread across the five seam suites for the reason the module itself is one module: the
defect was never *a* loader's, it was the same defect written six times, and a test per seam would
be the same duplication one layer up. Where a fix is genuinely one seam's — the sink's unwrapped
`ValidationError`, the channel's missing protocol check — the test drives that seam directly.

The fixtures are built here rather than checked in because two of them are hostile: a 375-byte
alias bomb and a 2000-deep document. Neither may sit on a discovery path.
"""

from pathlib import Path

import pytest

from chemclaw.connectors.jobs import ConnectorJobError, resolve_params_model
from chemclaw.connectors.registry import ConnectorError
from chemclaw.connectors.registry import discovered as connectors_discovered
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
from chemclaw.publish.registry import ResultSinkError
from chemclaw.publish.registry import discovered as sinks_discovered
from chemclaw.templates.registry import TemplateError
from chemclaw.templates.registry import discovered as templates_discovered

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
    """375 bytes on disk used to cost 687 MB of RSS and 3.2 s before pydantic refused it.

    The measurement that matters is that the refusal names the *expansion*: `yaml.safe_load` shares
    aliased nodes, so a bound on the parse would have counted 93 and passed. At one more level of
    nesting the same file drove the process past 9 GB, which is a whole pod fleet failing to start
    over one mounted ConfigMap and looking like a resource problem.
    """
    text = _alias_bomb(depth=7, fan=9)
    assert len(text) < 400, "the fixture's whole point is that it is tiny on disk"
    root = _bundle(tmp_path, "fix", text)
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    connectors_discovered.cache_clear()
    with pytest.raises(ConnectorError, match=f"over the {MAX_MANIFEST_NODES}-node ceiling"):
        connectors_discovered()
    connectors_discovered.cache_clear()


def test_deep_nesting_is_the_seams_own_error_and_not_a_bare_recursion_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RecursionError` is not a `YAMLError`, so it used to escape every loader untranslated.

    That mattered twice over: the failure named no file, and it was not a `ChemclawError`, so
    neither the `except ValueError` startup handlers nor Temporal's name-matched non-retryable set
    saw a manifest problem.
    """
    root = _bundle(tmp_path, "deep", "a: " + "[" * 2000 + "]" * 2000 + "\n")
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    connectors_discovered.cache_clear()
    with pytest.raises(ConnectorError):
        connectors_discovered()
    connectors_discovered.cache_clear()


def test_a_duplicated_classification_key_is_refused_rather_than_silently_last_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate-key rule earns its place on exactly this key.

    PyYAML keeps the last of a repeated key with no diagnostic and `extra="forbid"` offers nothing,
    because the key is not extra. Measured against the unfixed loader, this manifest loaded with
    `state_changing == []` and `read_only == ['a', 'b']` — two declared write tools reclassified as
    reads, which is the plan gate's input (D-167) failing *open* from a copy-paste.
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
    connectors_discovered.cache_clear()
    with pytest.raises(ConnectorError, match="appears more than once"):
        connectors_discovered()
    connectors_discovered.cache_clear()


def test_an_oversized_prose_field_cannot_become_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job's `summary` is the model-facing tool docstring, and it had no maximum.

    Measured against the unfixed source, a 5.4 MB `connector.yaml` built a 5,200,664-character tool
    description — around 1.3 million tokens on every model call — from one file that
    `connector-validate` reported nothing about. This fixture stays small on disk so the *field*
    bound is what refuses it rather than the file-size ceiling.
    """
    root = _bundle(
        tmp_path,
        "big",
        "name: big\ndescription: d\njobs:\n  - name: b\n    workflow: W\n    summary: "
        + "s" * (MAX_MANIFEST_TEXT_CHARS + 1)
        + "\n",
    )
    monkeypatch.setattr(settings, "connectors_dir", str(root))
    connectors_discovered.cache_clear()
    with pytest.raises(ConnectorError, match="summary"):
        connectors_discovered()
    connectors_discovered.cache_clear()


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
    templates_discovered.cache_clear()
    with pytest.raises(TemplateError, match="summary"):
        templates_discovered()
    templates_discovered.cache_clear()


@pytest.mark.parametrize("folder", ["UPPER", "with space", "dot.dot", "-leading"])
def test_a_data_source_name_is_held_to_the_shape_its_three_siblings_require(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    """`DataSourceManifest.name` was the one manifest name with no pattern.

    Defence in depth, stated as such: the review could not turn a hostile name into a traversal or
    a metric-label injection. But the name is the document-index partition key, the sweep's delete
    predicate, the citation label and a `retrieval_source_weights` key, and every sibling manifest
    already constrains it. All four of these folders loaded before, and `datasource-validate`
    exited 0 on them.
    """
    directory = tmp_path / folder
    directory.mkdir()
    (directory / "datasource.yaml").write_text(
        f"name: {folder}\ndescription: d\n"
        "retrieve: chemclaw.ingest.documents.retriever:ShareDocumentRetriever\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "data_sources_dir", str(tmp_path))
    sources_discovered.cache_clear()
    with pytest.raises(DataSourceError):
        sources_discovered()
    sources_discovered.cache_clear()


def test_a_sink_manifest_failure_is_a_result_sink_error_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`publish/registry._load` was the one loader that did not wrap `model_validate`.

    Not for the reason the finding gave — `"ValidationError"` *is* in
    `durable.publish._BAD_DATA_TYPES`, so the retry budget was never at risk, and that is worth
    recording so the next reader does not re-argue it. The real cost is the one
    `deliver/registry._load`'s own docstring describes from wave 4: `sink-validate` catches
    `ResultSinkError`, so a bad manifest surfaced as a raw traceback naming no path where its
    sibling reported a line.
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
    """`deliver.build` returned whatever the factory returned; the sink seam already checked.

    This is the surface whose whole job is to leave the building, so the failure has to land while
    a manifest is being read rather than while the message that was supposed to reach a person is
    being dropped.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "builtins")
    manifest = DeliveryChannelManifest(name="x", description="d", driver="builtins:dict")
    with pytest.raises(DeliveryChannelError, match="did not build a DeliveryDriver"):
        build_channel(manifest)


def test_a_manifest_may_not_name_a_package_no_operator_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The doctrinal fix: `a manifest is data` applied to the fields that execute.

    Measured against the unfixed source, a module named by `params_model` ran its top-level code
    inside `job_tools()` — the per-turn agent-build path — with `connector-validate`,
    `sink-validate` and `datasource-validate` all exiting 0. The refusal happens *before* the
    import, which is the whole point: an import is the execution.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "")
    with pytest.raises(ConnectorJobError, match="not on CHEMCLAW_MANIFEST_DRIVER_PACKAGES"):
        resolve_params_model("json:JSONDecoder")


def test_an_operator_can_still_allow_a_third_party_driver_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The D-118/D-120 property survives: a bundle is still zero core edits.

    A driver living in this tree needs nothing at all, and one outside it is one env var set by the
    operator who mounted the directory — the same "an operator turns it on" shape
    `connector_stdio_enabled` has. Without this arm the gate would be a deletion wearing a
    setting's clothes.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "pydantic")
    assert "pydantic" in settings.manifest_driver_package_list
    assert "chemclaw" in settings.manifest_driver_package_list
    assert resolve_params_model("pydantic:BaseModel") is not None


def test_a_bundle_symlinked_out_of_its_discovery_root_is_not_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery is enablement, and `iterdir()`/`is_file()` both follow symlinks.

    So a `linked -> /anywhere/else` entry inside a mounted ConfigMap loaded that other directory's
    manifest as a bundle — a way out of the root that a reviewer of the ConfigMap cannot see. The
    rule is resolution rather than "refuse a symlink", because a ConfigMap volume presents its own
    keys as symlinks into `..data` and those land inside the mount.
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
    connectors_discovered.cache_clear()
    assert connectors_discovered() == {}
    connectors_discovered.cache_clear()


def test_the_reader_still_refuses_python_tags_and_still_takes_a_mapping(tmp_path: Path) -> None:
    """The two properties the review found sound, asserted so this module cannot weaken them.

    `_ManifestLoader` subclasses `SafeLoader`, and a subclass is exactly how `!!python/` quietly
    comes back. The mapping rule is here because folding it in is what let five hand-written copies
    of it be deleted — and the sixth, which did not exist at all, be added.
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
    """The bounds are headroom, not a diet: nothing this tree ships is anywhere near one.

    Asserted rather than assumed because all four ceilings were derived from measurements of these
    files, and a ceiling derived from a corpus that then fails it is the failure mode CLAUDE.md
    keeps naming — a number in prose that is a claim about a commit.
    """
    connectors_discovered.cache_clear()
    sources_discovered.cache_clear()
    sinks_discovered.cache_clear()
    templates_discovered.cache_clear()
    assert connectors_discovered()
    assert sources_discovered()
    assert sinks_discovered()
    assert templates_discovered()
