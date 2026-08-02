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
3. **An enabled source no manifest declares.** `data_sources` naming a missing source would
   otherwise be a corpus that silently stops being searched — indistinguishable, from the chemist's
   side, from a corpus with no matches.

Resolving every half is exactly the eager import the runtime seam avoids, which is the point: this
runs in CI, in its own process, where importing everything is free and finding out early is not.

Read-only; touches nothing.
"""

import inspect

from chemclaw.core.config import settings
from chemclaw.ingest.sources.registry import DataSourceError, discovered, resolve_half


def _check_half(name: str, field: str, reference: str, config: dict[str, object]) -> list[str]:
    """Resolve one half and bind the manifest's config against its signature (rules 1 and 2)."""
    try:
        factory = resolve_half(reference)
    except DataSourceError as exc:
        return [f"{name}: {field}: {exc}"]
    try:
        inspect.signature(factory).bind(**config)
    except TypeError as exc:
        return [f"{name}: {field}: {reference} will not accept config {sorted(config)}: {exc}"]
    return []


def validate_datasources() -> list[str]:
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
        for field, reference in (("ingest", manifest.ingest), ("retrieve", manifest.retrieve)):
            if reference is not None:
                problems += _check_half(name, field, reference, manifest.config)

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


def main() -> int:
    """Validate every manifest; print problems and exit non-zero if any (the CI gate)."""
    problems = validate_datasources()
    if problems:
        print("data source validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("data source validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
