"""The companion checkouts: where they are, and what each tree declares.

Where they are is asked of the one script that already knows.

Some tests in this suite need a sibling repository on disk — `tests/test_context_floor.py` runs
`Chemclaw3-mcp`'s own servers to bound the half of the request prefix this repository cannot
measure, and `tests/test_sibling_manifest_agreement.py` reads the fleet's manifests and its
recorded `calc` surface. Every one of them is **opt-in**: without a checkout it skips, loudly, and
a skip is not a pass. How many that is, the run says — `tests/conftest.py::_report_sibling_skips`
counts them and names what the run is therefore not evidence about.

**Which makes the search itself load-bearing, and it was wrong.** `tests/test_context_floor.py`
searched exactly one path in one casing (`../Chemclaw3-mcp`) and read one variable
(`CHEMCLAW_MCP_CHECKOUT`), while `infra/live/siblings.sh` — merged the same day, in a different
pull request, with a header describing this bug being fixed for the live lanes — searched four
candidates and read `CHEMCLAW_MCP_REPO`. Measured on the container this repository's own tooling
provisions, with the fleet checked out at `../8fqycwdt8v-oss/chemclaw3-mcp`: the live lanes
resolved it and the ratchet skipped. So `SERVED_ELSEWHERE_ALLOWANCE`, and therefore `PREFIX_BOUND`,
and therefore both compaction defaults `core/config/agent.py` derives from it, had never been
checked by a machine anywhere.

**So there is one resolution and it is the shell's**, invoked here rather than reimplemented.
A Python copy of that search would be a second answer to one question — which is the
defect `infra/live/siblings.sh` exists to have ended, and re-committing it inside the fix would be
this repository's favourite kind of mistake. The cost is a `bash` subprocess per lookup, tens of
milliseconds, on a path that then spawns the sibling's interpreter anyway; and where `bash` is
missing the lookup fails to a reason string, so the caller skips with an explanation instead of
resolving something different from what `make live-up` would.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

#: The head of every skip message a missing companion checkout produces, so one reporter can find
#: them all. Defined here rather than restated in `tests/conftest.py` because a marker that is
#: transcribed is a marker that drifts, and the whole subject of this module is one fact declared
#: twice: a reporter matching a phrase two files claim to share is that defect, one layer up.
SIBLING_SKIP = "[no Chemclaw3-mcp checkout]"

REPO_ROOT = Path(__file__).resolve().parents[1]
_SIBLINGS_SH = REPO_ROOT / "infra" / "live" / "siblings.sh"


def _ask_shell(function: str, *args: str) -> tuple[str, str]:
    """Run one function out of `infra/live/siblings.sh`, returning `(stdout, reason)`.

    `REPO_ROOT` is that file's stated contract — the sourcing script defines it first — so it is
    set here to this checkout, exactly as `infra/live/processes.sh` and `e2e-full-stack/up.sh` set
    it to theirs. A non-zero exit or a missing `bash` comes back as a reason rather than an
    exception: every caller of this module is deciding between running and skipping, and a sibling
    somebody has not cloned is a fact about their machine rather than a regression.
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

    `sibling_repo` prints its first candidate when it finds nothing, so that a live lane's own
    `die` can name a concrete place to clone into. A test has to tell that apart from a hit, and
    the only way to is to test the printed path — which is what makes the fallback safe to keep.
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

    Separate from `sibling_root` because the two costs are different and only one of them is
    plausible in CI. Reading the fleet's manifests and its recorded `calc` surface needs a shallow
    clone and no install; running its servers to measure their tool schemas needs a built `.venv`
    with RDKit, torch and a T5 checkpoint's dependencies in it. A check that needs only the first
    should not be gated on the second.
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

    That directory and no other, because it is the one a deployment points
    `CHEMCLAW_CONNECTORS_DIR` at. The fleet keeps `calc` and `rxnlabel` in `manifests-internal/`,
    which no published `export` line names and whose manifests declare a `mount:` key this
    repository's `extra="forbid"` manifest model refuses outright — so counting "the bundles the
    fleet serves" as six counts two servers this repository is built to be unable to mount, and
    one of them (`calc`) would take the calculation cache off the agent's surface if it could.

    Keyed on the **directory**, which is what `registry.discovered()` keys on and therefore what
    resolves a collision on `CHEMCLAW_CONNECTORS_DIR`. `_load_manifest` rejects a manifest whose
    `name:` disagrees with its folder outright, so the two are equal for anything this repository
    can load at all — and a fleet manifest where they disagree is a startup error rather than a
    differently-named bundle, which `tests/test_sibling_manifest_agreement.py` asserts separately.
    """
    return {
        manifest.parent.name: manifest
        for manifest in sorted((root / "manifests").glob("*/connector.yaml"))
    }


def bundles_declared_here() -> dict[str, Path]:
    """The bundle manifests *this repository's own tree* holds, whatever the environment says.

    Deliberately not `registry.discovered()`, which reads `CHEMCLAW_CONNECTORS_DIR`: every caller
    below is asking which names the two trees *both* declare, and under
    `infra/live/e2e-full-stack/up.sh` that variable already holds the fleet's manifests — so
    `discovered()` would report the sibling's declarations as this one's and the check would fail
    on a configuration rather than on a drift.
    """
    import chemclaw.connectors

    root = Path(chemclaw.connectors.__file__).resolve().parent
    return {bundle.parent.name: bundle for bundle in sorted(root.glob("*/connector.yaml"))}
