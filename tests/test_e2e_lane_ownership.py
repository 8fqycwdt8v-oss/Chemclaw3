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

import re

import yaml

from tests.siblings import REPO_ROOT, bundles_declared_here

_UP = REPO_ROOT / "infra/live/e2e-full-stack/up.sh"


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
