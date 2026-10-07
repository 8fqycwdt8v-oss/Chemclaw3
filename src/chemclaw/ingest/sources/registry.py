"""Discover data-source manifests, and build only the halves the calling process actually uses.

Mirrors `connectors/registry.py`: filesystem discovery (a folder with a `datasource.yaml`) plus a
config enable-token (`data_sources`). Discovery is not enablement; the repo ships every source and a
deployment enables a subset.

The property this module holds: a half is imported only where it is used. The chat process
(`active_retrieve_sources`) and the ELN sync worker (`active_ingest_source_names`) want disjoint
halves, so filtering runs on manifest data before any import. `tests/test_datasource_isolation.py`
checks this in a subprocess.
"""

import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.connect import option_type_mismatch
from chemclaw.core.errors import ChemclawError
from chemclaw.core.manifest_io import read_manifest, resolve_driver, within_root
from chemclaw.ingest.sources.base import DataSource, IngestHalf, RetrieveHalf, SourceSpec
from chemclaw.ingest.sources.manifest import DataSourceManifest

logger = logging.getLogger(__name__)

# The manifest filename inside a source folder. A constant because two modules look for it (here
# and `scripts.validate_datasources`) and a typo in either would report "no data sources found".
MANIFEST_FILENAME = "datasource.yaml"


class DataSourceError(ChemclawError):
    """A data-source folder is malformed, or an enabled source does not exist.

    A `ChemclawError` (so a `ValueError`), so one `except ValueError` at an entry point catches
    every misconfiguration. Also listed by class name in `durable.publish._BAD_DATA_TYPES`, since
    Temporal matches non-retryable types by exact name.
    """


def _source_dirs(dirs: tuple[str, ...]) -> list[Path]:
    """Every data-source folder found across `dirs`, sorted by name.

    Sorted so retrieval fan-out order is identical everywhere. Earlier dirs win on a name collision,
    so a deployment can mount a folder that overrides a shipped source.
    """
    found: dict[str, Path] = {}
    for directory in dirs:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if (path / MANIFEST_FILENAME).is_file() and within_root(root, path):
                found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


def _read_manifest(path: Path) -> DataSourceManifest:
    """Parse and validate one `datasource.yaml`, raising `DataSourceError` naming the file."""
    manifest_path = path / MANIFEST_FILENAME
    raw = read_manifest(manifest_path, DataSourceError)
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
def _discovered_in(dirs: tuple[str, ...]) -> dict[str, DataSourceManifest]:
    """Every data source found under `dirs`, by name: manifests only, nothing imported.

    Cached on the directory tuple, the only input. Only manifests are cached; halves are built fresh
    per call because they may close over per-call config.
    """
    return {path.name: _read_manifest(path) for path in _source_dirs(dirs)}


def discovered() -> dict[str, DataSourceManifest]:
    """Every data source found on disk, by name: manifests only, nothing imported.

    Settings are read outside the cache, so a changed `data_sources_dir` is a new key.
    """
    return _discovered_in(tuple(settings.data_sources_dirs))


def forget_discovered() -> None:
    """Drop the cache so the next `discovered()` re-reads data-source manifests from disk.

    Needed only when new manifests appear in an already discovered directory; repointing the
    directory is a different cache key.
    """
    _discovered_in.cache_clear()


def resolve_half(reference: str) -> Callable[..., Any]:
    """Import `module:callable` and return it: the one place this seam imports a half.

    Not cached: `sys.modules` already memoizes, and the isolation test measures which process
    imported what.
    """
    # Only callability can be checked here; the half's protocol is checked on the built object.
    # `resolve_driver` also enforces the package allow-list, since a manifest is data.
    factory: Callable[..., Any] = resolve_driver(reference, DataSourceError, "data source half")
    return factory


def _build_half(manifest: DataSourceManifest, reference: str, **extra: Any) -> Any:
    """Construct a half from its `module:callable`, the manifest `config`, and `extra` kwargs.

    Two checks: `except TypeError` catches a config key the callable does not take, and
    `option_type_mismatch` catches a value it would misread (e.g. the string `"false"` is truthy).
    `extra` is this repository's own keywords and is not checked.
    """
    factory = resolve_half(reference)
    mismatch = option_type_mismatch(factory, manifest.config)
    if mismatch:
        raise DataSourceError(
            f"data source {manifest.name!r}: {reference} was given {mismatch} Fix the manifest's "
            f"`config:` block; a value is passed through exactly as written."
        )
    try:
        return factory(**manifest.config, **extra)
    except TypeError as exc:
        # A config key the callable does not accept, re-raised naming both sides so a mistyped key
        # is distinguishable from a broken adapter.
        raise DataSourceError(
            f"data source {manifest.name!r}: {reference} rejected config "
            f"{sorted(manifest.config)}{' + ' + str(sorted(extra)) if extra else ''}: {exc}"
        ) from exc


def _build_ingest_half(manifest: DataSourceManifest) -> Any:
    """Build the ingest half and wrap it in the seam's normalisation.

    The one construction point for both production readers (the durable sync via `make_data_source`
    and the memory jobs via `active_ingest_sources`), so the normalisation (`DatedIngest`) applies
    to both and to any attached adapter. The import is lazy to keep this module's import discipline.
    Every ingest half is told its source name, which keys the rejection ledger, so two instances of
    one engine stay separate.
    """
    from chemclaw.ingest.eln.adapter import DatedIngest

    return DatedIngest(_build_half(manifest, manifest.ingest or "", name=manifest.name))


def _build_retrieve_half(manifest: DataSourceManifest) -> Any:
    """Build the retrieve half, telling it which source it is.

    A retrieve half's name is the manifest's: the document index partitions on it, its sweep deletes
    by it, citations and `retrieval_source_weights` use it. A defaulted name would let two instances
    of one engine (e.g. two mounted shares) collapse and delete each other's rows. Passed to every
    retrieve half, so a half that does not accept it fails at startup. Folder names are unique
    across mounted directories.
    """
    return _build_half(manifest, manifest.retrieve or "", name=manifest.name)


def _build_commitments_half(manifest: DataSourceManifest) -> Any:
    """Build the commitments half, telling it which source it is.

    `commitments` is keyed on `(source, external_id)`, so two portfolio exports must not share a
    name.
    """
    return _build_half(manifest, manifest.commitments or "", name=manifest.name)


def make_data_source(name: str) -> DataSource:
    """Build the fully-formed `DataSource` for `name` (every declared half), or raise.

    Used at the string-keyed Temporal boundary (`sync_eln_entries(source=name)`), which rebuilds a
    source from its name so workflow histories stay stable across deploys.
    """
    manifest = discovered().get(name)
    if manifest is None:
        valid = ", ".join(sorted(discovered())) or "(none discovered)"
        raise DataSourceError(f"unknown data source {name!r}; valid sources: {valid}")
    return SourceSpec(
        name=manifest.name,
        ingest=_build_ingest_half(manifest) if manifest.ingest else None,
        retrieve=_build_retrieve_half(manifest) if manifest.retrieve else None,
        commitments=_build_commitments_half(manifest) if manifest.commitments else None,
    )


def active_manifests() -> list[DataSourceManifest]:
    """The manifests of the enabled sources, in config order, importing nothing.

    An enabled name no folder declares is an error, not a silently empty corpus.
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
        _build_ingest_half(manifest)
        for manifest in active_manifests()
        if manifest.ingest is not None
    ]


def active_ingest_source_names() -> list[str]:
    """The names of the enabled sources declaring an ingest half (config order kept).

    From manifests alone. The ELN sync keys one cursor per name, so sources advance independently.
    """
    return [manifest.name for manifest in active_manifests() if manifest.ingest is not None]


def active_retrieve_sources() -> list[RetrieveHalf]:
    """The retrieve halves of the enabled sources; an ingest-only source is never imported.

    Runs in the chat process on the `gather_evidence` path.
    """
    return [
        _build_retrieve_half(manifest)
        for manifest in active_manifests()
        if manifest.retrieve is not None
    ]


def active_retrieve_corpora() -> dict[str, str]:
    """Each enabled retrieve source's name mapped to the corpus it reads.

    Lets fusion tell which result lists read one body of evidence. A source without `corpus:` is its
    own corpus.
    """
    return {
        manifest.name: manifest.corpus or manifest.name
        for manifest in active_manifests()
        if manifest.retrieve is not None
    }


def active_commitment_sources() -> list[str]:
    """The names of enabled sources holding committed work, importing nothing.

    Names, so the sync enumerates sources without building adapters and keys one cursor per name.
    """
    return [manifest.name for manifest in active_manifests() if manifest.commitments is not None]
