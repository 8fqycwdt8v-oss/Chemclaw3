"""A reference corpus baked into the image at build time.

This system takes no runtime external data sources (`tests/test_no_egress.py`), but it may know
things: a dataset arrives like a dependency, pinned, checksummed, licensed, reviewed in a pull
request and installed at build time, then read from local disk. This module imports no HTTP client.
It is a retriever only, so vendored records can be cited as evidence; it has no ingest half, because
third-party reference data must not enter `knowledge/` under this system's own provenance.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, model_validator

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

    **`mirrored` is the question a manifest must answer before it can go stale**
    (`D-2026-09-14-a-mirror-with-no-owner-goes-stale-in-silence`). A corpus copied from somewhere
    else has an upstream that moves; the copy does not, and nothing in this system can tell. So a
    mirrored corpus must also name `refresh_owner` and `refresh_cadence`, and the field is required
    rather than defaulted because a default answers the question on the author's behalf — which is
    the one thing a provenance model must never do. First-party content (`mirrored: false`) has no
    upstream and needs neither.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    licence: str = Field(min_length=1)
    retrieved_from: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # Is this a copy of a corpus maintained somewhere else? Required, never defaulted — see above.
    mirrored: bool
    # Who re-takes the snapshot, and how often. Required exactly when `mirrored` is true.
    refresh_owner: str | None = None
    refresh_cadence: str | None = None
    # SHA-256 of `records.csv`, so the file the deployment ships is provably the file that was
    # reviewed. Verified on load when `vendored_dataset_verify` is on.
    sha256: str = Field(min_length=64, max_length=64)
    # Column holding the text a query matches against, and the one holding the structure.
    text_column: str = Field(min_length=1)
    smiles_column: str | None = None

    @model_validator(mode="after")
    def _a_mirror_names_who_refreshes_it(self) -> "DatasetManifest":
        """A mirrored corpus without an owner and a cadence is a stale corpus waiting to happen.

        Enforced at load. Conversely, first-party content may not name a refresh owner, since there
        is no upstream.
        """
        named = [
            field
            for field in ("refresh_owner", "refresh_cadence")
            if (getattr(self, field) or "").strip()
        ]
        if self.mirrored and len(named) < 2:
            missing = sorted({"refresh_owner", "refresh_cadence"} - set(named))
            raise ValueError(
                f"dataset {self.name!r} is mirrored from somewhere else and does not say "
                f"{' or '.join(missing)}. A snapshot with no named owner and no cadence goes "
                "stale with nobody knowing it has."
            )
        if not self.mirrored and named:
            raise ValueError(
                f"dataset {self.name!r} is not mirrored and names {', '.join(sorted(named))}. "
                "First-party content has no upstream to refresh from, and saying otherwise sends "
                "the next reader looking for one."
            )
        return self


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
    # `UnicodeDecodeError` is neither a `JSONDecodeError` nor an `OSError`, so it is wrapped
    # explicitly into the non-retryable `VendoredDatasetError`.
    except UnicodeDecodeError as exc:
        raise VendoredDatasetError(
            f"{path} is not UTF-8: byte {exc.object[exc.start]:#04x} at offset {exc.start} is "
            f"not valid ({exc.reason}). The manifest is part of the image, so rebuild it rather "
            "than retrying"
        ) from exc
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

    try:
        text = data.decode("utf-8")
    # The checksum already passed, so the bytes are the reviewed ones and a decode failure is
    # permanent: raised as the non-retryable error, naming the byte and offset.
    except UnicodeDecodeError as exc:
        raise VendoredDatasetError(
            f"vendored dataset {manifest.name} has a {path.name} that is not UTF-8: byte "
            f"{exc.object[exc.start]:#04x} at offset {exc.start} is not valid ({exc.reason}). "
            "The file is in the image and its checksum matched, so this is what was reviewed — "
            "re-export it as UTF-8 and rebuild"
        ) from exc
    rows = list(csv.DictReader(text.splitlines()))
    if rows and manifest.text_column not in rows[0]:
        raise VendoredDatasetError(
            f"vendored dataset {manifest.name} declares text_column "
            f"{manifest.text_column!r}, which {path} does not have"
        )
    records = [
        VendoredRecord(
            text=row[manifest.text_column],
            smiles=row.get(manifest.smiles_column) if manifest.smiles_column else None,
            fields={key: value for key, value in row.items() if value},
        )
        for row in rows
        if row.get(manifest.text_column)
    ]
    # Rows with an empty text cell are dropped rather than refused, but counted in one log line per
    # load, since the reviewed file holds more rows than are served.
    if len(records) != len(rows):
        logger.warning(
            "vendored dataset %s: %d of %d rows in %s have an empty %r and were not loaded, so "
            "this corpus serves fewer rows than the file its checksum verified",
            manifest.name,
            len(rows) - len(records),
            len(rows),
            path.name,
            manifest.text_column,
        )
    return records


class VendoredDatasetRetriever:
    """Retrieve from a dataset baked into the image at build time. A `SourceRetriever`.

    Loads lazily and caches only on success, since the dataset is immutable. A load failure raises
    and is not cached, so the evidence sweep marks this branch failed instead of the corpus
    reporting "no matches" for the life of the pod.
    """

    def __init__(self, dataset_dir: str | None = None, name: str = "vendored") -> None:
        """Read from `dataset_dir`, or the configured `vendored_dataset_dir`.

        `name` is the data-source name the registry passes from the manifest.
        """
        self._dir = Path(dataset_dir) if dataset_dir is not None else settings.vendored_dataset_path
        self.name = name
        self._records: list[VendoredRecord] | None = None
        self._manifest: DatasetManifest | None = None

    def _load(self) -> list[VendoredRecord]:
        """The dataset's rows, read once per process, on success only.

        Raises:
            VendoredDatasetError: The corpus is not there, does not match its checksum, or does not
                have the column its manifest declares. Not cached, so a corpus that appears later is
                seen.
        """
        if self._records is not None:
            return self._records
        self._manifest = _read_manifest(self._dir)
        self._records = _read_records(self._dir, self._manifest)
        logger.info(
            "vendored dataset %s v%s loaded: %d records (%s)",
            self._manifest.name,
            self._manifest.version,
            len(self._records),
            self._manifest.licence,
        )
        return self._records

    async def retrieve(self, query: str, filters: dict[str, Any]) -> list[EvidenceChunk]:
        """Return chunks for records whose text contains `query`, best first.

        Substring matching, since this is a table of short labelled records; a shorter matching
        record ranks first as the closest to exact. Every chunk cites `vendored:<dataset>:<row>`,
        the row in the pinned file, not a note id.
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
                # Every match scores alike; ordering is carried by list position, which fusion
                # reads.
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
