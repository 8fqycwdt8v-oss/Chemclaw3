"""The locally-declared harness predicate types must keep matching the ones MAF actually uses.

`chemclaw.agent.harness_types` replaced an import from `agent_framework._harness._loop` — a private
module of an unbounded dependency — with local aliases, because both names are pure type aliases
with no runtime behaviour. That removes an `ImportError`-at-startup failure mode and creates a
quieter one: MAF could change the predicate's shape and nothing would notice, since a type alias is
erased at runtime and `mypy` would go on checking our code against our own stale copy.

This is the replacement signal. It reads MAF's private definitions *in a test*, where an
`ImportError` is a skipped test rather than a dead pod, and fails if they have moved apart. So the
divergence is caught in CI, naming what changed, instead of in production.

The comparison is on the alias *values* rather than on identity, because that is what a type alias
is: `ShouldContinueResult` is the string `'bool | tuple[bool, str | None]'` in MAF and a real
`TypeAlias` here, so the check normalizes both to their string form.
"""

import ast
from pathlib import Path
from types import ModuleType

import pytest

from chemclaw.agent import harness_types

_SRC = Path(__file__).resolve().parents[1] / "src" / "chemclaw"


def _maf_loop() -> ModuleType:
    """MAF's private loop module, or skip: this is about drift, not about MAF being installed."""
    module: ModuleType = pytest.importorskip(
        "agent_framework._harness._loop",
        reason="MAF's private harness loop module is gone — which is precisely the move this "
        "file exists to survive; `chemclaw.agent.harness_types` needs no rewrite for it.",
    )
    return module


def _normalized(alias: object) -> str:
    """Render a type alias to a comparable string, whichever form the two sides express it in."""
    text = alias if isinstance(alias, str) else str(alias)
    # MAF writes the forward reference `'ShouldContinueResult | Awaitable[ShouldContinueResult]'`
    # where ours is already resolved; strip quoting and module qualification so the two are
    # comparable as *shapes* rather than as spellings.
    return (
        text.replace("'", "")
        .replace("collections.abc.", "")
        .replace("typing.", "")
        .replace(" ", "")
    )


def test_the_result_alias_still_matches_maf() -> None:
    """A widened or narrowed result type would silently invalidate both wrappers' annotations."""
    theirs = _normalized(_maf_loop().ShouldContinueResult)
    ours = _normalized(harness_types.ShouldContinueResult)
    assert theirs == ours, (
        f"MAF's ShouldContinueResult is now {theirs!r} but "
        f"chemclaw.agent.harness_types declares {ours!r} — update the local alias, and check "
        "whether loop_cap and plan_gate still return what the harness expects"
    )


def test_the_callable_alias_still_matches_maf() -> None:
    """The predicate's own shape: parameters, and whether the loop still awaits the result."""
    theirs = _normalized(_maf_loop().ShouldContinueCallable)
    ours = _normalized(harness_types.ShouldContinueCallable)
    # MAF spells the return as a forward reference to its own alias; ours is expanded. Compare the
    # expanded form on both sides so the assertion is about the shape, not the indirection.
    theirs = theirs.replace(
        "ShouldContinueResult|Awaitable[ShouldContinueResult]",
        _normalized(harness_types.ShouldContinueResult)
        + "|Awaitable["
        + _normalized(harness_types.ShouldContinueResult)
        + "]",
    )
    assert theirs == ours, (
        f"MAF's ShouldContinueCallable is now {theirs!r} but "
        f"chemclaw.agent.harness_types declares {ours!r}"
    )


def test_nothing_imports_the_private_loop_module_any_more() -> None:
    """The point of the substitution: no production import path can fail at process start.

    `tests/test_third_party_layering.py` enforces the general rule for *declared* private imports;
    this asserts the specific one this module was written to remove, so deleting `harness_types`
    and reinstating the import fails here with the reason rather than only as an undeclared row.

    The scan is floored, because `assert not offenders` over an empty glob is a pass: a renamed
    `src/` layout would turn this into a test of nothing while still reporting green.
    """
    offenders = []
    scanned = 0
    for path in _SRC.rglob("*.py"):
        scanned += 1
        # Parsed, not grepped: `harness_types`'s own docstring names the module it replaced, and a
        # substring search would count that as the thing it forbids.
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "agent_framework._harness._loop"
            ):
                offenders.append(str(path.relative_to(_SRC)))
            elif isinstance(node, ast.Import) and any(
                alias.name.startswith("agent_framework._harness._loop") for alias in node.names
            ):
                offenders.append(str(path.relative_to(_SRC)))
    assert scanned > 100, f"only {scanned} files under {_SRC} — this scanned nothing worth scanning"
    assert not offenders, (
        f"{offenders} import MAF's private harness loop module again — that is an ImportError at "
        "process start of the front door and the worker on any release that moves it; "
        "chemclaw.agent.harness_types carries the same annotations with no import"
    )
