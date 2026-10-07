"""A commitments half over a JSON export on disk — the shape a portfolio tool's extract takes.

Needs no vendor client and makes the seam testable end to end with no network. It infers
nothing: fields are taken as the export states them, an invalid row is rejected and counted
rather than repaired, and no date is derived, so a mirrored row is data rather than a claim.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.ingest.commitments.models import Commitment

logger = logging.getLogger(__name__)


class JsonCommitmentExport:
    """Read commitments from a directory of JSON files, or from one file.

    Each file holds either a list of commitment objects or an object with a `commitments` list.
    """

    #: The protocol default; the instance attribute set in `__init__` is what answers.
    #:
    #: `False` even though every call reads the whole export: `snapshot` licenses a destructive
    #: sweep, so it is the operator's statement (in the site's `datasource.yaml`) that the export
    #: is written completely and atomically, not this class's observation that it read everything.
    snapshot: bool = False

    def __init__(self, name: str, path: str, *, snapshot: bool = False) -> None:
        """Bind the source's name, the file or directory its export lands in, and its completeness.

        Args:
            name: The data source's name, as the manifest declares it.
            path: The file or directory the export lands in.
            snapshot: Whether this export is complete every pass, which licenses the destructive
                sweep in `durable/commitment_sync.py`.
        """
        self.name = name
        self.path = Path(path)
        self.snapshot = snapshot

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        """Every commitment in the export.

        `since` is deliberately ignored: the export is a snapshot, and filtering by a watermark
        would drop rows whose state moved without their file being rewritten. The read runs in a
        thread because the worker's event loop also carries Temporal heartbeats and the health
        endpoints.
        """
        return await asyncio.to_thread(self._read)

    def _read(self) -> list[Commitment]:
        """The whole blocking read, in one synchronous function so one thread can hold it.

        Returns:
            Every commitment the export holds, refusals counted and skipped.
        """
        if not self.path.exists():
            # A missing path otherwise looks like a successful sync of an empty portfolio, so it is
            # counted on `chemclaw_degraded_total{subsystem="commitment_mirror"}` rather than only
            # logged. `exc_info=False` because this is a configuration fact, not a caught exception.
            degraded(
                logger,
                "commitment_mirror",
                "commitments.export_dir_missing: %s reads %s, which does not exist; nothing will "
                "be mirrored and the portfolio will read as empty",
                self.name,
                self.path,
                exc_info=False,
            )
            return []
        files = sorted(self.path.glob("*.json")) if self.path.is_dir() else [self.path]
        if not files:
            # An existing directory with nothing readable in it has the same symptom, so it is the
            # same `commitment_mirror` alert. Content faults below use a separate subsystem so
            # "found nothing" and "found something unreadable" are distinguishable from the metric.
            degraded(
                logger,
                "commitment_mirror",
                "commitments.export_empty: %s read %s and found no *.json file; nothing will be "
                "mirrored and the portfolio will read as empty",
                self.name,
                self.path,
                exc_info=False,
            )
            return []
        found: list[Commitment] = []
        rejected = 0
        unreadable = 0
        for file in files:
            if not file.is_file():
                continue
            # Reject-and-continue applies per file: one truncated or `null` file must not abort the
            # pass and freeze the mirror on the previous snapshot.
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
                rows = payload if isinstance(payload, list) else payload.get("commitments", [])
                if not isinstance(rows, list):
                    raise TypeError(f"expected a list of commitments, got {type(rows).__name__}")
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                RecursionError,
                MemoryError,
            ) as exc:
                logger.warning(
                    "commitments.file_unreadable: %s in %s: %s", self.name, file.name, exc
                )
                unreadable += 1
                continue
            for row in rows:
                try:
                    found.append(Commitment(source=self.name, **row))
                except (ValidationError, TypeError):
                    # Counted and skipped, the reject-and-continue rule the ELN ingest uses: one
                    # malformed row in a thousand-row export must not cost the other 999.
                    rejected += 1
        # Unparseable files and invalid rows are counted under `commitment_export`, the content's
        # own subsystem, since a mirror that reports success is not one anybody reads the log of.
        if unreadable:
            degraded(
                logger,
                "commitment_export",
                "commitments.files_unreadable: %s skipped %d unreadable file(s) of %d; %d "
                "commitment(s) were mirrored from the rest",
                self.name,
                unreadable,
                len(files),
                len(found),
                exc_info=False,
            )
        if rejected:
            degraded(
                logger,
                "commitment_export",
                "commitment_export_rejected: %s rejected %d row(s) that did not validate; %d "
                "commitment(s) were mirrored from the rest",
                self.name,
                rejected,
                len(found),
                exc_info=False,
            )
        return found


def json_commitment_export(
    name: str, path: str = "", *, snapshot: bool = False
) -> JsonCommitmentExport:
    """Build a `JsonCommitmentExport` — the `module:callable` a manifest names.

    `path` defaults to `commitment_export_dir` so a deployment can move it without editing a shipped
    manifest. `snapshot` is not a setting because it describes one site's export tool, so it belongs
    in that source's manifest `config:`.

    Args:
        name: The data source's name, as the manifest declares it.
        path: The export's file or directory; empty means `commitment_export_dir`.
        snapshot: Whether every pass reads a complete export, which licenses the destructive sweep.

    Returns:
        The adapter the commitments half of the source seam calls.
    """
    return JsonCommitmentExport(
        name=name, path=path or settings.commitment_export_dir, snapshot=snapshot
    )
