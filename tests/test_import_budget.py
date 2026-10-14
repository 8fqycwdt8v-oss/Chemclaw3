"""Importing the front door stays cheap: the heavy stacks load when something uses them.

`chemclaw.api.app` is imported by every worker, CLI and test collector that touches the API. The
agent framework's provider SDKs (about two thirds of `import deepagents`), the document parsers and
the numerical stack's sparse, linear-algebra and statistics parts each cost a second or more, and
none is needed until a turn compiles a graph or a tool uses one. A new module-level import that
brings one back fails here with the module that did it named by `python -X importtime`, which is
how to find it.

Measured in a fresh interpreter, because `sys.modules` of the test process already holds whatever
earlier tests imported.
"""

import json
import subprocess
import sys
from pathlib import Path

#: Loaded by importing the front door only if something reached for it at module level.
_MUST_NOT_BE_LOADED = (
    # The agent framework, imported when a graph is compiled (`langgraph_agent.prepare_imports`).
    "deepagents",
    "langchain_anthropic",
    "langchain_google_genai",
    "langchain_openai",
    "anthropic",
    "openai",
    "google.genai",
    # Imported inside the one function that uses each.
    "scipy.stats",
    "scipy.sparse",
    "scipy.linalg",
    "scipy.optimize",
    "pypdf",
    "docx",
    "openpyxl",
    "pptx",
    # Optimisation and learning stacks: loaded by the tools that need them, never by the front door.
    "bofire",
    "botorch",
    "torch",
    "pandas",
    "sklearn",
    "xgboost",
    "matplotlib",
)

#: A ceiling on the whole front door's module count, with headroom over what it loads today. It
#: exists so that growth the list above did not anticipate is still noticed.
_MODULE_CEILING = 3_500

_PROBE = """
import json, sys
import chemclaw.api.app
loaded = set(sys.modules)
print(json.dumps({
    "count": len(loaded),
    "present": sorted(
        name for name in %r
        if name in loaded or any(module.startswith(name + ".") for module in loaded)
    ),
}))
"""


def _imported() -> dict[str, object]:
    """What a fresh interpreter holds after `import chemclaw.api.app`."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE % (_MUST_NOT_BE_LOADED,)],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    result: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_importing_the_front_door_loads_none_of_the_heavy_stacks() -> None:
    """The front door loads no provider SDK, document parser or optimisation stack at import."""
    present = _imported()["present"]
    assert present == [], (
        f"importing chemclaw.api.app loaded {present}. Each is imported by the function that "
        "uses it, or by `langgraph_agent.prepare_imports` for the agent framework; find the "
        "module-level import with `python -X importtime -c 'import chemclaw.api.app'` and move it."
    )


def test_importing_the_front_door_stays_inside_its_module_budget() -> None:
    """The whole import stays under a module ceiling, so growth no list anticipated is noticed."""
    count = _imported()["count"]
    assert isinstance(count, int) and count <= _MODULE_CEILING, (
        f"importing chemclaw.api.app loads {count} modules against a ceiling of {_MODULE_CEILING}; "
        "see the heaviest with `python -X importtime`, and lazy-import before raising the ceiling"
    )
