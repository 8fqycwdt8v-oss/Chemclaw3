"""The ingest rejection ledger: what was refused, why, and since when.

A record an ingest source offers and this system refuses used to leave a WARNING and nothing
queryable. The seeded corpus has exactly one such entry — a well logged at 119.43% yield, refused
because `OrdReaction` bounds a yield at 100 — and a chemist asking about it could only be told "I
have no such record". That is true of the corpus and false of what the system knows: the entry was
seen, it was refused, and the reason names the defect
(`D-2026-08-27-a-refused-record-is-a-question-somebody-will-ask`).

**A rejection is a statement about data, never a result.** Nothing here carries a yield, a
structure, conditions or a body, and `IngestRejection` is deliberately shaped so it cannot be read
as a reaction record: it has no field a record has, and its `kind` says what it is in the payload
the model sees. A reader that renders one must say the record is *absent*.

**A ledger, not a second log.** The key is `(source, entry_id)`, so a record refused on every run
is one row with a moving `last_seen` and a rising `occurrences` rather than a growing trail — which
is what makes "is this still happening, and since when" answerable in one row.

**Growth is bounded here, not by a retention sweep.** At most `_MAX_ROWS_PER_SOURCE` rows survive
per source; a write evicts the least recently refused in the same transaction. The case that would
otherwise write millions of rows is a corpus with one systematically broken field, and it is
exactly the case where the newest refusals are the informative ones — an aged-out row is a defect
nothing has re-offered since. `infra/sql/065_ingest_rejections.sql` states the same bound beside
the table, and the runtime role's DELETE grant exists for this eviction alone.

**A ledger write never fails an ingest.** Recording that a record was refused is a side record
about the run; a database that cannot take it must not also cost the corpus the entries that
mapped cleanly, so `record_refusals` logs and returns rather than raising. The reader is the
opposite half of the same rule — it reports that it could not be asked instead of returning an
empty list, because an unreachable ledger and a clean corpus must not render alike.

**And one row the database will not take costs only itself.** That rule is not implied by the one
above and used to be contradicted by it: the whole batch was a single `executemany` in a single
transaction wrapped in a single `except`, so one unstorable character discarded every refusal of
the chunk and logged it as one warning. The values are sanitised before the write (`_storable`)
and the write falls back to one row at a time, because these records are already gone from the
corpus and the cursor has advanced past them — this row is the last answer there is.

**The fallback is for a row, and only a row.** A batch failing because the database is not there
looks identical at the `except` and is the one case retrying cannot improve: every row is refused
again, each paying a full connect timeout. That distinction is `record_refusals`' first handler,
and it is what keeps a ledger write from outlasting the sync activity that is making it.
"""

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)

# How many refused records one source may keep. Deliberately a module constant rather than a
# `Settings` field, for the reason `adapter._LATE_ARRIVAL_NAMES_LOGGED` is one: `core/config/` is
# the operator-facing deployment surface, and how much of a data-quality trail is worth keeping is
# not a deployment decision anybody tunes — it is the bound that keeps this table from becoming the
# log it replaces. 1,000 per source is ~1 MB at the reason cap below, and a source refusing more
# than that has a systematic defect the newest thousand rows describe as well as a million would.
_MAX_ROWS_PER_SOURCE = 1000

# How much of a refusal message is kept. A pydantic `ValidationError` renders the offending input
# and its rule in the first line or two; the rest is a URL and repeated context. Truncation is
# marked, so a reader never mistakes a cut message for the whole of one.
_MAX_REASON_CHARS = 500

# How many rows one question may pull back, and how many of its words are matched. Both are prompt
# budget: this rides inside a tool result the model reads on the turn, and a data-quality footnote
# that outgrows the evidence it accompanies has stopped being a footnote.
_MAX_MATCHES = 5
_MAX_QUERY_TOKENS = 12

# A word worth matching a refusal on: it carries a digit (`119`, `Y36`, a batch number) or it is
# long enough not to be a preposition. Short all-letter words — "our", "any", "the" — match
# everything and mean nothing, and this rule replaces a stopword list nobody would maintain.
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

# Keep the `cap` most recently refused rows of this source; delete the rest. Ordered by
# `entry_id` after `last_seen` so a tie — every row of one batch shares a transaction timestamp —
# resolves deterministically instead of leaving the cap to the physical row order.
_EVICT = """
DELETE FROM ingest_rejections
WHERE source = %(source)s
  AND entry_id NOT IN (
      SELECT entry_id FROM ingest_rejections
      WHERE source = %(source)s
      ORDER BY last_seen DESC, entry_id
      LIMIT %(cap)s
  )
"""

_SELECT_MATCHING = """
SELECT source, entry_id, reason, first_seen, last_seen, occurrences
FROM ingest_rejections
WHERE lower(entry_id || ' ' || reason) LIKE ANY(%(patterns)s)
ORDER BY last_seen DESC
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

    # A literal, not a comment: it is the first thing in the rendered repr, so the discriminator
    # travels with the object into the prompt rather than living in a docstring the model never
    # sees.
    kind: Literal["ingest-rejection"] = "ingest-rejection"
    source: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    first_seen: datetime
    last_seen: datetime
    occurrences: int = Field(ge=1)


async def record_refusals(source: str, refusals: Mapping[str, str]) -> None:
    """Record that `source` offered these entries and each was refused for the given reason.

    Keyed by entry id, so one call cannot enter the same record twice and a repeat across runs
    moves `last_seen` instead of adding a row. Empty is the ordinary case and touches no database
    at all — a clean fetch must not cost a connection.

    Never raises. A refusal that cannot be recorded is logged with the reason it could not be, for
    the rule this module's docstring states: the ledger is a side record about the run, and losing
    it must not also lose the entries that mapped cleanly. A batch that fails on *data* is retried
    row by row, so what is lost is the row the database refused rather than every row beside it.

    **A batch that fails because there is no database is not retried at all.** The two arrive as
    one exception from `_write` and they are not the same fault: a poison row will be refused
    again by a server that is answering, while an absent server refuses *every* row, each at the
    full `pg_connect_timeout_seconds`. Measured at the shipped defaults, three refusals cost
    40.2 s that way — 10 s for the batch and 10 s per row — so at `eln_sync_batch_size = 100` the
    ledger write alone was ~1,010 s inside an activity whose start-to-close is 300. The mechanism
    written so that one bad row does not cost the batch was costing the whole sync.
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
    # `ConnectionError` is this system's published "there is no database" (`core.db`), so it is the
    # one failure the row-by-row retry cannot improve on: the retry re-attempts the *connection*,
    # not the row.
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
    # `Exception`, not a list of database errors: the rule this module states is that *nothing*
    # here may cost the corpus an entry, and a list of types is a list somebody has to keep right.
    # `BaseException` stays uncaught, so a cancelled activity is still a cancelled activity.
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


async def _write(source: str, rows: list[dict[str, str]]) -> None:
    """Upsert these ledger rows and re-apply the source's growth bound, in one transaction.

    Raises whatever the database raises — the swallowing rule belongs to `record_refusals`, which
    reads the failure to decide whether retrying row by row can help at all.
    """
    async with db.connection(settings.postgres_dsn, operation="ingest_rejections.record") as conn:
        async with conn.cursor() as cur:
            # `executemany`, not a loop of `execute`: psycopg pipelines the batch, where the loop
            # paid one round trip per refused record. A source with a systematically broken field
            # offers hundreds per chunk, and every one of them was a round trip inside the sync
            # activity's own start-to-close window.
            await cur.executemany(_UPSERT, rows)
            # Once per batch rather than once per row: the bound is on what the table holds,
            # and every row of this batch is newer than everything it would evict.
            await cur.execute(_EVICT, {"source": source, "cap": _MAX_ROWS_PER_SOURCE})
        await conn.commit()


async def _write_one_at_a_time(source: str, rows: list[dict[str, str]]) -> None:
    """Upsert each row in its own transaction, over **one** connection, then bound the table once.

    **One bad row may not cost the batch**, which is what a single `executemany` in a single
    transaction made it do. `_storable` knows the two ways a value reaches here unwritable; the
    database knows more — an entry id past the primary key's index-row limit is one, and no
    rewriting of it would leave it the same id. So this is the isolation
    `ingest/documents/sync.py::_reembed_individually` and `ingest/labels/enrich.py::_batch` already
    use for the same reason, and it matters more here than in either: these records are already
    gone from the corpus and the cursor has advanced past them, so this row is the last answer to
    "why is there no such record".

    **One connection, not one per row.** A borrowed connection per row made the cost of this path
    a connect handshake per refusal — and, against a database that had just stopped answering, a
    full connect timeout per refusal. The isolation the fallback needs is per *transaction*, which
    is what the rollback below gives; the connection is shared because the rows have nothing to
    isolate from each other. The eviction runs once at the end rather than per row, for the reason
    `_write` runs it once: the bound is on what the table holds.

    Never raises, on the same rule `record_refusals` follows — its own connection failing is the
    same fact the batch's failure already was, and it is reported rather than re-thrown.
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
                await cur.execute(_EVICT, {"source": source, "cap": _MAX_ROWS_PER_SOURCE})
            await conn.commit()
    except Exception as exc:
        logger.warning(
            "could not record %d ingest rejection(s) for source %r one at a time (%s)",
            len(rows),
            source,
            exc,
        )


async def refusals_matching(question: str) -> list[IngestRejection]:
    """The refused records whose id or reason matches a word of `question`, newest first.

    Substring matching on the question's own distinctive words, because the thing a chemist asks
    about is the value that caused the refusal: "logged at 119% yield" has to reach a reason
    reading `input_value=119.43`, which no tokenised full-text match makes — `119` and `119.43` are
    different lexemes, and the same is true of an entry id and the plate it names. A word qualifies
    if it carries a digit or is at least `_MIN_WORD_CHARS` long (see `_TOKEN`).

    The stance on false positives is deliberate: a loose match may surface a refusal the question
    was not about, and the shape of what comes back says plainly what it is, so the cost is a
    reader seeing one irrelevant "this record was refused" line. The opposite error — the ledger
    holding the answer and the match being too strict to find it — is the failure this whole ledger
    exists to end.

    Raises whatever the database raises. The caller decides what an unreachable ledger means for
    its answer; swallowing it here would make "nothing was refused" and "nothing could be asked"
    the same empty list, which is the one thing this module must not do.

    **What comes back is unneutralised, deliberately.** `reason` is external text and `entry_id` is
    the id an export chose; the agent-facing caller frames the first and defangs the other two
    (`agent/research_tools.py::_refused_on_ingest`), for the reason `agent/memory_tools.py` keeps
    an observation's `statement` plain in its store and frames it in the tool: the envelope belongs
    to the one channel that feeds a model, and a row rewritten here would also be rewritten for the
    operator reading the table.
    """
    patterns = _patterns(question)
    if not patterns:
        return []
    async with db.connection(settings.postgres_dsn, operation="ingest_rejections.matching") as conn:
        cursor = await conn.execute(_SELECT_MATCHING, {"patterns": patterns, "limit": _MAX_MATCHES})
        rows = await cursor.fetchall()
    return [
        IngestRejection(
            source=row[0],
            entry_id=row[1],
            reason=row[2],
            first_seen=row[3],
            last_seen=row[4],
            occurrences=row[5],
        )
        for row in rows
    ]


def _patterns(question: str) -> list[str]:
    """The `LIKE` patterns one question contributes, deduplicated and bounded.

    Each word is escaped before it becomes a pattern. `_` is the one that bites today: `_TOKEN`
    admits it, it is a `LIKE` wildcard matching any single character, and a chemist quoting
    `yield_percent` would otherwise match every row whose reason merely mentions a yield. A percent
    sign and a backslash cannot come out of `_TOKEN`'s character class at all; the escape is
    written whole anyway, so widening that class later cannot quietly turn a word into a wildcard.
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
    """The refusal message, cut to `_MAX_REASON_CHARS` with the cut marked.

    Marked rather than silent, the same rule the sweep's `truncated_by` follows: a message cut
    without saying so reads as the whole of what the refusal said.
    """
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[:_MAX_REASON_CHARS] + " … (message truncated)"


def _storable(text: str) -> str:
    r"""The text with the two things a UTF-8 database cannot hold taken out of it.

    Both arrive here as ordinary external data rather than as edge cases. A NUL byte anywhere in an
    ELN's free text reaches this module inside `str(exc)` — a `ValidationError` renders the
    offending `input_value=` verbatim — and Postgres refuses a NUL in a `text` value outright. A
    lone surrogate arrives the same way from a JSON export with a truncated `\\u` escape, and
    psycopg refuses it one step earlier, when it encodes the parameter. `entry_id` is subject to
    both: it is whatever the source keys its rows on, validated `min_length=1` and nothing more.

    **This module sanitises where `ingest/eln/records.py` refuses, and the asymmetry is the point.**
    A record carrying a byte the corpus cannot store is refused, because a transcription is what
    the source said and quietly deleting a chemist's characters is the mistake
    `record._without_wikilinks` names. A *rejection* is the record of that refusal, and it is the
    last thing standing between a chemist and "I have no such record": it has nowhere left to
    refuse to, so it keeps as much of the value as the database can hold and drops the rest. That
    trade includes the key — a row filed under the closest spelling that can be stored answers the
    question, and no row answers nothing.
    """
    # `errors="replace"` rather than `"ignore"`: a lone surrogate becomes a visible `?` in the
    # stored reason, so a reader sees that something was there rather than a seamless gap.
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")
