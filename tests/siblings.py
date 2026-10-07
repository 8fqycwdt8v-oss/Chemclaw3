"""The companion checkouts: where they are, and what each tree declares.

Some tests need a sibling repository on disk (`tests/test_context_floor.py`,
`tests/test_sibling_manifest_agreement.py`); without a checkout they skip, and
`tests/conftest.py::_report_sibling_skips` counts the skips. Where a checkout is is resolved by
`infra/live/siblings.sh`, invoked rather than reimplemented, so tests and the live lanes find the
same checkout. A missing `bash` fails the lookup to a reason string.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

#: The head of every skip message a missing companion checkout produces, so one reporter can find
#: them all. Defined once and imported by `tests/conftest.py`.
SIBLING_SKIP = "[no Chemclaw3-mcp checkout]"

REPO_ROOT = Path(__file__).resolve().parents[1]
_SIBLINGS_SH = REPO_ROOT / "infra" / "live" / "siblings.sh"


def _ask_shell(function: str, *args: str) -> tuple[str, str]:
    """Run one function out of `infra/live/siblings.sh`, returning `(stdout, reason)`.

    `REPO_ROOT` is that script's contract, set here to this checkout. A non-zero exit or a missing
    `bash` comes back as a reason, since every caller decides between running and skipping.
    """
    bash = shutil.which("bash")
    if bash is None:
        return "", "no `bash` on PATH, so the shared sibling search could not be consulted"
    script = (
        f"set -eu; REPO_ROOT={shlex.quote(str(REPO_ROOT))}; "
        f'. {shlex.quote(str(_SIBLINGS_SH))}; {function} "$1" "$2"'
    )
    # Both positionals always supplied: `set -u` makes an unset `$2` a fatal error, and the
    # one-argument function ignores the pad.
    positional = [*args, "", ""][:2]
    try:
        completed = subprocess.run(
            [bash, "-c", script, "--", *positional],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - environment
        return "", f"could not run {_SIBLINGS_SH}: {error}"
    if completed.returncode != 0:  # pragma: no cover - environment
        return "", f"{_SIBLINGS_SH} failed: {completed.stderr.strip()[-300:]}"
    return completed.stdout, ""


def env_var_names(canonical_name: str) -> tuple[str, ...]:
    """Every environment variable that may name this checkout, from the shell's own table.

    Read rather than restated so a skip message names the variables a reader can actually set —
    including the one the caller does not itself pass — and so adding a variable stays one edit.
    """
    listed, reason = _ask_shell("sibling_env_vars", canonical_name)
    if reason:  # pragma: no cover - environment
        return ()
    return tuple(name for name in listed.split() if name)


def sibling_root(env_var: str, canonical_name: str) -> tuple[Path | None, str]:
    """The sibling checkout, or `None` and the reason there is not one.

    `sibling_repo` prints its first candidate when it finds nothing, so the printed path is tested
    for existence.
    """
    printed, reason = _ask_shell("sibling_repo", env_var, canonical_name)
    if reason:
        return None, reason
    root = Path(printed.strip())
    if not root.is_dir():
        names = " or ".join(env_var_names(canonical_name) or (env_var,))
        return None, f"no {canonical_name} checkout at {root} (set {names})"
    return root, ""


def sibling_python(env_var: str, canonical_name: str) -> tuple[Path | None, str]:
    """The sibling checkout's own interpreter, or `None` and the reason there is not one.

    Separate from `sibling_root`: reading the fleet's manifests needs only a clone, while running
    its servers needs a built `.venv`, and a check needing the first should not be gated on the
    second.
    """
    root, reason = sibling_root(env_var, canonical_name)
    if root is None:
        return None, reason
    interpreter = root / ".venv" / "bin" / "python"
    if not interpreter.exists():
        return None, f"{root} has no .venv — run `make install` there to measure its schemas"
    return interpreter, ""


def fleet_published_bundles(root: Path) -> dict[str, Path]:
    """Every bundle `Chemclaw3-mcp`'s published `manifests/` directory offers, by declared name.

    Only that directory, because it is what a deployment points `CHEMCLAW_CONNECTORS_DIR` at;
    `manifests-internal/` holds servers this repository refuses to mount. Keyed on the directory, as
    `registry.discovered()` is; a manifest whose `name:` disagrees with its folder is rejected,
    which `tests/test_sibling_manifest_agreement.py` asserts separately.
    """
    return {
        manifest.parent.name: manifest
        for manifest in sorted((root / "manifests").glob("*/connector.yaml"))
    }


def fleet_published_tool_names(root: Path) -> dict[str, frozenset[str]]:
    """Every tool each published fleet bundle declares, by bundle name.

    The declared surface, read as YAML from a clone, so it is cheap enough for CI; the fleet checks
    declared against served itself. Reads `endpoint.tools`; a bundle with no `endpoint:` contributes
    an empty set rather than being dropped, so "declares nothing" is distinguishable from "not in
    the fleet".
    """
    declared: dict[str, frozenset[str]] = {}
    for name, path in fleet_published_bundles(root).items():
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        endpoint = document.get("endpoint") or {}
        declared[name] = frozenset(str(tool) for tool in endpoint.get("tools") or ())
    return declared


def bundles_declared_here() -> dict[str, Path]:
    """The bundle manifests *this repository's own tree* holds, whatever the environment says.

    Not `registry.discovered()`, which reads `CHEMCLAW_CONNECTORS_DIR` — under
    `infra/live/e2e-full-stack/up.sh` that already points at the fleet's manifests.
    """
    import chemclaw.connectors

    root = Path(chemclaw.connectors.__file__).resolve().parent
    return {bundle.parent.name: bundle for bundle in sorted(root.glob("*/connector.yaml"))}
