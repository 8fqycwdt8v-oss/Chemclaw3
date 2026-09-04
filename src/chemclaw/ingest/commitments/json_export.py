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
from chemclaw.core.metrics_bridge import degraded
from chemclaw.ingest.commitments.models import Commitment

logger = logging.getLogger(__name__)


class JsonCommitmentExport:
    """Read commitments from a directory of JSON files, or from one file.

    Each file holds either a list of commitment objects or an object with a `commitments` list, so
    both shapes an export tool produces are readable without a per-site flag.
    """

    #: Declared `False` even though `fetch_commitments` does return the whole export every time,
    #: and the two are not the same claim: `snapshot` licenses a *destructive* sweep that deletes
    #: every commitment this pass did not see, so it is the operator's statement that the export is
    #: complete rather than this class's statement that it read all of it. A directory this adapter
    #: is pointed at mid-write, or one an export tool half-filled, returns "everything" and means
    #: nothing. Turning it on for the shipped adapter is a behaviour change and a `BACKLOG.md` row.
    snapshot: bool = False

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
        if not self.path.exists():
            # **Said out loud, because the alternative is a truthful-looking empty portfolio.** A
            # mistyped `CHEMCLAW_COMMITMENT_EXPORT_DIR` or a mount that failed returns no files, the
            # sync reports success with nothing mirrored, `mirror_freshness` stays NULL, and
            # `review_commitments` presents that to a project leader as "nothing was ever mirrored".
            # Creating the shipped default directory does not help a deployment that points the knob
            # somewhere else, which is the only reason the knob exists.
            #
            # Through `degraded()` rather than a bare `logger.warning`, for the reason
            # `deliver/message.py` states about the sibling it was extracted from: a WARNING with no
            # counter is invisible to everything except a person already reading the log of the pod
            # they already suspect. This failure lasts as long as the misconfiguration and its whole
            # symptom is *silence*, so `chemclaw_degraded_total{subsystem="commitment_mirror"}` is
            # the only place it can be seen from outside. `exc_info=False` because this is a
            # configuration fact rather than a caught exception.
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
            # **The same silence, one step later, and the first version of this guard missed it.**
            # A directory that exists and holds nothing this reads — the wrong subdirectory, a mount
            # that came up empty, an export written as `.jsonl` — produces the identical symptom the
            # missing-path branch above was added to end: zero commitments, a successful sync,
            # `mirror_freshness` NULL, and a project leader told the portfolio is empty. The
            # existence check alone covers only the mistyped half of a mistyped knob.
            #
            # Same `commitment_mirror` subsystem as the branch above, deliberately: both mean
            # "nothing was mirrored and the pointer is why", which is one alert and one operator
            # action. The *content* faults below are a different subsystem, so that "found nothing"
            # and "found something and could not read it" are distinguishable from the metric alone.
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
        # **Both totals are counted, not only logged, and for the reason `degraded()` exists.** A
        # `logger.warning` with no series behind it is visible to a person already reading the log
        # of the pod they already suspect — and nobody suspects a mirror that reports success. These
        # are the two ways an export can be *present* and still not become a portfolio: the files
        # would not parse, or the rows would not validate. Neither is a missing knob, so neither
        # belongs on `commitment_mirror`: `commitment_export` is the content's own subsystem, which
        # is what lets an operator read "found nothing because the export is empty" apart from
        # "found nothing because none of it parsed" off the metric rather than off the prose.
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


def json_commitment_export(name: str, path: str = "") -> JsonCommitmentExport:
    """Build a `JsonCommitmentExport` — the `module:callable` a manifest names.

    `path` defaults to the configured `commitment_export_dir` rather than being required in the
    manifest, the same way `eln-json` leaves its directory to `eln_export_dir`: a path in a manifest
    is CWD-relative and a deployment cannot override it without editing a shipped file.
    """
    return JsonCommitmentExport(name=name, path=path or settings.commitment_export_dir)
