"""`.live/run/connector-env.sh` holds every credential the live lane minted; this is its file mode.

`infra/live/processes.sh` writes one file for a *second shell* to source — `connector_env`'s minted
per-bundle MCP bearers, the two fleet tokens it mints, the four the lane inherits, and, in the
enforced posture, the probe's Entra access token. Its comment has always said "0600 and under the
run dir". It was 0644, because of the one thing about `umask` in a subshell that a reader cannot see
by reading: for `( umask 077; … ) > file`, bash applies the compound command's redirection **in the
forked child before running its body**, so the file is created under the *inherited* umask and the
`umask 077` runs one syscall too late. The measurement is the first test below, in both directions,
because a rule about a shell built on a claim about that shell is worth grounding in the shell.

The other two drive the real function out of the real file. Extracting `write_connector_env` by name
and sourcing it is what makes this a test of the shipped script rather than of a copy of it: the
whole failure mode here is a form that *reads* correct, so a test that restates the form proves
nothing about the file anybody runs.

Not covered, and it is the interesting limit: the directory. `mkdir -p "$RUN_DIR"` creates
`.live/run` under the ambient umask, so on a shared host the file's own mode is what protects it,
not the path to it. `.live/` is gitignored (`tests/test_repo_map.py` has the tree rules; the ignore
itself is checked here), so none of this reaches a commit.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROCESSES = _ROOT / "infra" / "live" / "processes.sh"


def _bash(
    script: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `script` under bash with this repository's live-lane shell options."""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _mode(path: Path) -> int:
    """`path`'s permission bits alone."""
    return stat.S_IMODE(path.stat().st_mode)


def test_a_subshell_umask_does_not_cover_a_redirection_written_outside_it(tmp_path: Path) -> None:
    """The mechanism, measured in both directions — the reason the rule below is a rule.

    `( umask 077; … ) > f` and `( umask 077; { … } > f )` differ by where the `>` sits and by
    nothing else a reader would notice. One creates the file before the umask applies and one after,
    and the whole finding is that difference. Asserting only the safe form would leave the claim
    "the unsafe form is unsafe" as prose; asserting both makes the first assertion mean something.
    """
    result = _bash(
        "umask 022\n"
        "( umask 077; printf 'outer\\n' ) > outer.sh\n"
        "( umask 077; { printf 'inner\\n'; } > inner.sh )\n",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert _mode(tmp_path / "outer.sh") == 0o644, "bash no longer creates the file before the body"
    assert _mode(tmp_path / "inner.sh") == 0o600


@pytest.fixture
def write_connector_env(tmp_path: Path) -> str:
    """The real `write_connector_env`, lifted out of the real script with its callers stubbed.

    `sed` by function name rather than by line number, so the extraction survives the file moving
    under it and fails loudly if the function is renamed (`bash -c` reports an unset command).
    """
    body = subprocess.run(
        ["sed", "-n", "/^write_connector_env()/,/^}/p", str(_PROCESSES)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "umask 077" in body, "write_connector_env no longer sets a umask at all"
    (tmp_path / "run").mkdir()
    return (
        "set -euo pipefail\n"
        "umask 022\n"
        f'RUN_DIR="{tmp_path}/run"\n'
        'MCP_REPO="/nonexistent"\n'
        "CHEMCLAW_CONNECTOR_URLS='{}'\n"
        "CHEMCLAW_CHEM_TOKEN=chem-secret\n"
        "CHEMCLAW_SAFETY_TOKEN=safety-secret\n"
        # The real one derives these from the fleet's manifests; what matters here is only that the
        # *last* name it prints is one nothing in this script sets, which is the shipped shape.
        "fleet_token_vars() { printf 'CHEMCLAW_PROPS_TOKEN\\nCHEMCLAW_CALC_TOKEN\\n'; }\n"
        f"{body}\n"
    )


def test_the_lane_writes_its_credentials_owner_only(
    write_connector_env: str, tmp_path: Path
) -> None:
    """The file every connector's bearer and the probe's access token land in is not world-readable.

    Under a 022 umask, which is the default on every image this lane runs on. A second local account
    reading `.live/run/connector-env.sh` holds the credential each connector verifies and the token
    `make live-probes` presents to the front door.
    """
    result = _bash(
        write_connector_env + 'write_connector_env "export A=1"\n',
        tmp_path,
        {"CHEMCLAW_LIVE_PROBE_TOKEN": "probe-jwt", "CHEMCLAW_CALC_TOKEN": "calc-secret"},
    )
    assert result.returncode == 0, result.stderr
    written = tmp_path / "run" / "connector-env.sh"
    assert _mode(written) == 0o600, (
        f"mode {_mode(written):o}: any local account can read the lane's credentials"
    )
    text = written.read_text()
    # The probe token is part of this one write rather than a second, appended one — the append
    # subshell got the umask right and could not help, because the file already existed at 0644.
    assert "export CHEMCLAW_LIVE_PROBE_TOKEN=probe-jwt" in text
    assert "export CHEMCLAW_CALC_TOKEN=calc-secret" in text


def test_an_unset_trailing_token_does_not_fail_the_bring_up(
    write_connector_env: str, tmp_path: Path
) -> None:
    """`up` must survive the shipped shape: the last name `fleet_token_vars` prints is never set.

    The block this function replaced ended on `for … do [ -n "${!key:-}" ] && printf …; done`, so an
    unset *last* variable made the loop — and the subshell around it — return 1. Under
    `set -euo pipefail` that killed `processes.sh up` immediately after the file was written and
    before the connectors, the workers and the front door started, with `CHEMCLAW_CALC_TOKEN` (the
    last name, and one nothing in that script sets) taking it on the standalone `make live-up` path.
    """
    result = _bash(
        write_connector_env + 'write_connector_env "export A=1"\necho STILL-RUNNING\n',
        tmp_path,
        {"CHEMCLAW_CALC_TOKEN": "", "CHEMCLAW_LIVE_PROBE_TOKEN": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "STILL-RUNNING" in result.stdout, "the write ended the shell; nothing after it would run"


def test_the_lane_directory_is_ignored_by_git() -> None:
    """A credential file is only ever machine-local state; `git` must refuse to see it."""
    result = subprocess.run(["git", "check-ignore", "-q", ".live/run/connector-env.sh"], cwd=_ROOT)
    assert result.returncode == 0, (
        ".live/ is no longer gitignored — the lane's tokens are commitable"
    )
