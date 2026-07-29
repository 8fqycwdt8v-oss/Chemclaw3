"""Discover data-source manifests, and build only the halves the calling process actually uses.

One place turns folders on disk into attached corpora, mirroring `connectors/registry.py` down to
the two idioms it combines: **filesystem discovery** for the sources themselves (a source is a
folder with a `datasource.yaml`, exactly as a connector is a folder with a `connector.yaml`) and a
**config enable-token** (`data_sources`) for which of the discovered sources a deployment turns on.
Discovery is not enablement — the repo ships every source, a deployment runs the subset it has
validated (D-018: registry membership is the enable switch, never a second boolean).

**The property this module exists to hold: a half is imported only where it is used.** The two
consumers want disjoint halves — `gather_evidence` fans out over `active_retrieve_sources()` in the
chat process, the durable ELN sync ingests `active_ingest_source_names()` in a worker — and neither
should pay for the other. The old `DATA_SOURCES` dict of factories could not offer that, because
naming an adapter inside a lambda still imports it at module scope: asking for the retrieve sources
(one source, `graph`, under the default config) loaded all five ELN ingest modules, `drfp`, and 836
modules in total.

Here the manifest answers "does this source have an ingest half?" as *data*, so the filter runs
before any import and a half's callable is resolved only when it is about to be used.
`tests/test_datasource_isolation.py` asserts it in a subprocess, because by the time any test runs
in the shared session `sys.modules` already holds what every other test imported.
"""

import importlib
import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.sources.base import DataSource, IngestHalf, RetrieveHalf, SourceSpec
from chemclaw.ingest.sources.manifest import DataSourceManifest

logger = logging.getLogger(__name__)

# The manifest filename inside a source folder. A constant because two modules look for it (here
# and `scripts.validate_datasources`) and a typo in either would report "no data sources found".
MANIFEST_FILENAME = "datasource.yaml"


class DataSourceError(ValueError):
    """A data-source folder is malformed, or an enabled source does not exist.

    A `ValueError` subclass for the same reason `ConnectorError` is one: this is a configuration
    error surfaced at startup, so a single `except ValueError` at an entry point catches every
    "this deployment is misconfigured" failure regardless of which seam raised it.
    """


def _source_dirs() -> list[Path]:
    """Every data-source folder found across the configured dirs, sorted by name.

    Sorted rather than filesystem order so retrieval fan-out order is identical on every machine.
    Earlier dirs win on a name collision, so a deployment can mount a folder that overrides a
    repo-shipped source — the mechanism that replaced the typed `data_source_specs` list: a second
    JSON-ELN drop with its own `export_dir` is a manifest in a mounted dir, not a new config
    variant plus a new branch in core.
    """
    found: dict[str, Path] = {}
    for directory in settings.data_sources_dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / MANIFEST_FILENAME).is_file():
                found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


def _read_manifest(path: Path) -> DataSourceManifest:
    """Parse and validate one `datasource.yaml`, raising `DataSourceError` naming the file."""
    manifest_path = path / MANIFEST_FILENAME
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DataSourceError(f"{manifest_path}: unreadable data source manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise DataSourceError(
            f"{manifest_path}: manifest must be a mapping, got {type(raw).__name__}"
        )
    try:
        manifest = DataSourceManifest.model_validate(raw)
    except ValidationError as exc:
        raise DataSourceError(f"{manifest_path}: invalid data source manifest:\n{exc}") from exc
    if manifest.name != path.name:
        raise DataSourceError(
            f"{manifest_path}: manifest name {manifest.name!r} does not match its folder "
            f"{path.name!r}; the folder name is what `CHEMCLAW_DATA_SOURCES` enables"
        )
    return manifest


@cache
def discovered() -> dict[str, DataSourceManifest]:
    """Every data source found on disk, by name — manifests only, nothing imported.

    Cached because discovery is filesystem I/O over a fixed layout and both consumers call it per
    operation. The cache holds *manifests*, never built halves: a built half may close over
    per-call config (a monkeypatched `knowledge_dir` in tests, a rotated export dir), so sources
    are constructed fresh on every call exactly as the old factories did.
    """
    return {path.name: _read_manifest(path) for path in _source_dirs()}


def resolve_half(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it — the one place this seam imports a half.

    Deliberately *not* cached: `importlib.import_module` already memoizes on `sys.modules`, and a
    second cache here would only obscure which process resolved what, which is exactly the
    property `tests/test_datasource_isolation.py` measures.
    """
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise DataSourceError(
            f"cannot import {module_name!r} for data source half {reference!r}: {exc}"
        ) from exc
    resolved = getattr(module, attribute, None)
    if resolved is None:
        raise DataSourceError(
            f"{module_name!r} has no attribute {attribute!r} (from {reference!r})"
        )
    if not callable(resolved):
        raise DataSourceError(
            f"{reference!r} resolved to {type(resolved).__name__}, which is not callable"
        )
    # `getattr` on a module is `Any`, and the `callable()` guard above is the only check that can be
    # made — what a half must satisfy is `IngestHalf`/`RetrieveHalf`, which are runtime-checkable
    # protocols on the *built* object, not on the factory. `_build_half`'s caller gets that check
    # for free the moment it assigns the result into `SourceSpec`.
    factory: Callable[..., Any] = resolved
    return factory


def _build_half(manifest: DataSourceManifest, reference: str) -> Any:
    """Construct one half from its `module:callable` and the manifest's `config` kwargs."""
    factory = resolve_half(reference)
    try:
        return factory(**manifest.config)
    except TypeError as exc:
        # A config key the callable does not accept. Re-raised as a configuration error naming both
        # sides, because a bare TypeError from inside a constructor gives the operator no way to
        # tell a mistyped manifest key from a broken adapter.
        raise DataSourceError(
            f"data source {manifest.name!r}: {reference} rejected config "
            f"{sorted(manifest.config)}: {exc}"
        ) from exc


def make_data_source(name: str) -> DataSource:
    """Build the fully-formed `DataSource` for `name` (every declared half), or raise.

    Used by the string-keyed Temporal boundary (`sync_eln_entries(source=name)`), which rebuilds a
    source from just its name so in-flight workflow histories stay byte-identical across a deploy.
    This is the one entry point that resolves *all* declared halves, because its caller asked for
    the whole source by name rather than for a particular capability.
    """
    manifest = discovered().get(name)
    if manifest is None:
        valid = ", ".join(sorted(discovered())) or "(none discovered)"
        raise DataSourceError(f"unknown data source {name!r}; valid sources: {valid}")
    return SourceSpec(
        name=manifest.name,
        ingest=_build_half(manifest, manifest.ingest) if manifest.ingest else None,
        retrieve=_build_half(manifest, manifest.retrieve) if manifest.retrieve else None,
    )


def active_manifests() -> list[DataSourceManifest]:
    """The manifests of the enabled sources, in config order, importing nothing.

    An enabled name that no folder declares is a loud error rather than a silently missing corpus —
    the failure this seam is most exposed to, since a retrieval that quietly returns nothing looks
    exactly like a corpus with no matches.
    """
    manifests = discovered()
    active = []
    for name in settings.data_source_list:
        manifest = manifests.get(name)
        if manifest is None:
            valid = ", ".join(sorted(manifests)) or "(none discovered)"
            raise DataSourceError(
                f"data source {name!r} is enabled in `data_sources` but no manifest declares it; "
                f"discovered: {valid}"
            )
        active.append(manifest)
    return active


def active_ingest_sources() -> list[IngestHalf]:
    """The ingest halves of the enabled sources — a retrieve-only source is never imported."""
    return [
        _build_half(manifest, manifest.ingest)
        for manifest in active_manifests()
        if manifest.ingest is not None
    ]


def active_ingest_source_names() -> list[str]:
    """The names of the enabled sources declaring an ingest half (config order kept).

    Answered from manifests alone, so the durable ELN sync enumerates what it must sync without
    constructing a single adapter. It keys one high-water cursor per name, so two ingest sources
    advance independently and neither's furthest cursor can skip the other's lagging entries.
    """
    return [manifest.name for manifest in active_manifests() if manifest.ingest is not None]


def active_retrieve_sources() -> list[RetrieveHalf]:
    """The retrieve halves of the enabled sources — an ingest-only source is never imported.

    This is the call that made the old registry's shape a production concern rather than a tidiness
    one: it runs in the chat process on the `gather_evidence` path, and under the default config it
    wants exactly one source.
    """
    return [
        _build_half(manifest, manifest.retrieve)
        for manifest in active_manifests()
        if manifest.retrieve is not None
    ]
