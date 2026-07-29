"""What is type-checked, what ships, and what coverage measures are one rule (D-117, D-148).

Three places used to name the first-party packages by hand — `make type`, the wheel's `packages`,
and coverage's `source` — and all three had silently drifted:

- `make type` omitted `service` and `sources`; they were type-checked only *transitively*, because
  `tests/` imported them, so deleting an importing test would have dropped a package from the gate
  with no signal at all.
- The wheel and coverage lists both omitted `connectors` (37 modules — the entire capability
  surface) and `templates`. The wheel omission was the serious one: `pyproject.toml` stated the
  invariant it was violating ("a non-editable `pip install` of the wheel must ship all of them or
  the `chemclaw` command and its imports break"), and nothing checked it.

D-148 removed the class of bug rather than detecting it: there is one package, `src/chemclaw`, so
there is no list to keep in sync and nothing that can be omitted from it. What is left to check is
that the three declarations still say that one thing, and that the `src/` layout stays honest — no
top-level import package may reappear beside `src/`, because that is exactly how the eighteen
accumulated.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"

# Not product code, and deliberately outside `src/`: `tests` is the checker and `examples` is a
# runnable walkthrough (D-029). Both are still type-checked — only shipping and coverage exclude
# them.
_NOT_SHIPPED = ("tests", "examples")


def _pyproject() -> dict[str, Any]:
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _make_type_targets() -> list[str]:
    """The paths `make type` passes to mypy."""
    makefile = (_ROOT / "Makefile").read_text()
    match = re.search(r"^\tuv run mypy (.+)$", makefile, re.MULTILINE)
    assert match, "the `type` target no longer invokes mypy the way this test reads it"
    return match.group(1).split()


def test_there_is_exactly_one_first_party_package() -> None:
    """`src/` holds the package and nothing else — the invariant the whole layout rests on."""
    entries = sorted(entry.name for entry in _SRC.iterdir() if not entry.name.startswith("."))
    assert entries == ["chemclaw"], f"src/ should hold only `chemclaw`, found {entries}"
    assert (_SRC / "chemclaw" / "__init__.py").is_file(), "src/chemclaw is not a package"


def test_no_import_package_has_reappeared_beside_src() -> None:
    """A top-level `__init__.py` outside `src/` is the eighteen-package layout growing back.

    It is also a real hazard rather than a style point: a directory importable from the repository
    root shadows the installed package for anything started there, so the code under test would
    stop being the code that ships. `tests` and `examples` are the two deliberate exceptions.
    """
    stray = sorted(
        entry.name
        for entry in _ROOT.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name not in _NOT_SHIPPED
        and (entry / "__init__.py").is_file()
    )
    assert not stray, (
        f"top-level import packages outside src/: {stray}. First-party code lives in "
        "src/chemclaw/ (D-148); see ARCHITECTURE.md for which subpackage it belongs to."
    )


def test_make_type_checks_the_package_and_both_exceptions() -> None:
    """Nothing is exempt from `mypy --strict` — not the package, not the tests that check it."""
    checked = set(_make_type_targets())
    assert checked == {"src", *_NOT_SHIPPED}, (
        f"`make type` checks {sorted(checked)}; expected src plus {list(_NOT_SHIPPED)}"
    )


def test_the_wheel_ships_the_package() -> None:
    """The invariant `pyproject.toml` states in prose, kept enforced (audit ARC-1)."""
    shipped = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert shipped == ["src/chemclaw"], f"wheel packages should be ['src/chemclaw'], got {shipped}"


def test_coverage_measures_exactly_what_is_shipped() -> None:
    """The floor must be measured over the deployed tree, or it measures the wrong thing."""
    measured = _pyproject()["tool"]["coverage"]["run"]["source"]
    assert measured == ["chemclaw"], (
        f"coverage `source` should be the shipped package ['chemclaw'], got {measured}"
    )


def test_the_console_script_points_at_a_module_that_exists() -> None:
    """`uv run chemclaw` is the documented front door; a stale entry point fails only on install."""
    script = _pyproject()["project"]["scripts"]["chemclaw"]
    module, _, attribute = script.partition(":")
    assert attribute, f"console script {script!r} names no callable"
    path = _SRC.joinpath(*module.split(".")).with_suffix(".py")
    assert path.is_file(), f"console script {script!r} points at missing module {path}"
    assert re.search(rf"^def {attribute}\b", path.read_text(), re.MULTILINE), (
        f"console script {script!r}: {path.name} defines no `{attribute}`"
    )
