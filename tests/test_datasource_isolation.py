"""A data source's driver loads only in the process that uses that half of it (D-120).

A data source's heavy closure is a driver (a database client or vendor SDK). The registry answers
from manifests, so "which halves does this source have?" is data and the filter runs before any
adapter import; an ingest-only worker must not pay for retrieve-half drivers in image size,
memory and start-up.

Checked in a subprocess, because by the time a test runs `sys.modules` already holds what every
other test imported.
"""

import json
import subprocess
import sys
import textwrap
from typing import TypedDict

# Everything that can open a document. A share's *retrieve* half must bring none of it: the chat
# pod builds every active retrieve half to answer a question, and it has no reason to hold a PDF
# reader, a Word reader, a slide reader and a spreadsheet reader to do it.
_DOCUMENT_PARSERS = ("pypdf", "docx", "pptx", "openpyxl")

# Closures a *retrieve* half brings that an ingest-only worker has no use for. `rdkit` and `numpy`
# are absent because `core/chem.py` imports rdkit for unrelated reasons. `databricks` is a driver
# brought only by a warehouse source's half, the case the manifest seam exists for.
_RETRIEVE_ONLY_CLOSURE = ("drfp", "psycopg", "databricks")

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

    The ELN sync wants two strings and should pay for two strings; module-level adapter imports make
    this fail.
    """
    result = _probe()

    assert result["names"] == ["eln-json"], result
    assert result["third_party"] == [], result
    # `report.retrievers` is the retrieve-side closure (it pulls rdkit, drfp and the note index).
    # An ingest-only worker must never reach it. `report.evidence` is the shared DTO module that
    # `ingest/sources/base.py` imports for the contract itself, so it is expected and harmless.
    assert "chemclaw.retrieval.retrievers" not in result["report_modules"], result
    assert "chemclaw.retrieval.vector_index" not in result["report_modules"], result


_SHARE_PROBE = textwrap.dedent(
    """
    import json, sys

    from chemclaw.ingest.documents.retriever import ShareDocumentRetriever

    ShareDocumentRetriever(
        binding={"mount": "/mnt/x", "roots": [{"path": "."}], "public": True},
    name="sharedrive",
    )
    loaded = {m.split(".")[0] for m in sys.modules}
    print(json.dumps({"parsers": sorted(loaded & set(%r))}))
    """
)


def test_building_a_share_retriever_loads_no_document_parser() -> None:
    """Building a share retriever loads no document parser.

    The chat pod answers from the index, so the parsers stay behind
    `src/chemclaw/ingest/documents/parse.py`, imported only by the sync worker. A subprocess,
    because another test has already imported `pypdf` in this session.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _SHARE_PROBE % (_DOCUMENT_PARSERS,)],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(completed.stdout.strip().splitlines()[-1])
    assert loaded["parsers"] == [], loaded
