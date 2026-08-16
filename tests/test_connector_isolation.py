"""A bundle's heavy dependencies must not reach the chat service's process (D-118).

This is the seam's central promise and the reason a capability "earns a bundle by taking a
dependency closure with it". It held for imports the eye can see — core's modules simply do not
`import chemclaw.connectors.calc` — and it was broken by the one field that resolves an import
invisibly.

`connector.yaml`'s `params_model` names a pydantic model as `module:Class`, and
`connectors/jobs.py` resolves it by importing that module. It does so inside `build_job_tool`,
which `agent/chemclaw_agent.py` calls on **every** `build_agent`. The `calc` bundle pointed its
five jobs at `connectors/calc/specs.py`, which imported four science modules for the *result* types
that lived alongside the request types. Measured on `main` at the time, building the enabled job
tools loaded **`tblite`** — a compiled quantum-chemistry library — **and fifteen science modules**
into the agent's process.

The heavy half has since left the repository altogether
(`D-2026-08-16-the-physics-leaves-the-cache-stays`), which makes this check *more* worth keeping
rather than less: the boundary it guards must hold because it is declared, not because nothing
heavy happens to exist on the other side of it today.

Nothing failed. The chat pod just carried, in memory and in its image, the whole closure the
bundle exists to keep out of it. That is the failure mode this file exists to make loud: a
correctness property no test asserted, broken by a change that looked like tidy organisation.

A subprocess is not incidental — it is the only way to ask the question. `sys.modules` in the test
session is already polluted by every other test's imports, so an in-process check would pass no
matter what.
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
