"""A reference corpus baked into the image at build time — the one sanctioned escalation of D-089.

D-089 fixed the scope: **this system takes no external data sources.** That decision stands and
`tests/test_no_egress.py` keeps enforcing it. What it correctly rules out is a *runtime* dependency
on somebody else's service — an address in first-party code, a network call on the retrieval path,
an availability and licensing question the deployment cannot answer. What it was never meant to
rule out is knowing things.

The gap that leaves is concrete. `chemclaw/reagents.py` is a hand-maintained name→SMILES table, and
it is the ceiling on `resolve_compound`: a molecule nobody typed into that file does not resolve, so
a chemist naming an ordinary reagent gets nothing back. Every fix for that is a dataset.

**So a dataset arrives the way a dependency arrives: pinned, checksummed, licensed, and installed at
build time.** It is reviewed once, in a pull request, by a person who can read its licence — exactly
like adding a package — and at runtime it is a file on local disk that this module reads. There is
no network path here at all: this module imports no HTTP client, and it cannot, because the egress
test asserts it.

**What this does and does not claim to be.** It is a retriever over a local corpus, so a vendored
reagent table can be *cited* like any other evidence. It is not an ingest half: vendored data is
reference material, not experiments, and giving it a write path into the knowledge graph would put
unreviewed third-party records behind the PR-gate's back.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.retrieval.evidence import EvidenceChunk

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "dataset.json"


class VendoredDatasetError(ChemclawError):
    """A vendored dataset is absent, malformed, or does not match its manifest."""


class DatasetManifest(BaseModel):
    """The provenance a vendored dataset must carry to be usable at all.

    Every field is required, and that is the point of the model. A dataset with no recorded licence
    is a legal question nobody can answer later; one with no version cannot be reproduced; one with
    no checksum cannot be shown to be what the review approved. Refusing to load an unlabelled
    corpus is cheaper than discovering, during an audit, that nobody knows where it came from.

    `retrieved_from` is documentation of where a human obtained the file, recorded so provenance
    survives. Nothing reads it as an address and nothing here can fetch it.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    licence: str = Field(min_length=1)
    retrieved_from: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # SHA-256 of `records.csv`, so the file the deployment ships is provably the file that was
    # reviewed. Verified on load when `vendored_dataset_verify` is on.
    sha256: str = Field(min_length=64, max_length=64)
    # Column holding the text a query matches against, and the one holding the structure.
    text_column: str = Field(min_length=1)
    smiles_column: str | None = None


class VendoredRecord(BaseModel):
    """One row of a vendored dataset, reduced to what a retriever needs."""

    text: str
    smiles: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


def _read_manifest(directory: Path) -> DatasetManifest:
    """Parse and validate the dataset's manifest, or say precisely what is wrong with it."""
    path = directory / MANIFEST_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VendoredDatasetError(f"no vendored dataset manifest at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VendoredDatasetError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return DatasetManifest.model_validate(raw)
    except ValidationError as exc:
        raise VendoredDatasetError(f"{path} is not a usable dataset manifest: {exc}") from exc


def _read_records(directory: Path, manifest: DatasetManifest) -> list[VendoredRecord]:
    """Read `records.csv` into typed rows, verifying it against the manifest's checksum."""
    import hashlib

    path = directory / "records.csv"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VendoredDatasetError(f"vendored dataset {manifest.name} has no {path}") from exc

    if settings.vendored_dataset_verify:
        digest = hashlib.sha256(data).hexdigest()
        if digest != manifest.sha256:
            raise VendoredDatasetError(
                f"vendored dataset {manifest.name} does not match its manifest: {path} hashes to "
                f"{digest}, manifest says {manifest.sha256}. The shipped data is not what was "
                "reviewed — rebuild the image rather than editing the manifest."
            )

    rows = list(csv.DictReader(data.decode("utf-8").splitlines()))
    if rows and manifest.text_column not in rows[0]:
        raise VendoredDatasetError(
            f"vendored dataset {manifest.name} declares text_column "
            f"{manifest.text_column!r}, which {path} does not have"
        )
    return [
        VendoredRecord(
            text=row[manifest.text_column],
            smiles=row.get(manifest.smiles_column) if manifest.smiles_column else None,
            fields={key: value for key, value in row.items() if value},
        )
        for row in rows
        if row.get(manifest.text_column)
    ]


class VendoredDatasetRetriever:
    """Retrieve from a dataset baked into the image at build time. A `SourceRetriever`.

    Loads lazily and once: a dataset is immutable for the life of the image, so re-reading it per
    query would be pure cost. A load failure is logged and yields no evidence rather than raising —
    a missing optional corpus must not break every retrieval in the process, and the log line names
    the dataset and the reason.
    """

    name = "vendored"

    def __init__(self, dataset_dir: str | None = None, name: str | None = None) -> None:
        """Read from `dataset_dir`, or the configured `vendored_dataset_dir`."""
        self._dir = Path(dataset_dir) if dataset_dir is not None else settings.vendored_dataset_path
        if name is not None:
            self.name = name
        self._records: list[VendoredRecord] | None = None
        self._manifest: DatasetManifest | None = None

    def _load(self) -> list[VendoredRecord]:
        """The dataset's rows, read once per process."""
        if self._records is not None:
            return self._records
        try:
            self._manifest = _read_manifest(self._dir)
            self._records = _read_records(self._dir, self._manifest)
            logger.info(
                "vendored dataset %s v%s loaded: %d records (%s)",
                self._manifest.name,
                self._manifest.version,
                len(self._records),
                self._manifest.licence,
            )
        except VendoredDatasetError:
            logger.warning("vendored dataset unavailable at %s", self._dir, exc_info=True)
            self._records = []
        return self._records

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for records whose text contains `query`, best first.

        Substring matching, deliberately: this is a *reference* corpus of short labelled records —
        names, synonyms, structures — not prose, and a lookup table wants exact containment rather
        than a relevance model. A shorter matching record ranks first, because on a name table the
        shortest containing entry is the closest thing to an exact match.

        Every chunk cites `vendored:<dataset>:<row>`. That is not a knowledge-graph note id, and
        it is not pretending to be one: the citation must resolve to *something a reader can check*,
        and for vendored data that is the row in the pinned, checksummed file.
        """
        needle = query.strip().lower()
        if not needle:
            return []
        records = self._load()
        dataset = self._manifest.name if self._manifest else "unknown"
        matches = [
            (index, record) for index, record in enumerate(records) if needle in record.text.lower()
        ]
        matches.sort(key=lambda pair: (len(pair[1].text), pair[0]))
        limited = matches[: settings.retrieval_top_k]
        return [
            EvidenceChunk(
                content=_describe(record),
                source_note_id=f"vendored:{dataset}:{index}",
                retriever=self.name,
                # A lookup hit is exact or it is not there, so every match scores alike; ordering
                # is carried by the list order RRF reads, not by a similarity this source cannot
                # honestly compute.
                score=1.0,
            )
            for index, record in limited
        ]


def _describe(record: VendoredRecord) -> str:
    """One line describing a record — its text, its structure, and any other populated column."""
    parts = [record.text]
    if record.smiles:
        parts.append(f"SMILES {record.smiles}")
    extras = sorted(
        f"{key}: {value}"
        for key, value in record.fields.items()
        if value not in (record.text, record.smiles)
    )
    return " — ".join([*parts, *extras])
