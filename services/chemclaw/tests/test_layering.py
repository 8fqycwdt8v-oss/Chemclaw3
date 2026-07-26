"""Package layering: retrieval infrastructure must not depend on orchestration.

`report/` is the retrieval layer that `agents/` (and `workflows/`, `sources/`) build on, so the
dependency must point one way: report → chemclaw (shared kernel), never report → agents. This
was once violated via `agents.embedding_provider`, closing an agents ↔ report import cycle; the
embedding seam now lives in `chemclaw.embeddings`. Each module is imported in a fresh interpreter
(subprocess) so previously cached imports cannot mask a transitive dependency.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REPORT_MODULES = [
    "report",
    "report.evidence",
    "report.harness",
    "report.hybrid",
    "report.retrievers",
    "report.vector_index",
]

_CHECK = """
import importlib
import sys

importlib.import_module(sys.argv[1])
leaked = sorted(name for name in sys.modules if name == "agents" or name.startswith("agents."))
if leaked:
    raise SystemExit(f"{sys.argv[1]} transitively imports orchestration modules: {leaked}")
"""


@pytest.mark.parametrize("module", _REPORT_MODULES)
def test_report_does_not_import_agents(module: str) -> None:
    """Importing a report module in a clean interpreter pulls in nothing from `agents`."""
    result = subprocess.run(
        [sys.executable, "-c", _CHECK, module],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
