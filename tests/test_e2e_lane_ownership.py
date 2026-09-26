"""The four-repo lane and `processes.sh` must never both start one server.

`infra/live/e2e-full-stack/up.sh` calls `infra/live/processes.sh up`, and the two keep pidfiles in
different run dirs. So a fleet server both scripts start is one `processes.sh` cannot recognise as
its own: its `running` check is false while the port is already served, and its collision guard
kills the lane at boot. That happened twice — `rxnpredict`, then `props` the day core gained a
`props` manifest — each time behind a comment in `up.sh` that enumerated `processes.sh`'s set and
had gone stale. The rule is derived here from the same test `processes.sh::fleet_bundle_names`
makes, so a new core manifest turns this red instead of the lane.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.siblings import REPO_ROOT, SIBLING_SKIP, bundles_declared_here, sibling_python

_UP = REPO_ROOT / "infra/live/e2e-full-stack/up.sh"
_PROCESSES = REPO_ROOT / "infra/live/processes.sh"


def _started_by_up() -> set[str]:
    """Every `start_<name>` the `up()` function calls, as the process name it starts."""
    text = _UP.read_text(encoding="utf-8")
    body = re.search(r"^up\(\) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)
    assert body is not None, f"{_UP} has no `up()` function"
    return {
        match.replace("_", "-")
        for match in re.findall(r"^\s*start_([a-z_]+)\b", body.group(1), re.MULTILINE)
    }


def test_up_starts_no_bundle_processes_sh_derives() -> None:
    """No server `up()` starts is one core declares an endpoint for.

    Core's endpoint-declaring bundles are the superset `fleet_bundle_names` intersects with the
    fleet's manifests, so staying out of it needs no sibling checkout to check.
    """
    started = _started_by_up()
    assert "pyexec" in started, f"{_UP}'s `up()` parse found {sorted(started)}; the regex is stale"
    owned_elsewhere = {
        name
        for name, manifest in bundles_declared_here().items()
        if (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("endpoint")
    }
    clash = sorted(started & owned_elsewhere)
    assert not clash, (
        f"{_UP}'s `up()` starts {clash}, which this repository declares an endpoint for — so "
        "`processes.sh::start_fleet_bundles` starts it too and dies on the served port. Remove the "
        "call; processes.sh is the one owner."
    )


def _function(name: str) -> str:
    """The body of shell function `name` in `up.sh`, as source that defines it."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", _UP.read_text(encoding="utf-8"), re.M | re.S)
    assert match is not None, f"{_UP} has no `{name}()` function"
    return match.group(0)


def test_up_checks_no_derived_bundle_s_credential_by_a_hardcoded_name() -> None:
    """A fleet bundle's credential check is derived, like its start, never a literal in `up()`.

    `start_props` moving to `processes.sh` took its credential check with it, and the literal list
    left behind (`chem`, `safety`) silently skipped `props`, `rxnpredict` and every later bundle.
    `calc` is exempt by name: the literal check is of the *backend* on `calc_server_url`, which is
    not a connector, while core's `calc` bundle is served by this repository's own process.
    """
    body = _function("up")
    assert "check_fleet_bundle_credentials" in body, f"{_UP}'s `up()` checks no fleet credential"
    literal = set(re.findall(r"^\s*assert_credential_accepted ([a-z_]+)\b", body, re.M))
    endpoint_bundles = {
        name
        for name, manifest in bundles_declared_here().items()
        if (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("endpoint")
    }
    clash = sorted((literal - {"calc"}) & endpoint_bundles)
    assert not clash, f"{_UP}'s `up()` names {clash}; `check_fleet_bundle_credentials` owns them"


def test_the_credential_check_covers_every_fleet_bundle_in_the_persisted_map(
    tmp_path: Path,
) -> None:
    """Driven: every URL-map entry the fleet publishes is checked, with the lane's own token."""
    live, fleet = tmp_path / "live", tmp_path / "fleet"
    (live / "run").mkdir(parents=True)
    for name in ("chem", "props"):
        (fleet / "manifests" / name).mkdir(parents=True)
        (fleet / "manifests" / name / "connector.yaml").write_text("name: x\n")
    urls = {name: f"http://127.0.0.1:1/{name}" for name in ("bo", "chem", "props")}
    (live / "run" / "connector-env.sh").write_text(
        f"export CHEMCLAW_CONNECTOR_URLS='{json.dumps(urls)}'\nexport CHEMCLAW_PROPS_TOKEN=file\n"
    )
    script = (
        "set -euo pipefail\n"
        'die() { echo "DIE $*"; exit 1; }\n'
        'assert_credential_accepted() { echo "CHECK $*"; }\n'
        f"{_function('check_fleet_bundle_credentials')}"
        f"LIVE_DIR={live} MCP_REPO={fleet} CHEMCLAW_PROPS_TOKEN=lane\n"
        f"check_fleet_bundle_credentials {sys.executable}\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("CHEMCLAW_")}
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=True
    ).stdout.splitlines()
    assert out == [
        "CHECK chem http://127.0.0.1:1/chem dev-token",
        "CHECK props http://127.0.0.1:1/props lane",
    ]


def _rxnpredict_lane_defaults() -> dict[str, str]:
    """The `CHEMCLAW_RXNPREDICT_*` defaults `processes.sh` exports for every lane it starts."""
    text = _PROCESSES.read_text(encoding="utf-8")
    return dict(
        re.findall(
            r'^export (CHEMCLAW_RXNPREDICT_[A-Z_]+)="\$\{\1:-([^}]*)\}"$', text, re.MULTILINE
        )
    )


def test_the_lane_starts_rxnpredict_with_its_deterministic_doubles() -> None:
    """`rxnpredict` comes up with a working forward and conditions surface, not an empty one.

    `up.sh`'s own `start_rxnpredict` set `fake_a`/`fake_c`; when `start_fleet_bundles` took the
    server over, the defaults went with the deleted function, and a fleet checkout with no ML extras
    then answers `/healthz` 200 with no predictor registered — every call fails while the lane reads
    green. This runs the fleet's own registration and readiness check under the lane's environment.
    """
    defaults = _rxnpredict_lane_defaults()
    assert set(defaults) == {
        "CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS",
        "CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS",
    }, f"{_PROCESSES} no longer exports rxnpredict's lane defaults: {defaults}"
    interpreter, reason = sibling_python("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if interpreter is None:
        pytest.skip(f"{SIBLING_SKIP} the doubles were NOT registered: {reason}")
    probe = (
        "import json, chemclaw_mcp_rxnpredict.tools\n"
        "from chemclaw_mcp_rxnpredict.engine.predictors import list_conditions, list_forward\n"
        "from chemclaw_mcp_rxnpredict.engine.readiness import verify_predictors\n"
        "verify_predictors()\n"
        "print(json.dumps([[p.name for p in list_forward()], [p.name for p in list_conditions()]]))"
    )
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("CHEMCLAW_RXNPREDICT_")
    } | defaults
    done = subprocess.run(
        [str(interpreter), "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    forward, conditions = json.loads(done.stdout.strip().splitlines()[-1])
    assert forward == [defaults["CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS"]]
    assert conditions == [defaults["CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS"]]
