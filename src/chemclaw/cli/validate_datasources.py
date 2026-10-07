"""Validate the data-source manifests: real halves, real signatures, real enable tokens.

`make datasource-validate`. Manifests name their halves as strings (late binding), so nothing is
checked until a half is first used, possibly hours later in a worker. Beyond pydantic's per-file
schema this checks:

1. **A half that does not resolve** (a typo in a `module:attr` reference).
2. **`config:` the half's constructor will not accept** — the callable's signature is the schema,
   bound here with the manifest's `config:` plus the `name` the registry passes.
3. **A `labels:` block that does not match the source** — a `provides` with no column, or labels on
   a source that contributes no reactions; the coverage report repeats such claims to chemists.
4. **An enabled source no manifest declares**, which would silently stop being searched.

Resolving every half is the eager import the runtime avoids; in CI that is cheap. `--construct`
(opt-in) also builds each half, which is where a binding document's values (column paths,
transforms) are validated; opt-in because construction runs a site's own code. Needs no warehouse.

Read-only; touches nothing.
"""

import argparse
import inspect
from collections.abc import Sequence

from chemclaw.core.config import settings
from chemclaw.core.connect import option_type_mismatch, resolve_driver, signature_mismatch
from chemclaw.ingest.eln.warehouse.binding import (
    BindingError,
    ConnectionBinding,
    CorpusBinding,
    load_binding,
)
from chemclaw.ingest.sources.manifest import DataSourceManifest
from chemclaw.ingest.sources.registry import (
    DataSourceError,
    discovered,
    make_data_source,
    resolve_half,
)


def _check_half(name: str, field: str, reference: str, config: dict[str, object]) -> list[str]:
    """Resolve one half and bind the manifest's config against its signature (rules 1 and 2).

    Bound against exactly what the registry passes for every half: the manifest's `config:` plus
    `name=<the manifest's name>`. Binding config alone would fail a half that correctly requires
    `name` and pass one that refuses it. `tests/test_datasource_seam.py` asserts the gate and the
    build agree.
    """
    try:
        factory = resolve_half(reference)
    except DataSourceError as exc:
        return [f"{name}: {field}: {exc}"]
    passed: dict[str, object] = {**config, "name": name}
    try:
        inspect.signature(factory).bind(**passed)
    except TypeError as exc:
        return [f"{name}: {field}: {reference} will not accept config {sorted(passed)}: {exc}"]
    # `bind` proves the keys are real; this proves each scalar value has the declared type — a YAML
    # string `"false"` is truthy. `_build_half` raises on the same condition, so gate and build
    # agree.
    if mismatch := option_type_mismatch(factory, config):
        return [f"{name}: {field}: {reference} was given {mismatch}"]
    return []


def _check_construction(name: str) -> list[str]:
    """Build every declared half, so a config the constructor rejects is found here (opt-in).

    The source name is prefixed because a half's error describes the binding, not which source
    carried it.
    """
    try:
        make_data_source(name)
    except (DataSourceError, ValueError) as exc:
        return [f"{name}: will not build: {exc}"]
    return []


def _check_connection(name: str, manifest: DataSourceManifest) -> list[str]:
    """Bind a `connection:` block against its driver's signature, with nothing connected.

    `ConnectionBinding` is `extra="allow"` because drivers share no vocabulary, so key validity is
    checked here against the callable. Every `*_env` key is bound under its stem, the keyword the
    driver is built with. The driver is resolved, not called: construction would need credentials.
    """
    raw = manifest.config.get("binding")
    if not isinstance(raw, dict):
        return []
    block = raw.get("connection")
    if not isinstance(block, dict):
        return []
    try:
        connection = ConnectionBinding.model_validate(block)
    except ValueError as exc:
        return [f"{name}: connection: {exc}"]
    try:
        driver = resolve_driver(connection.driver, error=BindingError, what="connection driver")
    except BindingError as exc:
        return [f"{name}: connection: {exc}"]
    if problem := signature_mismatch(driver, connection.options):
        return [f"{name}: connection driver {connection.driver!r} {problem}"]
    return []


def _check_labels(name: str, manifest: DataSourceManifest) -> list[str]:
    """Rule 3: a `labels:` block must describe a source that actually contributes reactions.

    A block on a source with neither an ingest half nor a `corpus:` binding is a policy nothing
    applies; a `provides` naming a group the binding maps no column for would make the coverage
    report claim the source provided it. Read from `config:` directly, so no driver is needed.
    """
    if manifest.labels is None:
        return []
    binding = _corpus_binding(manifest)
    if manifest.ingest is None and binding is None:
        return [
            f"data source {name!r} declares a `labels:` block but has no `ingest:` half and no "
            "`corpus:` binding, so it contributes no reactions and nothing would ever label them"
        ]
    if binding is None:
        return []
    overclaimed = manifest.labels.provides - binding.label_groups()
    if overclaimed:
        named = ", ".join(sorted(g.value for g in overclaimed))
        return [
            f"data source {name!r} claims to provide {named}, but its `corpus:` binding maps no "
            "column for it — the coverage report would repeat that claim to a chemist"
        ]
    return []


def _corpus_binding(manifest: DataSourceManifest) -> CorpusBinding | None:
    """The manifest's `corpus:` binding, or `None` when it declares no warehouse binding at all.

    A malformed binding is not reported here; `--construct` surfaces it with the binding
    validator's own message, so one typo is reported once.
    """
    raw = manifest.config.get("binding")
    if not isinstance(raw, dict):
        return None
    try:
        return load_binding(raw).corpus
    except ValueError:
        return None


def validate_datasources(construct: bool = False) -> list[str]:
    """Return every problem found across the discovered manifests (empty means valid)."""
    problems: list[str] = []
    try:
        manifests = discovered()
    except DataSourceError as exc:
        # A malformed manifest stops discovery entirely, so there is nothing further to check.
        return [str(exc)]

    if not manifests:
        return [
            f"no data sources discovered under {settings.data_sources_dir!r} — "
            "every retrieval and every sync would come back empty"
        ]

    for name, manifest in sorted(manifests.items()):
        resolved = []
        # Every declared half, because the registry builds every declared half.
        for field, reference in (
            ("ingest", manifest.ingest),
            ("retrieve", manifest.retrieve),
            ("commitments", manifest.commitments),
        ):
            if reference is not None:
                resolved += _check_half(name, field, reference, manifest.config)
        problems += resolved
        problems += _check_connection(name, manifest)
        problems += _check_labels(name, manifest)
        # Only when the references themselves are sound — building a source whose half does not
        # resolve would report the same typo twice, in two different vocabularies.
        if construct and not resolved:
            problems += _check_construction(name)

    # Rule 3, checked against the manifests rather than by building anything: an enabled name that
    # no folder declares.
    missing = [name for name in settings.data_source_list if name not in manifests]
    if missing:
        valid = ", ".join(sorted(manifests))
        problems.append(
            f"enabled in `data_sources` but not declared by any manifest: {missing}; "
            f"discovered: {valid}"
        )
    if not settings.data_source_list:
        problems.append("`data_sources` is empty — the agent would have no corpus to retrieve from")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Validate every manifest; print problems and exit non-zero if any (the CI gate)."""
    parser = argparse.ArgumentParser(description="Validate the data-source manifests.")
    parser.add_argument(
        "--construct",
        action="store_true",
        help="also build every declared half, validating each source's config as its own code "
        "sees it. Run this after mounting your own manifest directory; no network is used.",
    )
    options = parser.parse_args(argv)
    problems = validate_datasources(construct=options.construct)
    if problems:
        print("data source validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("data source validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
