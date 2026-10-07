"""Absolute paths to the declarations that ship inside the installed package (D-148)."""

from pathlib import Path

# The installed package root; `parents[2]` climbs config/ -> core/ -> chemclaw/.
_PACKAGE = Path(__file__).resolve().parents[2]


def _shipped(*parts: str) -> str:
    """An absolute path to a declaration that ships *inside* the package.

    Resolved against `__file__` rather than the CWD, so `uv run` from any directory, an installed
    wheel and the container agree. The env var still overrides, and the `PATH`-style lists still
    accept additional private directories.
    """
    return str(_PACKAGE.joinpath(*parts))
