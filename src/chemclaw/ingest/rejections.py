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
    it must not also lose the entries that mapped cleanly.
    """
    if not refusals:
        return
    try:
        async with db.connection(
            settings.postgres_dsn, operation="ingest_rejections.record"
        ) as conn:
            async with conn.cursor() as cur:
                for entry_id, reason in refusals.items():
                    await cur.execute(
                        _UPSERT,
                        {
                            "source": source,
                            "entry_id": entry_id,
                            "reason": _truncated(reason),
                        },
                    )
                # Once per batch rather than once per row: the bound is on what the table holds,
                # and every row of this batch is newer than everything it would evict.
                await cur.execute(_EVICT, {"source": source, "cap": _MAX_ROWS_PER_SOURCE})
            await conn.commit()
    # `Exception`, not a list of database errors: the rule this module states is that *nothing*
    # here may cost the corpus an entry, and a list of types is a list somebody has to keep right.
    # `BaseException` stays uncaught, so a cancelled activity is still a cancelled activity.
    except Exception as exc:
        # `%r` on the source, `%s` on the exception: the first is external text and repr escapes
        # the control characters that would otherwise let an export forge a log line.
        logger.warning(
            "could not record %d ingest rejection(s) for source %r: %s",
            len(refusals),
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
