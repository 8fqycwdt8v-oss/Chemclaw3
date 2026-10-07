"""A bundle's heavy dependencies must not reach the chat service's process (D-118).

`connector.yaml`'s `params_model` (`module:Class`) is imported by `build_job_tool` on every
`build_agent`, so a params module that imports science code drags that closure into the chat pod
silently. The boundary must hold because it is declared, whatever happens to be heavy today. Run
in a subprocess, because the test session's `sys.modules` is already polluted.
"""

import subprocess
import sys
import textwrap

# Third-party closures that must arrive only through a bundle's own worker. Deliberately *not*
# `rdkit` or `numpy`: `core/chem.py` imports rdkit for canonical SMILES, so it is core's own
# dependency regardless and naming it here would make the assertion a lie.
_HEAVY = ("tblite", "bofire", "botorch", "torch")

# First-party packages whose whole point is to live behind a bundle boundary.
_BUNDLE_ONLY_PACKAGES = ("calc",)

_PROBE = textwrap.dedent(
    """
    import json, sys
    from chemclaw.connectors.jobs import build_job_tool
    from chemclaw.connectors.registry import enabled

    for manifest in enabled():
        for job in manifest.jobs:
            build_job_tool(manifest.name, job)

    loaded = set(sys.modules)
    print(json.dumps(sorted(loaded)))
    """
)


def _modules_loaded_by_building_every_job_tool() -> set[str]:
    """Build every enabled bundle's job tools in a fresh interpreter; return what that imported."""
    import json

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_building_job_tools_loads_no_bundle_heavy_dependency() -> None:
    """The chat service resolves every `params_model` — none may drag a bundle's closure in."""
    loaded = _modules_loaded_by_building_every_job_tool()
    offenders = sorted(name for name in loaded if name.split(".")[0] in _HEAVY)
    assert not offenders, (
        f"building the connector job tools loaded {offenders} into the agent's process — a "
        "`params_model` is resolved by importing it, so it must name a leaf module "
        "(see connectors/calc/specs.py)"
    )


def test_building_job_tools_loads_no_bundle_only_first_party_package() -> None:
    """Same rule one level in: a bundle's own domain package is not core's to import."""
    loaded = _modules_loaded_by_building_every_job_tool()
    offenders = sorted(
        name
        for name in loaded
        if name.split(".")[0] in _BUNDLE_ONLY_PACKAGES
        and not name.startswith("chemclaw.connectors.")
    )
    assert not offenders, (
        f"building the connector job tools loaded {offenders} into the agent's process; the "
        "request models a manifest names must not import the bundle's result types"
    )
