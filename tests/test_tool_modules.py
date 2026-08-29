"""The capability-tool registry is populated on every production entrypoint, not just in tests.

**This file exists because a test suite cannot see this defect from the inside.** The registry is
filled by import side effect, so any test that imports `chemclaw.agent.chemclaw_agent` — directly or
through a fixture, a conftest, or another test module in the same session — leaves the registry
populated for everything that runs afterwards. A consumer that forgets to seed it therefore passes
every in-process test and serves nothing in production, which is exactly what `api/mcp_face.py` did.

So each assertion runs in a **fresh interpreter** that imports one entrypoint and nothing else. That
is the only arrangement in which these can fail.
"""

import subprocess
import sys

#: Long enough that a partially-seeded registry fails too, not just an empty one. The face's set is
#: the read-only intersection, so it is the smaller of the two and bounds both.
_MINIMUM_FACE_TOOLS = 5


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


def test_the_agent_builder_advertises_tools_when_it_is_the_only_thing_imported() -> None:
    """The same property for the path that always had it, so a regression here is caught too."""
    count = _in_fresh_interpreter(
        "from chemclaw.agent.chemclaw_agent import _capability_tools as t; print(len(t()))"
    )
    assert int(count) >= _MINIMUM_FACE_TOOLS, (
        f"the agent assembles {count} in-process tool(s) when imported alone"
    )


def test_seeding_the_registry_is_one_module_rather_than_a_block_each_consumer_repeats() -> None:
    """One definition, so a third consumer inherits the seeding instead of rediscovering it.

    Asserted as an absence: a consumer that hand-rolled the import block again would work, and
    would put the tool list back in two places — which is how the two got to disagree.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "chemclaw"
    seeding = root / "agent" / "tool_modules.py"
    assert seeding.is_file(), "the one seeding module is gone; consumers are seeding themselves"

    repeats = [
        str(path.relative_to(root.parent))
        for path in sorted(root.rglob("*.py"))
        if path != seeding
        and path.read_text(encoding="utf-8").count("from chemclaw.agent import ") > 6
    ]
    assert repeats == [], (
        f"{repeats} import many agent modules directly, which is how the registry seeding got "
        "duplicated; import `chemclaw.agent.tool_modules` instead"
    )
