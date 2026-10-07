"""What is type-checked, what ships and what coverage measures are one rule (D-148).

There is one package, `src/chemclaw`, so no list of packages can drift. What is checked is that
`make type`, the wheel and coverage still name that one thing, and that no top-level import
package reappears beside `src/`.
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
    """No top-level `__init__.py` outside `src/`.

    A directory importable from the repository root shadows the installed package, so tests would no
    longer exercise what ships. `tests` and `examples` are the deliberate exceptions.
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


# What the image must not carry: the CUDA runtime a GPU build drags in. Matched by prefix against
# the lock's package names.
_GPU_RUNTIME_PREFIXES = ("nvidia-", "triton", "cuda-")
# The marker `[tool.uv] override-dependencies` uses to remove a transitive requirement: no platform
# satisfies it, so the edge is in the lock and never installed.
_NEVER = "sys_platform == 'never'"


def _locked_packages() -> list[dict[str, Any]]:
    with (_ROOT / "uv.lock").open("rb") as handle:
        packages: list[dict[str, Any]] = tomllib.load(handle)["package"]
    return packages


def test_linux_installs_the_cpu_build_of_torch() -> None:
    """The lock gives Linux the CPU torch, not the CUDA build PyPI defaults to there.

    torch comes in via bofire[optimization]; `[tool.uv.sources]` routes it to the PyTorch CPU index
    on Linux. Reads the lock because that is what `uv sync --frozen` installs in the image.
    """
    torches = [pkg for pkg in _locked_packages() if pkg["name"] == "torch"]
    linux = [
        pkg
        for pkg in torches
        if any("sys_platform == 'linux'" in marker for marker in pkg.get("resolution-markers", []))
    ]
    assert linux, f"no torch in uv.lock is resolved for Linux: {[p['version'] for p in torches]}"
    for pkg in linux:
        assert pkg["source"].get("registry") == "https://download.pytorch.org/whl/cpu", (
            f"torch {pkg['version']} for Linux comes from {pkg['source']}, not the CPU index — "
            "the image gets the CUDA build and its multi-GB runtime back"
        )

    # `make deps-audit` cannot look a `+cpu` local version up on PyPI and skips it; what keeps
    # torch audited is that the PyPI torch the other platforms lock is the *same* public version.
    # Let Linux drift to another release and its torch is never audited at all.
    audited = {pkg["version"] for pkg in torches if "+" not in pkg["version"]}
    for pkg in linux:
        public = pkg["version"].split("+", 1)[0]
        assert public in audited, (
            f"Linux torch {pkg['version']} has no PyPI twin in uv.lock ({sorted(audited)}), so "
            "deps-audit skips it. Relock both platforms to one torch release."
        )


def test_nothing_in_the_lock_installs_a_gpu_runtime() -> None:
    """No package in the closure pulls a CUDA/NCCL/Triton runtime on any platform.

    torch is closed by the CPU index and xgboost's `nvidia-nccl-cu12` by `override-dependencies`; a
    new dependency bringing one fails here rather than in an image.
    """
    edges = [
        f"{pkg['name']} {pkg['version']} -> {dep['name']} ({dep.get('marker', 'always')})"
        for pkg in _locked_packages()
        for dep in pkg.get("dependencies", [])
        if dep["name"].startswith(_GPU_RUNTIME_PREFIXES) and _NEVER not in dep.get("marker", "")
    ]
    assert not edges, "GPU runtime(s) in the locked closure:\n" + "\n".join(edges)
