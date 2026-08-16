"""Absolute paths to the declarations that ship inside the installed package (D-148).

Shared by the section modules whose defaults name a directory the repository itself
provides (connector bundles, data sources); see `_shipped`.
"""

from pathlib import Path

# The installed package root (`src/chemclaw/`, or wherever a wheel put it).
# `parents[2]` climbs config/ -> core/ -> chemclaw/; this module sits one package deeper
# than the single-file config.py that first computed it.
_PACKAGE = Path(__file__).resolve().parents[2]


def _shipped(*parts: str) -> str:
    """An absolute path to a declaration that ships *inside* the package (D-148).

    Two defaults name a directory of declarations the repository itself provides: the connector
    bundles and the data sources. (A third, the hazard rule table, went with
    `D-2026-08-15-safety-is-a-tool-not-a-gate` — the screen answers from `Chemclaw3-mcp` now, out
    of data baked into its image.) Before D-148 all of them were CWD-relative
    strings (`"connectors"`, `"sources"`, `"safety/rules.yaml"`), which only resolved when the
    process happened to be started from the repository root — which is precisely why the
    Containerfile had to COPY them into the workdir rather than just installing the package.

    Resolving them against `__file__` instead makes a `uv run` from any directory, an installed
    wheel, and the container all agree. The env var still overrides, and `connectors_dir` /
    `data_sources_dir` remain `PATH`-style lists, so pointing at an *additional* private bundle
    directory works exactly as before — that seam is what these defaults must not close.
    """
    return str(_PACKAGE.joinpath(*parts))
