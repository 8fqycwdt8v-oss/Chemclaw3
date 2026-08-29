"""The capability-tool registry is populated on every production entrypoint, not just in tests.

**This file exists because a test suite cannot see this defect from the inside.** The registry is
filled by import side effect, so any test that imports `chemclaw.agent.chemclaw_agent` — directly or
through a fixture, a conftest, or another test module in the same session — leaves it populated for
everything that runs afterwards. A consumer that forgets to seed it therefore passes every
in-process test and serves nothing in production, which is what `api/mcp_face.py` did.

So each assertion runs in a **fresh interpreter** that imports one entrypoint and nothing else.

**The first version of this file contained a test that could not fail**, which is the defect it was
written to prevent, one level down. It asserted `len(_capability_tools())` — and that function
merges the `@tool` registry with the *generated* job and template launchers, which
`_register_generated_tools()` mints from manifests regardless. With the seeding import deleted it
still returned 23 callables and the assertion passed, while the in-process registry was empty. A
test of a union cannot see one member go to zero; this now asks the registry directly.
"""

import subprocess
import sys
from pathlib import Path

#: Enough that losing one tool *module* fails, not just losing the lot. The face's set is the
#: read-only intersection and is the smaller of the two, so it bounds both; the smallest module
#: contributing to it holds three tools, and the face currently advertises nine.
_MINIMUM_FACE_TOOLS = 7

#: The registry itself, which is what the seeding actually populates.
_MINIMUM_REGISTERED = 25


def _in_fresh_interpreter(source: str) -> str:
    """Run `source` in a new interpreter and return its stdout, failing loudly on a crash."""
    done = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, f"entrypoint import failed:\n{done.stderr[-2000:]}"
    return done.stdout.strip()


def test_the_mcp_face_advertises_tools_when_it_is_the_only_thing_imported() -> None:
    """The production entrypoint is `create_face_app`, and nothing runs before it.

    `deploy/entrypoint.sh`'s `mcp-face` case starts uvicorn against this factory, so whatever the
    registry holds at that moment is the whole surface the pod will ever serve. It held nothing.
    """
    count = _in_fresh_interpreter(
        "import chemclaw.api.mcp_face as f; print(len(f.advertised_tools()))"
    )
    assert int(count) >= _MINIMUM_FACE_TOOLS, (
        f"the face advertises {count} tool(s) when imported alone: the capability-tool registry is "
        "not seeded on this path, so the deployed pod answers tools/list with an empty array"
    )


def test_the_agent_builder_populates_the_registry_when_it_is_the_only_thing_imported() -> None:
    """The path that always had the seeding, asserted against the registry rather than the union.

    `registered_tool_names()` is exactly what the `@tool` decorators fill, so it goes to zero the
    moment the seeding is missing — which `_capability_tools()` does not, because it merges in
    generated launchers built from manifests. That is what made the earlier version of this test
    unable to fail.
    """
    count = _in_fresh_interpreter(
        "import chemclaw.agent.chemclaw_agent  # noqa: F401\n"
        "from chemclaw.core.tool_registry import registered_tool_names\n"
        "print(len(registered_tool_names()))"
    )
    assert int(count) >= _MINIMUM_REGISTERED, (
        f"importing the agent registers {count} in-process tool(s); the seeding is not on this path"
    )


def test_every_consumer_of_the_registry_seeds_it() -> None:
    """The real invariant, and the realistic regression: a *third* consumer that seeds nothing.

    The earlier version of this test failed a file only if it contained more than six
    `from chemclaw.agent import` lines — a threshold six away from any value in the tree, which
    detects a copy of the old import block and nothing else. What actually matters is that anything
    reading `registered_tools()`/`registered_tool_names()` has first imported the module that fills
    them, whether by naming it or by importing something that does.

    A file scan rather than a fresh interpreter, because this one is a property of the source.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    #: Both spellings of each, because `from chemclaw.agent import tool_modules` does not contain
    #: the dotted path — matching only the dotted form flagged the very file that does seed itself.
    seeders = (
        "chemclaw.agent.tool_modules",
        "chemclaw.agent.chemclaw_agent",
        "from chemclaw.agent import tool_modules",
        "from chemclaw.agent import chemclaw_agent",
    )

    unseeded = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "registered_tool" not in text:
            continue
        if path.name in {"tool_registry.py", "tool_modules.py", "chemclaw_agent.py"}:
            continue
        if not any(seeder in text for seeder in seeders):
            unseeded.append(str(path.relative_to(root.parent)))

    assert unseeded == [], (
        f"{unseeded} read the capability-tool registry without importing anything that fills it. "
        "The registry is filled by import side effect, so such a consumer sees an empty "
        "registry in production while every in-process test passes"
    )
