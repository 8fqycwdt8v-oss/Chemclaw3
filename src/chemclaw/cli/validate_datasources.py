"""Validate the data-source manifests: real halves, real signatures, real enable tokens.

`make datasource-validate`, the gate this seam did not have. It was the only registry in the repo
with no validator, which was defensible while a source was a lambda in a Python file that `mypy`
already checked — and stopped being defensible the moment a source became a YAML manifest naming
its half as a string (D-120). Late binding buys the isolation this seam exists for; the bill is
that nothing is checked until something asks for that half, in whichever process asks first.

This gate pays the bill up front. Pydantic already rejects a malformed manifest at load; the three
things a per-file schema cannot see are:

1. **A half that does not resolve.** `ingest: chemclaw.ingest.eln.json_adapter:JsonExprotAdapter`
is a perfectly
   valid string. It fails at the first sync, in a worker, hours after the deploy that introduced
   it — the worst place and time to learn about a typo.
2. **`config:` the half's constructor will not accept.** Free-form config is the deliberate trade
   (the callable's signature *is* the schema, so there is no second model to keep in step with the
   adapter) and this is what makes it safe: the kwargs are bound against the real signature here.
3. **A `labels:` block that does not match the source.** A `provides` naming a group the source
   has no column for is not inert: it is read by the coverage report, so the manifest's claim ends
   up in a sentence a chemist reads. And a `labels:` block on a source that contributes no
   reactions at all is a policy nothing will ever apply.
4. **An enabled source no manifest declares.** `data_sources` naming a missing source would
   otherwise be a corpus that silently stops being searched — indistinguishable, from the chemist's
   side, from a corpus with no matches.

Resolving every half is exactly the eager import the runtime seam avoids, which is the point: this
runs in CI, in its own process, where importing everything is free and finding out early is not.

**`--construct` goes one step further, opt-in.** Binding kwargs checks their *names*; it cannot see
whether the values make sense. That gap was academic while every `config:` was a directory path, and
stopped being academic when a source's config became a whole binding document
(`chemclaw.ingest.eln.warehouse`): a mistyped column path or an unknown transform binds perfectly
and fails when the half is built. `--construct` builds each half, which is where such a config is
validated. Opt-in rather than default because construction is a half's own code — the shipped ones
open nothing, but this gate cannot promise that of a source a deployment mounted. It is the check to
run after mounting your own manifest directory, and it needs no warehouse to be reachable.

Read-only; touches nothing.
"""

import argparse
import inspect
from collections.abc import Sequence

from chemclaw.core.config import settings
from chemclaw.core.connect import resolve_driver, signature_mismatch
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

    Bound against **exactly what the registry passes**, which is not the same for the two halves: a
    retrieve half additionally receives `name=<the manifest's name>` (see
    `chemclaw.ingest.sources.registry._build_retrieve_half` for why every retrieve half is told
    which source it is). Binding the config alone would report a retriever that correctly requires
    `name` as broken, and would pass one that refuses it — in both directions the opposite of the
    truth, which is the only thing worse than not checking.
    """
    try:
        factory = resolve_half(reference)
    except DataSourceError as exc:
        return [f"{name}: {field}: {exc}"]
    passed: dict[str, object] = dict(config)
    if field == "retrieve":
        passed["name"] = name
    try:
        inspect.signature(factory).bind(**passed)
    except TypeError as exc:
        return [f"{name}: {field}: {reference} will not accept config {sorted(passed)}: {exc}"]
    return []


def _check_construction(name: str) -> list[str]:
    """Build every declared half, so a config the constructor rejects is found here (opt-in).

    Reported with the source name in front, because the error a half raises describes the *binding*
    ("unknown transform 'exek'") and says nothing about which source carried it.
    """
    try:
        make_data_source(name)
    except (DataSourceError, ValueError) as exc:
        return [f"{name}: will not build: {exc}"]
    return []


def _check_connection(name: str, manifest: DataSourceManifest) -> list[str]:
    """Bind a `connection:` block against its driver's signature, with nothing connected.

    This is the offline half of "the driver's signature is the schema"
    (`D-2026-08-26-the-driver-s-signature-is-the-schema`), and it is what replaced a model that
    enumerated one vendor's connection fields. `ConnectionBinding` is `extra="allow"` precisely
    because a Postgres, a lakehouse and a vector database share no vocabulary — so the check that a
    key is real cannot live in a model, and lives here instead, against the callable that owns the
    words. Every `*_env` key is bound under its stem, because that is the keyword the driver will
    actually be built with once the variable is read.

    The driver is *resolved*, not called: resolution imports the vendor client, which is the same
    eager import this gate already makes for every half, and construction would need credentials.
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

    Two checks, and both are about a claim reaching a chemist rather than about a crash. A block on
    a source with neither an ingest half nor a `corpus:` binding is a policy nothing will ever
    apply — it looks like labelling is configured when nothing is being labelled. And a `provides`
    naming a group the binding maps no column for is a lie the coverage report repeats: it would
    say "this source provided the name" for rows where nothing did.

    The binding is read straight out of `config:` rather than by constructing the half, so this
    runs in the default gate and needs no driver.
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

    A malformed binding is not reported here: `--construct` builds the half and surfaces it with
    the binding validator's own message, which names the offending path. Two reports of one typo in
    two vocabularies is what the `resolved` guard above exists to avoid.
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
        for field, reference in (("ingest", manifest.ingest), ("retrieve", manifest.retrieve)):
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
