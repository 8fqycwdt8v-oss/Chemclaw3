"""The ingest rejection ledger: what was refused, why, and since when.

Lets the system answer "why is there no such record" for an entry a source offered and this system
refused (`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask`).

- A rejection is a statement about data, never a result: `IngestRejection` has no field a record
  has, and its `kind` says what it is. A reader must render the record as absent.
- Keyed on `(source, entry_id)`, so a repeat refusal moves `last_seen` and `occurrences` rather than
  adding rows. At most `_MAX_ROWS_PER_SOURCE` rows per source survive; each write evicts the least
  recently refused. A refusal is withdrawn when a later run stores the entry.
- A ledger write never fails an ingest: writers log and return. One unstorable row costs only itself
  (values are sanitised, and a failed batch falls back to row-at-a-time), except when the database
  is unreachable, where retrying per row cannot help. The reader raises instead, so an unreachable
  ledger never looks like a clean corpus.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

# How many refused records one source may keep. A module constant, not a setting: it bounds the
# table rather than being a deployment choice. A source refusing more has a systematic defect the
# newest rows describe.
_MAX_ROWS_PER_SOURCE = 1000

# How much of a refusal message is kept; a `ValidationError`'s first lines carry the input and rule.
# Truncation is marked.
_MAX_REASON_CHARS = 500

# How many rows one question may return, and how many of its words are matched: both are prompt
# budget, since this rides inside a tool result.
_MAX_MATCHES = 5
_MAX_QUERY_TOKENS = 12

# A word worth matching: it carries a digit (`119`, `Y36`) or is long enough not to be a filler
# word; this replaces a stopword list.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-]*")
_MIN_WORD_CHARS = 5

_UPSERT = """
INSERT INTO ingest_rejections (source, entry_id, reason)
VALUES (%(source)s, %(entry_id)s, %(reason)s)
ON CONFLICT (source, entry_id) DO UPDATE SET
    -- The newest reason wins: a source that changed how it is broken has changed what the answer
    -- to "why was this refused" is, and the row is about the record rather than about one run.
    reason = EXCLUDED.reason,
    last_seen = now(),
    occurrences = ingest_rejections.occurrences + 1
"""

# Keep the `cap` most recently refused rows of this source; delete the rest. `entry_id` breaks ties
# (a batch shares one timestamp). `RETURNING` so evictions are counted: a source with many distinct
# one-off refusals loses its oldest rows, which then read as never refused.
_EVICT = """
DELETE FROM ingest_rejections
WHERE source = %(source)s
  AND entry_id NOT IN (
      SELECT entry_id FROM ingest_rejections
      WHERE source = %(source)s
      ORDER BY last_seen DESC, entry_id
      LIMIT %(cap)s
  )
RETURNING entry_id
"""

# A refusal is withdrawn by the record arriving. Run in the drain's own activity, after the chunk
# has stored what it ingested — see `forget_refusals`.
_FORGET = """
DELETE FROM ingest_rejections
WHERE source = %(source)s AND entry_id = ANY(%(entry_ids)s)
"""

# `count(*) OVER ()` returns the total in the same scan and snapshot as the rows.
#
# A refusal is not served once the same source stored the same entry id at or after it, covering
# rows `forget_refusals` never reached (a source may never re-offer the entry). A record older than
# the refusal does not supersede it: a broken amendment is refused while the earlier transcription
# stays.
_SELECT_MATCHING = """
SELECT r.source, r.entry_id, r.reason, r.first_seen, r.last_seen, r.occurrences,
       count(*) OVER () AS matching
FROM ingest_rejections r
WHERE lower(r.entry_id || ' ' || r.reason) LIKE ANY(%(patterns)s)
  AND NOT EXISTS (
      SELECT 1 FROM reaction_records x
      WHERE x.ingest_source = r.source
        AND x.reaction_id = r.entry_id
        AND x.last_seen >= r.last_seen
  )
ORDER BY r.last_seen DESC
LIMIT %(limit)s
"""


class IngestRejection(BaseModel):
    """One record an ingest source offered and this system refused — **not** a reaction record.

    Frozen, and shaped so it cannot be mistaken for a result. There is no yield here, no structure,
    no conditions and no body: the only chemistry-shaped thing a row carries is the refusal's own
    words, and `kind` names what the object is in the repr the model actually receives (a pydantic
    tool return reaches it as `repr`, per `tests/test_upstream_surface.py`). Anything reading one
    is reading a statement about data that is **absent** from the corpus.
    """

    model_config = ConfigDict(frozen=True)

    # A literal field so the discriminator is the first thing in the rendered repr the model sees.
    kind: Literal["ingest-rejection"] = "ingest-rejection"
    source: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    first_seen: datetime
    last_seen: datetime
    occurrences: int = Field(ge=1)


class RefusalMatches(BaseModel):
    """The refusals one question matched, **and how many it matched**.

    `_MAX_MATCHES` is 5 and the bound is argued rather than accidental — "both are prompt budget:
    this rides inside a tool result the model reads on the turn, and a data-quality footnote that
    outgrows the evidence it accompanies has stopped being a footnote". What was missing is that a
    caller could not tell: "the refusals" and "the top 5 refusals" were the same list.

    That is the same swallowing this module's own header refuses one category over — "swallowing it
    here would make 'nothing was refused' and 'nothing could be asked' the same empty list, which
    is the one thing this module must not do". A cut list with nothing saying so is that sentence
    applied to the other end of the answer.
    """

    model_config = ConfigDict(frozen=True)

    rejections: list[IngestRejection] = Field(default_factory=list)
    # How many rows matched before `_MAX_MATCHES` cut them, from the same scan as the rows.
    total_matching: int = Field(default=0, ge=0)

    @property
    def truncated(self) -> bool:
        """Whether refusals matched that this answer does not carry."""
        return self.total_matching > len(self.rejections)


async def record_refusals(source: str, refusals: Mapping[str, str]) -> None:
    """Record that `source` offered these entries and each was refused for the given reason.

    Keyed by entry id; a repeat across runs moves `last_seen`. An empty mapping touches no database.
    Never raises: a failure is logged. A batch that fails on data is retried row by row; a batch
    that fails because the database is unreachable is not retried, since each row would pay a full
    connect timeout inside the sync activity's deadline.
    """
    if not refusals:
        return
    rows = [
        {"source": source, "entry_id": _storable(entry_id), "reason": _storable(_truncated(reason))}
        for entry_id, reason in refusals.items()
    ]
    try:
        await _write(source, rows)
        return
    # `ConnectionError` is `core.db`'s "there is no database"; a per-row retry cannot improve on it.
    except ConnectionError as exc:
        logger.warning(
            "could not record %d ingest rejection(s) for source %r: the database is unreachable "
            "(%s). Not retried one at a time — that dials an absent server once per row, at the "
            "connect timeout each, inside the sync activity's own budget",
            len(rows),
            source,
            exc,
        )
        return
    # Broad on purpose: nothing here may cost the corpus an entry. `BaseException` (cancellation)
    # still propagates.
    except Exception as exc:
        # `%r` on the source, `%s` on the exception: the first is external text and repr escapes
        # the control characters that would otherwise let an export forge a log line.
        logger.warning(
            "could not record %d ingest rejection(s) for source %r in one batch (%s); retrying "
            "them one at a time",
            len(rows),
            source,
            exc,
        )
    await _write_one_at_a_time(source, rows)


async def forget_refusals(source: str, entry_ids: Sequence[str]) -> None:
    """Withdraw the ledger rows of entries `source` has now ingested.

    A refusal describes an absent record, so the record arriving ends the row. Never raises; a
    missed row is still not served, because `refusals_matching` treats a later stored record as
    superseding it.
    """
    if not entry_ids:
        return
    params = {"source": source, "entry_ids": [_storable(entry_id) for entry_id in entry_ids]}
    try:
        async with db.connection(
            settings.postgres_dsn, operation="ingest_rejections.forget"
        ) as conn:
            await conn.execute(_FORGET, params)
            await conn.commit()
    except Exception as exc:
        logger.warning(
            "could not withdraw the ingest rejections of %d entr(y/ies) source %r has now "
            "ingested (%s); the reader still treats them as superseded",
            len(entry_ids),
            source,
            exc,
        )


async def _evict(cursor: psycopg.AsyncCursor[TupleRow], source: str) -> int:
    """Apply this source's growth bound, reporting what it deleted, on `cursor`'s transaction.

    Shared by both write paths so the reporting cannot diverge. Evicted rows read afterwards as
    never refused, so each eviction logs a WARNING and increments
    `chemclaw_ingest_rejections_evicted_total{source}`; zero is the healthy state.

    Returns:
        How many rows the bound deleted; 0 when the source is inside it, which is the ordinary case.
    """
    await cursor.execute(_EVICT, {"source": source, "cap": _MAX_ROWS_PER_SOURCE})
    evicted = await cursor.fetchall()
    if evicted:
        logger.warning(
            "evicted %d ingest rejection(s) for source %r: the ledger keeps the %d most recently "
            "refused rows per source, and these are older than all of them. Their refusals are now "
            "unanswerable — a question about one of those records will report no refusal at all",
            len(evicted),
            source,
            _MAX_ROWS_PER_SOURCE,
        )
        METRICS.increment(
            "chemclaw_ingest_rejections_evicted_total",
            len(evicted),
            {"source": source},
        )
    return len(evicted)


async def _write(source: str, rows: list[dict[str, str]]) -> None:
    """Upsert these ledger rows and re-apply the source's growth bound, in one transaction.

    Raises whatever the database raises; `record_refusals` decides whether a per-row retry can help.
    """
    async with db.connection(settings.postgres_dsn, operation="ingest_rejections.record") as conn:
        async with conn.cursor() as cur:
            # `executemany` pipelines the batch instead of a round trip per refused record.
            await cur.executemany(_UPSERT, rows)
            # Once per batch rather than once per row: the bound is on what the table holds,
            # and every row of this batch is newer than everything it would evict.
            await _evict(cur, source)
        await conn.commit()


async def _write_one_at_a_time(source: str, rows: list[dict[str, str]]) -> None:
    """Upsert each row in its own transaction, over one connection, then bound the table once.

    One row the database refuses (e.g. an over-long key) must not cost the batch, and these records
    are already gone from the corpus, so this row is the last answer. Isolation is per transaction;
    one shared connection avoids a handshake per row. Never raises.
    """
    try:
        async with db.connection(
            settings.postgres_dsn, operation="ingest_rejections.record"
        ) as conn:
            for row in rows:
                try:
                    async with conn.cursor() as cur:
                        await cur.execute(_UPSERT, row)
                    await conn.commit()
                except Exception as exc:
                    # The failed statement aborted this transaction; without the rollback every
                    # remaining row fails on it instead of on its own merits.
                    await conn.rollback()
                    logger.warning(
                        "could not record the ingest rejection of entry %r for source %r: %s",
                        row["entry_id"],
                        source,
                        exc,
                    )
            async with conn.cursor() as cur:
                await _evict(cur, source)
            await conn.commit()
    except Exception as exc:
        logger.warning(
            "could not record %d ingest rejection(s) for source %r one at a time (%s)",
            len(rows),
            source,
            exc,
        )


async def refusals_matching(question: str) -> RefusalMatches:
    """The refused records whose id or reason matches a word of `question`, newest first.

    Substring matching on the question's distinctive words, because a chemist asks about the value
    that caused the refusal (`119` must reach `input_value=119.43`), which tokenised full-text
    search would miss. Loose on purpose: a stray match costs one irrelevant line, a miss hides the
    answer. Raises on database failure so "nothing refused" and "could not ask" stay distinct.
    Returned text is not neutralised; the agent-facing caller frames it.

    Returns:
        The matching rows, at most `_MAX_MATCHES` of them, and `total_matching` saying how many
        there were, so a truncated answer says so.
    """
    patterns = _patterns(question)
    if not patterns:
        return RefusalMatches()
    async with db.connection(settings.postgres_dsn, operation="ingest_rejections.matching") as conn:
        cursor = await conn.execute(_SELECT_MATCHING, {"patterns": patterns, "limit": _MAX_MATCHES})
        rows = await cursor.fetchall()
    return RefusalMatches(
        rejections=[
            IngestRejection(
                source=row[0],
                entry_id=row[1],
                reason=row[2],
                first_seen=row[3],
                last_seen=row[4],
                occurrences=row[5],
            )
            for row in rows
        ],
        # The window function is per row and identical across them, so the first row carries it;
        # no rows means nothing matched, which is a total of zero either way.
        total_matching=int(rows[0][6]) if rows else 0,
    )


def _patterns(question: str) -> list[str]:
    """The `LIKE` patterns one question contributes, deduplicated and bounded.

    Each word is escaped, since `_` is a `LIKE` wildcard that `_TOKEN` admits; `%` and the
    backslash are escaped too in case `_TOKEN` widens.
    """
    seen: dict[str, None] = {}
    for word in _TOKEN.findall(question.lower()):
        if len(word) < _MIN_WORD_CHARS and not any(char.isdigit() for char in word):
            continue
        escaped = word.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        seen.setdefault(f"%{escaped}%")
        if len(seen) == _MAX_QUERY_TOKENS:
            break
    return list(seen)


def _truncated(reason: str) -> str:
    """The refusal message, cut to `_MAX_REASON_CHARS` with the cut marked."""
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[:_MAX_REASON_CHARS] + " … (message truncated)"


def _storable(text: str) -> str:
    r"""The text with the two things a UTF-8 database cannot hold taken out of it.

    NUL bytes (refused by Postgres) and lone surrogates (refused by psycopg) arrive in ordinary
    external data, including `str(exc)` and `entry_id`. A record carrying them is refused elsewhere,
    but a rejection is the last answer left, so it keeps as much as can be stored, key included.
    """
    # `errors="replace"` rather than `"ignore"`: a lone surrogate becomes a visible `?` in the
    # stored reason, so a reader sees that something was there rather than a seamless gap.
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
