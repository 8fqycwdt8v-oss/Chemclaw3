"""Template arguments for a bundle this tree declares and does not run, checked offline.

**The gap this closes.** `make template-validate` resolves a tool *name* through every discovered
manifest, so a step naming `mtsr` passes; it resolves that tool's *arguments* through the bundle's
own server module, which a declared-not-served bundle has none of
(`connectors/registry.py::server_tools_module` returns `None` for it). Its own output says so —
`arguments unchecked` — and the only gate that did check them,
`chemclaw.cli.validate_template_args_live`, needs a **running** connector, which no CI lane has.

So a template naming a fleet tool could carry any argument key at all. That is not a hypothetical
risk: writing such a template means typing argument names for a server nothing in this lane can
introspect, which is the fabricated-argument shape
`D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-a-gate` refuses one layer over.

**The third authority.** Every fleet server records a `servers/<name>/tool-surface.json` — the same
file `tests/test_sibling_manifest_agreement.py` already reads for the `calc` and `rxnlabel` backend
seams, and for the same reason. It needs a shallow clone and no `.venv`, so unlike the live gate it
is the half that can plausibly run in CI. Read here, it makes a fleet template's arguments as
checkable as a local one's.

`ToolArguments` is the shared vocabulary rather than a fourth reading of "what does this tool
accept": `agent/template_surface.py`'s docstring makes that argument for its two existing
authorities, and this is the third one arriving under it.
"""

import json
from pathlib import Path

import pytest

from chemclaw.agent.template_surface import ToolArguments, argument_problems
from chemclaw.connectors.registry import discovered, server_tools_module
from chemclaw.templates.manifest import ToolStep
from chemclaw.templates.registry import discovered as discovered_templates
from tests.siblings import SIBLING_SKIP, sibling_root


def _recorded_surfaces(root: Path) -> dict[str, ToolArguments]:
    """Every tool the fleet records a surface for, by name, as `ToolArguments`.

    Scoped to the bundles *this* tree declares and does not serve — a fleet server nothing here
    names is not this repository's contract, and folding it in would make the returned set larger
    than the thing being checked.
    """
    surfaces: dict[str, ToolArguments] = {}
    for name, (_path, manifest) in discovered().items():
        if manifest.endpoint is None or server_tools_module(name) is not None:
            continue
        recorded = root / "servers" / name / "tool-surface.json"
        if not recorded.is_file():
            continue
        for tool, arguments in json.loads(recorded.read_text(encoding="utf-8")).items():
            surfaces[tool] = ToolArguments(
                accepted=frozenset(arguments),
                required=frozenset(a for a, spec in arguments.items() if spec.get("required")),
                # A recorded surface enumerates the parameters the server declares; there is no
                # `**kwargs` on the far side of an MCP call, so the unknown-key check is never
                # vacuous here — which is the one way this authority is stricter than the other two.
                takes_any_key=False,
            )
    return surfaces


def test_every_template_argument_for_a_fleet_tool_is_one_that_tool_takes() -> None:
    """The check `template-validate` prints `arguments unchecked` for.

    Skips with the reason when there is no sibling checkout, because a check that quietly shrinks
    is worse than one that says what it did not look at — the argument
    `cli/validate_connectors.py::unverified_tool_surfaces` makes about the identical blind spot one
    layer over, and the discipline `tests/conftest.py`'s epilogue exists to keep visible.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(
            f"{SIBLING_SKIP} no template argument naming a fleet tool was checked: {reason}. "
            "`make template-validate` name-checks those steps and says `arguments unchecked`; "
            "nothing in this run closed that."
        )
    surfaces = _recorded_surfaces(root)
    if not surfaces:
        pytest.skip(
            f"{SIBLING_SKIP} the fleet checkout at {root} records no tool surface for any bundle "
            "this tree declares and does not serve, so there was nothing to check against."
        )
    problems: list[str] = []
    for template in discovered_templates().values():
        for step in template.steps:
            if isinstance(step, ToolStep) and step.tool in surfaces:
                problems.extend(argument_problems(template, step, surfaces[step.tool]))
    assert not problems, (
        "these template steps pass argument keys the fleet's own recorded tool surface does not "
        "accept:\n  " + "\n  ".join(problems)
    )


def test_the_recorded_surface_is_read_for_the_bundles_that_have_no_local_server() -> None:
    """The scope is derived, not asserted non-empty, and this is what proves it is not vacuous.

    `tasks/lessons.md`'s standing rule: a filter that silently matches nothing turns every
    assertion downstream into a pass. If the process-development bundles stop being declared here,
    or the fleet stops recording their surfaces, this is what says so rather than the test above
    quietly checking zero templates.
    """
    root, reason = sibling_root("CHEMCLAW_MCP_REPO", "Chemclaw3-mcp")
    if root is None:
        pytest.skip(f"{SIBLING_SKIP} the fleet's recorded surfaces were NOT read: {reason}.")
    surfaces = _recorded_surfaces(root)
    # One tool from each declared-not-served bundle that records a surface, so a bundle dropping
    # out of either tree is visible here rather than in a silently smaller check.
    assert {"mtsr", "filtration_time", "rate_constant_at_temperature", "screen_hazards"} <= set(
        surfaces
    ), f"the recorded surfaces read back only {sorted(surfaces)}"


def test_a_wrong_argument_key_against_a_recorded_surface_is_caught() -> None:
    """The mutation this gate exists to fail on, driven rather than assumed.

    Without it, the test above passes whether or not `argument_problems` can see a recorded
    surface at all — the shape of every vacuous check this file's own header is about.
    """
    accepts = ToolArguments(
        accepted=frozenset({"mass_kg", "moles"}),
        required=frozenset({"mass_kg"}),
        takes_any_key=False,
    )
    step = ToolStep(
        id="s", kind="tool", purpose="x", tool="mtsr", arguments={"mass_lb": "${inputs.m}"}
    )
    template = next(iter(discovered_templates().values()))
    problems = argument_problems(template, step, accepts)
    assert any("mass_lb" in problem for problem in problems)
    assert any("omits required argument" in problem for problem in problems)
