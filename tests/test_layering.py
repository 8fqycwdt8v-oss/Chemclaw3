"""Package layering: the kernel depends on nothing, and retrieval does not depend on orchestration.

`chemclaw.retrieval` is the layer `chemclaw.agent` (and `chemclaw.durable`, `chemclaw.ingest`)
build on, so the dependency must point one way: retrieval → core, never retrieval → agent. That
was once violated via `chemclaw.agent.embedding_provider`, closing an agent ↔ retrieval import
cycle; the
embedding seam now lives in `chemclaw.core.embeddings`.

D-147 grouped the packages by layer, which makes a second rule stateable that the flat layout could
only imply: **`chemclaw.core` is the shared kernel, so it imports no sibling.** A kernel that
reaches back up into a layer above it is how the first cycle formed, and it is much easier to do by
accident when the thing you need is one `from chemclaw.…` away.

Each module is imported in a fresh interpreter (subprocess) so previously cached imports cannot mask
a transitive dependency.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_RETRIEVAL_MODULES = [
    "chemclaw.retrieval",
    "chemclaw.retrieval.evidence",
    "chemclaw.retrieval.harness",
    "chemclaw.retrieval.hybrid",
    "chemclaw.retrieval.retrievers",
    "chemclaw.retrieval.vector_index",
]

_CORE_MODULES = [
    "chemclaw.core",
    "chemclaw.core.chem",
    "chemclaw.core.config",
    "chemclaw.core.db",
    "chemclaw.core.embeddings",
    "chemclaw.core.errors",
    "chemclaw.core.http",
    "chemclaw.core.ids",
    "chemclaw.core.logging",
    "chemclaw.core.reagents",
    "chemclaw.core.temporal_client",
]

# Importing `chemclaw.core.x` necessarily imports the `chemclaw` and `chemclaw.core` parents, so
# those two are not leaks; any *other* `chemclaw.` module is.
_CHECK = """
import importlib
import sys

target, forbidden = sys.argv[1], sys.argv[2]
allowed = {"chemclaw", "chemclaw.core"}
importlib.import_module(target)
leaked = sorted(
    name for name in sys.modules
    if name not in allowed
    and (name == forbidden or name.startswith(forbidden + "."))
)
if leaked:
    raise SystemExit(f"{target} transitively imports {forbidden}: {leaked}")
"""


def _assert_does_not_import(module: str, forbidden: str) -> None:
    """Import `module` in a clean interpreter and fail if `forbidden` shows up in `sys.modules`."""
    result = subprocess.run(
        [sys.executable, "-c", _CHECK, module, forbidden],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", _RETRIEVAL_MODULES)
def test_retrieval_does_not_import_orchestration(module: str) -> None:
    """A retrieval module in a clean interpreter pulls in nothing from `chemclaw.agent`."""
    _assert_does_not_import(module, "chemclaw.agent")


@pytest.mark.parametrize("module", _CORE_MODULES)
@pytest.mark.parametrize(
    "sibling",
    [
        "chemclaw.agent",
        "chemclaw.api",
        "chemclaw.connectors",
        "chemclaw.durable",
        "chemclaw.evals",
        "chemclaw.ingest",
        "chemclaw.kg",
        "chemclaw.mcp",
        "chemclaw.memory",
        "chemclaw.retrieval",
        "chemclaw.science",
        "chemclaw.templates",
    ],
)
def test_the_kernel_imports_no_sibling(module: str, sibling: str) -> None:
    """`chemclaw.core` is what everything else builds on, so it may build on none of them.

    `chemclaw.cli` is absent from the sibling list on purpose: it is the terminal entrypoint layer,
    nothing imports *it*, and listing it here would assert something no dependency edge can violate.
    """
    _assert_does_not_import(module, sibling)
