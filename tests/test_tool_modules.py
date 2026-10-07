"""The capability-tool registry is populated on every production entrypoint, not just in tests.

The registry is filled by import side effect, so any in-process test leaves it populated for
everything after it, and a consumer that forgets to seed it passes in-process and serves nothing
in production. Each assertion therefore runs in a fresh interpreter importing one entrypoint, and
asks the `@tool` registry directly: `_capability_tools()` also merges generated launchers, so it
never reaches zero.
"""

import subprocess
import sys
from pathlib import Path

#: Enough that losing one tool module fails, not just losing the lot. The face's read-only set is
#: the smaller of the two and bounds both; its smallest contributing module holds three tools, so
#: the floor is the advertised count minus three, plus one.
_MINIMUM_FACE_TOOLS = 4

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
    """The agent builder alone populates the `@tool` registry.

    Asserted on `registered_tool_names()`, which goes to zero without the seeding, unlike
    `_capability_tools()`.
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
    """Every file that reads the registry has imported the module that fills it.

    A source scan rather than a fresh interpreter, because this is a property of the source.
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
