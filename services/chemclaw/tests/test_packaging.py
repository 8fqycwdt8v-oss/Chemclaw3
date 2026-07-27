"""The three "first-party packages" lists are one rule, checked against the filesystem (D-117).

Three places name the first-party packages, and all three had silently drifted:

- `make type` omitted `service` and `sources` — they type-checked only *transitively*, because
  `tests/` imports them, so deleting an importing test would have dropped a package from the gate
  without any signal.
- `[tool.hatch.build.targets.wheel] packages` and `[tool.coverage.run] source` both omitted
  `connectors` (37 modules — the entire capability surface) and `templates`. The wheel omission is
  the serious one: `pyproject.toml` states the invariant it was violating ("a non-editable
  `pip install` of the wheel must ship all of them or the `chemclaw` command and its imports
  break"), and nothing checked it. The coverage omission meant the 80% floor was measured over a
  tree that excluded the newest subsystem.

This is the same class of bug the repo already fixed once by hand for `connectors`/`templates` in
`make type`. A comment saying "keep this list in sync" is what failed; this test is the fix.
`tests/test_deploy_chart.py` already proves the pattern works for the Containerfile's COPY set.

The rule, stated once:

- **Type-checked** — every first-party package, plus `tests` itself. Nothing is exempt.
- **Shipped and coverage-measured** — every first-party package a deployed component can import,
  which is all of them except `tests` and `examples` (a walkthrough, not product code).
"""

import re
import tomllib
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent

# Not product code: `tests` is the checker, `examples` is a runnable walkthrough (D-029). Both are
# still type-checked — only shipping and coverage exclude them.
_NOT_SHIPPED = {"tests", "examples"}


def _packages_on_disk() -> set[str]:
    """Every first-party top-level package, discovered rather than listed."""
    return {
        entry.name
        for entry in _ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and (entry / "__init__.py").is_file()
    }


def _pyproject() -> dict[str, Any]:
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _make_type_packages() -> list[str]:
    """The package arguments `make type` passes to mypy."""
    makefile = (_ROOT / "Makefile").read_text()
    match = re.search(r"^\tuv run mypy (.+)$", makefile, re.MULTILINE)
    assert match, "the `type` target no longer invokes mypy the way this test reads it"
    return match.group(1).split()


def test_make_type_checks_every_first_party_package() -> None:
    """A package missing here is checked only as far as something else happens to import it."""
    checked = _make_type_packages()
    missing = sorted(_packages_on_disk() - set(checked))
    assert not missing, f"`make type` does not check: {missing}"
    stale = sorted(set(checked) - _packages_on_disk())
    assert not stale, f"`make type` names packages that no longer exist: {stale}"


def test_the_wheel_ships_every_package_a_deployment_imports() -> None:
    """The invariant `pyproject.toml` states in prose, finally enforced (audit ARC-1)."""
    shipped = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    expected = _packages_on_disk() - _NOT_SHIPPED
    assert set(shipped) == expected, (
        f"wheel packages != first-party packages; missing {sorted(expected - set(shipped))}, "
        f"stale {sorted(set(shipped) - expected)}"
    )


def test_coverage_measures_exactly_what_is_shipped() -> None:
    """The floor must be measured over the deployed tree, or it measures the wrong thing."""
    measured = _pyproject()["tool"]["coverage"]["run"]["source"]
    shipped = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert sorted(measured) == sorted(shipped), (
        "coverage `source` and the wheel `packages` must be the same set — coverage measures the "
        "shipped tree"
    )


def test_the_lists_are_sorted_so_a_diff_stays_readable() -> None:
    """Three hand-maintained lists; sorted order is what keeps an added package a one-line diff."""
    project = _pyproject()
    shipped = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    measured = project["tool"]["coverage"]["run"]["source"]
    assert shipped == sorted(shipped), "wheel `packages` is not sorted"
    assert measured == sorted(measured), "coverage `source` is not sorted"
    checked = _make_type_packages()
    assert checked == sorted(checked), "`make type`'s package list is not sorted"
