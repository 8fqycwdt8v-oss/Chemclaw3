"""A data source's driver must load only in the process that uses that half of it (D-120).

The same defect as `test_connector_isolation.py`, in the seam next door, and it is worth stating
why both exist rather than one generic check: a connector's heavy closure is *compute* (`tblite`,
`bofire`), a data source's is a **driver** — a database client or vendor SDK. The connector seam
leaks through one YAML field; this one leaked through the registry's shape.

`sources/registry.py` used to hold `DATA_SOURCES: dict[str, Callable[[], DataSource]]`, mapping a
name to a lambda that constructed its adapter. Every one of those lambdas named a class, so the
module imported every adapter at module scope — and the two consumers want *disjoint* halves. The
durable ELN sync worker, asking only which sources it must ingest (`['eln-json']`, two strings),
loaded 836 modules including `rdkit`, `drfp`, `numpy` and `psycopg`, none of which it uses. The
retrieve half of a source it was not touching came along because the dict could not express that
the half existed without also naming what built it.

Nothing failed, which is the point. The cost is paid in image size, process memory and start-up
time, and it grows with every source added — in a system whose stated direction is significantly
more databases. A manifest fixes it by making "which halves does this source have?" answerable as
data, so the filter runs before the import.

A subprocess is not incidental — it is the only way to ask the question. By the time any test runs,
`sys.modules` already holds what every other test imported, so an in-process check would pass no
matter what the registry did.
"""

import json
import subprocess
import sys
import textwrap
from typing import TypedDict

# Closures a *retrieve* half brings that an ingest-only worker has no use for. `rdkit` and `numpy`
# are deliberately absent: `chemclaw/chem.py` imports rdkit for canonical SMILES, so a worker may
# hold it for reasons that have nothing to do with this seam, and asserting on it would make the
# test a lie the day some unrelated core import changes.
_RETRIEVE_ONLY_CLOSURE = ("drfp", "psycopg")

_PROBE = textwrap.dedent(
    """
    import json, sys

    from chemclaw.ingest.sources.registry import active_ingest_source_names

    names = active_ingest_source_names()
    loaded = set(sys.modules)
    print(json.dumps({
        "names": names,
        "third_party": sorted(t for t in {m.split(".")[0] for m in loaded} if t in %r),
        "report_modules": sorted(m for m in loaded if m.startswith("chemclaw.retrieval.")),
        "total": len(loaded),
    }))
    """
)


class _Probe(TypedDict):
    """What the subprocess reports back — typed so the assertions below are checked, not guessed."""

    names: list[str]
    third_party: list[str]
    report_modules: list[str]
    total: int


def _probe() -> _Probe:
    """Run the probe in a clean interpreter and return what it loaded."""
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE % (_RETRIEVE_ONLY_CLOSURE,)],
        capture_output=True,
        text=True,
        check=True,
    )
    result: _Probe = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


def test_asking_which_sources_to_ingest_imports_no_adapter_at_all() -> None:
    """`active_ingest_source_names()` answers from manifests — it constructs nothing.

    The strongest form of the property, and the one the durable ELN sync actually needs: it wants
    two strings, and it should pay for two strings. Counterfactually verified — restoring the
    module-level adapter imports makes this fail.
    """
    result = _probe()

    assert result["names"] == ["eln-json"], result
    assert result["third_party"] == [], result
    # `report.retrievers` is the retrieve-side closure (it pulls rdkit, drfp and the note index).
    # An ingest-only worker must never reach it. `report.evidence` is the shared DTO module that
    # `sources/base.py` imports for the contract itself, so it is expected and harmless.
    assert "chemclaw.retrieval.retrievers" not in result["report_modules"], result
    assert "chemclaw.retrieval.vector_index" not in result["report_modules"], result
