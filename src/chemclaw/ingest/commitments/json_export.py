"""A commitments half over a JSON export on disk — the shape a portfolio tool's extract takes.

The counterpart of `ingest/eln/json_adapter.py`, and here for the same two reasons: it is the
integration a site can stand up without a vendor client, and it is what makes the seam provable end
to end with no network. A portfolio system's scheduled extract is a file on a share; this reads it.

**It infers nothing.** Every field is taken as the export stated it, a row missing a required field
is *rejected and counted* rather than repaired, and no date is derived. That is the same discipline
`record_from_ord_reaction` follows, and it is what lets a mirrored row be data rather than a claim
(`D-2026-08-25-an-eln-transcription-is-data-not-a-claim`) — a mirror that guessed a due date would
be asserting a plan, which is the one thing this tier must not do.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.ingest.commitments.models import Commitment

logger = logging.getLogger(__name__)


class JsonCommitmentExport:
    """Read commitments from a directory of JSON files, or from one file.

    Each file holds either a list of commitment objects or an object with a `commitments` list, so
    both shapes an export tool produces are readable without a per-site flag.
    """

    def __init__(self, name: str, path: str) -> None:
        """Bind the source's name and the file or directory its export lands in."""
        self.name = name
        self.path = Path(path)

    async def fetch_commitments(self, since: datetime | None) -> list[Commitment]:
        """Every commitment in the export.

        `since` is accepted and **deliberately ignored**, which the sync is built for: a portfolio
        extract is a snapshot rather than a change feed, and filtering one by a watermark this side
        would drop rows whose state moved without their file being rewritten. The upsert is keyed on
        `(source, external_id)`, so re-reading the whole snapshot converges rather than duplicating.
        """
        files = sorted(self.path.glob("*.json")) if self.path.is_dir() else [self.path]
        found: list[Commitment] = []
        rejected = 0
        unreadable = 0
        for file in files:
            if not file.is_file():
                continue
            # **Reject-and-continue applies to a *file*, not only to a row.** It was written for
            # the row and the file was left to raise: a truncated export (a partial write, a failed
            # extract) or one containing `null` aborted `fetch_commitments` before every file
            # sorting after it was read, the activity failed, the cursor never advanced, and the
            # mirror silently froze on last week's snapshot while `review_commitments` kept
            # answering from it. One bad file must cost that file.
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
                rows = payload if isinstance(payload, list) else payload.get("commitments", [])
                if not isinstance(rows, list):
                    raise TypeError(f"expected a list of commitments, got {type(rows).__name__}")
            except (OSError, ValueError, TypeError, AttributeError) as exc:
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
        if unreadable:
            logger.warning(
                "commitments.files_unreadable: %s skipped %d unreadable file(s) of %d",
                self.name,
                unreadable,
                len(files),
            )
        if rejected:
            logger.warning(
                "commitment_export_rejected: %s rejected %d row(s) that did not validate",
                self.name,
                rejected,
            )
        return found


def json_commitment_export(name: str, path: str = "") -> JsonCommitmentExport:
    """Build a `JsonCommitmentExport` — the `module:callable` a manifest names.

    `path` defaults to the configured `commitment_export_dir` rather than being required in the
    manifest, the same way `eln-json` leaves its directory to `eln_export_dir`: a path in a manifest
    is CWD-relative and a deployment cannot override it without editing a shipped file.
    """
    return JsonCommitmentExport(name=name, path=path or settings.commitment_export_dir)
