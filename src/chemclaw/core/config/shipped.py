"""Absolute paths to the declarations that ship inside the installed package (D-148).

Shared by the section modules whose defaults name a directory the repository itself
provides (connector bundles, data sources, the hazard rule table); see `_shipped`.
"""

from pathlib import Path

# The installed package root (`src/chemclaw/`, or wherever a wheel put it).
# `parents[2]` climbs config/ -> core/ -> chemclaw/; this module sits one package deeper
# than the single-file config.py that first computed it.
_PACKAGE = Path(__file__).resolve().parents[2]


def _shipped(*parts: str) -> str:
    """An absolute path to a declaration that ships *inside* the package (D-148).

    Three defaults name a directory of declarations the repository itself provides: the connector
    bundles, the data sources, and the hazard rule table. Before D-148 all three were CWD-relative
    strings (`"connectors"`, `"sources"`, `"safety/rules.yaml"`), which only resolved when the
    process happened to be started from the repository root — which is precisely why the
    Containerfile had to COPY them into the workdir rather than just installing the package.

    Resolving them against `__file__` instead makes a `uv run` from any directory, an installed
    wheel, and the container all agree. The env var still overrides, and `connectors_dir` /
    `data_sources_dir` remain `PATH`-style lists, so pointing at an *additional* private bundle
    directory works exactly as before — that seam is what these defaults must not close.
    """
    return str(_PACKAGE.joinpath(*parts))
