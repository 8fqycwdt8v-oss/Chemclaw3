"""Make a shortened audit trail detectable — including one shortened by a restore.

`agent/audit_store.py` chains every audited row to its predecessor, so modification, reordering,
interior deletion and prefix truncation all break a link. Deleting a *trailing* run does not: the
survivors chain cleanly, and nothing ever recorded how many rows there should have been.
`durable/audit_chain.py` has said so in a "Known limit" paragraph since it was written, and
`DEFERRED.md` held the fix pending a regulated deployment asking for provable tail completeness.

The readiness review turned that from a regulatory question into an operational one, and the
argument is short enough to state in full: **a point-in-time restore is a trailing deletion.** The
system has four unowned stores and no documented recovery, and the moment one is written, the
recovery procedure for Postgres silently shortens the compliance trail in the exact way the chain
was built not to notice. So the anchor is no longer a thing to build when an auditor asks; it is
what makes the backup story safe to have.

**An anchor is signed, and the key is not in the database.** An actor able to delete rows can also
insert a lower anchor, so an unsigned high-water mark defends against accidents and nothing else.
HMAC-SHA256 under `audit_anchor_secret` means forging one needs something a database compromise
alone does not provide.

**An anchor is published out of band, and the table is only the convenient copy.** A PITR rolls
`audit_anchors` back together with `audit_events`, so a control that lived solely in Postgres would
be restored into agreement with the truncated trail it was supposed to catch — the same mistake as
the chain, one level up. Every anchor is therefore also written to the process log at a stable
marker, landing in whatever log store the deployment has; after a restore an operator recovers that
line and hands it to `verify-audit-chain --anchor`. The table catches tampering that did not think
to rewrite anchors; the log line catches the restore.

**Anchoring is off without a secret**, and that is stated rather than defaulted around: with no
`audit_anchor_secret` the trail is exactly as it was — chained, and silent about its own tail.
"""

import hashlib
import hmac
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.core.db import connection

logger = logging.getLogger(__name__)

# The log marker an operator greps for after a restore. Stable and boring on purpose: it is the
# out-of-band copy of the control, so it must survive log-format changes and be findable by someone
# who has never read this module (`docs/guides/runbook.md` §(xiii) quotes it).
ANCHOR_LOG_MARKER = "audit_chain_anchor"

# The fields the signature covers, in the order it covers them. Explicit rather than "the model's
# fields", because a field added later must not silently change what every existing signature
# meant — the same reasoning `audit_store._CHAINED_FIELDS` uses, and the reason the chain is
# versioned at all.
_SIGNED_FIELDS = ("taken_at", "row_count", "max_event_id", "tip_hash", "chain_version")

# Over the *chained* rows only, and the filter is load-bearing rather than defensive. A deployment
# that predates `infra/sql/011` carries a leading run with an empty `row_hash`, which the verifier
# skips as pre-chain — so a `count(*)` over the whole table would anchor a number the verifier can
# never reproduce, and every run would report a phantom gap the size of that legacy run.
_TAKE = """
    SELECT count(*), coalesce(max(id), 0)
    FROM audit_events
    WHERE row_hash <> ''
"""
# The tip's own hash and version, read separately from the aggregate above so an empty trail is one
# clean "no row" rather than a null-riddled aggregate row.
_TIP = """
    SELECT row_hash, chain_version
    FROM audit_events
    WHERE row_hash <> ''
    ORDER BY id DESC
    LIMIT 1
"""
_INSERT = """
    INSERT INTO audit_anchors
        (taken_at, row_count, max_event_id, tip_hash, chain_version, signature,
         reseal_reason, reseal_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
# The newest *few*, not the newest one. Reading a single row made an appended junk anchor a way to
# disable the control: `latest_anchor` returned None, `durable/audit_chain.py` set `held_to = None`
# and skipped its compare entirely, so a trail truncated to any length verified clean. That needs
# no forgery — only INSERT on `audit_anchors`, or a half-finished secret rotation — and it is
# strictly less work than the tampering the module says the table still catches.
#
# Bounded rather than unbounded: an attacker who can insert can insert many, and scanning the whole
# table to find one valid anchor would trade a silent failure for a slow one. Past this many
# consecutive invalid anchors the control reports absent, loudly, which is the honest answer.
_LATEST_CANDIDATES = 16

_LATEST = """
    SELECT taken_at, row_count, max_event_id, tip_hash, chain_version, signature
    FROM audit_anchors
    ORDER BY taken_at DESC, id DESC
    LIMIT %(limit)s
"""


class Anchor(BaseModel):
    """What the audit trail held at one moment, and the signature that makes it evidence."""

    taken_at: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    max_event_id: int = Field(ge=0)
    tip_hash: str = ""
    chain_version: int = 0
    signature: str = ""

    def payload(self) -> str:
        """The canonical string the signature covers.

        Sorted, separator-fixed JSON so the same anchor signs identically in any process — a
        signature over a dict whose serialization can vary is a signature that fails at random.
        """
        return json.dumps(
            {name: getattr(self, name) for name in _SIGNED_FIELDS},
            sort_keys=True,
            separators=(",", ":"),
        )


def sign(anchor: Anchor) -> str:
    """The HMAC-SHA256 of `anchor`'s payload under the configured secret."""
    return hmac.new(
        settings.audit_anchor_secret.encode(), anchor.payload().encode(), hashlib.sha256
    ).hexdigest()


def signature_ok(anchor: Anchor) -> bool:
    """Whether `anchor` carries a signature this deployment's secret produces.

    `compare_digest`, not `==`: this is a MAC comparison, and a short-circuiting one leaks the
    prefix length an attacker got right.
    """
    if not settings.audit_anchor_secret or not anchor.signature:
        return False
    return hmac.compare_digest(sign(anchor), anchor.signature)


def parse_anchor(text: str) -> Anchor:
    """Read an anchor from the JSON an operator recovered from the logs.

    Deliberately lenient about surrounding text: the value is copied out of a log line, so a caller
    who pastes the whole line rather than just the object should get their anchor and not a lesson
    in JSON. Anything else raises, because a silently empty anchor would verify everything.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object found in the supplied anchor: {text[:80]!r}")
    return Anchor.model_validate_json(text[start : end + 1])


def compare(anchor: Anchor, *, row_count: int, max_event_id: int, tip_hash: str) -> list[str]:
    """Problems found comparing an observed trail against `anchor` (empty means consistent).

    Three separate comparisons because they fail differently and an operator needs to know which:

    - **Fewer rows** is the truncation this whole module exists for — a restore, or a deletion.
    - **A lower max id** means rows past the anchor are gone. Distinct from the count, because
      deleting old rows while appending new ones keeps the count and moves neither back the same
      way; reporting one number would let that pass.
    - **A different tip** at the same height means the trail was rebuilt rather than shortened, and
      is the case a count-only anchor misses entirely.

    A *longer* trail is not a problem: the anchor is a high-water mark, and appending is what the
    trail is for.
    """
    problems: list[str] = []
    if row_count < anchor.row_count:
        problems.append(
            f"audit trail is short: {row_count} rows against an anchor of {anchor.row_count} taken "
            f"at {anchor.taken_at} — {anchor.row_count - row_count} record(s) are missing from the "
            "tail (a restore, or a deletion)"
        )
    if max_event_id < anchor.max_event_id:
        problems.append(
            f"audit trail lost its most recent rows: highest id {max_event_id} against an anchored "
            f"{anchor.max_event_id} at {anchor.taken_at}"
        )
    if row_count == anchor.row_count and anchor.tip_hash and tip_hash != anchor.tip_hash:
        problems.append(
            f"audit trail tip does not match the anchor taken at {anchor.taken_at}: the trail is "
            "the anchored length but not the anchored content (it was rebuilt, not shortened)"
        )
    return problems


async def take_anchor(
    dsn: str | None = None, *, reseal_reason: str = "", reseal_by: str = ""
) -> Anchor | None:
    """Record and publish an anchor over the current trail; None when anchoring is not configured.

    Called after a *successful* verification, never before: anchoring a trail whose chain has not
    been checked would sign whatever damage is already there and make it the new baseline.

    `reseal_reason`/`reseal_by` are set only by a deliberate re-anchor after a recovery. They are
    stored rather than merely allowed, because the GxP position is that a trail may be shortened by
    a legitimate restore and may never pretend it was not.
    """
    if not settings.audit_anchor_secret:
        return None
    target = dsn if dsn is not None else settings.postgres_dsn
    async with connection(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_TAKE)
        totals = await cursor.fetchone()
        cursor = await conn.execute(_TIP)
        tip = await cursor.fetchone()
        anchor = Anchor(
            taken_at=await _now(conn),
            row_count=int(str(totals[0])) if totals else 0,
            max_event_id=int(str(totals[1])) if totals else 0,
            tip_hash=str(tip[0]) if tip else "",
            chain_version=int(str(tip[1])) if tip else 0,
        )
        anchor = anchor.model_copy(update={"signature": sign(anchor)})
        await conn.execute(
            _INSERT,
            (
                anchor.taken_at,
                anchor.row_count,
                anchor.max_event_id,
                anchor.tip_hash,
                anchor.chain_version,
                anchor.signature,
                reseal_reason,
                reseal_by,
            ),
        )
        await conn.commit()
    # The out-of-band copy. Logged at ERROR-adjacent prominence would be wrong (nothing is wrong),
    # but it must be at a level a deployment actually ships, so INFO with a stable marker.
    logger.info("%s=%s", ANCHOR_LOG_MARKER, anchor.model_dump_json())
    return anchor


async def _now(conn: Any) -> str:
    """The database's clock as an ISO string, so anchors from different pods order consistently.

    The server's `now()` rather than the pod's, for the same reason the audit rows use it: a skewed
    replica must not be able to write an anchor that sorts ahead of a later one.
    """
    cursor = await conn.execute("SELECT now()")
    row = await cursor.fetchone()
    return str(row[0])


async def latest_anchor(dsn: str | None = None) -> Anchor | None:
    """The newest anchor recorded in the database, or None if there is none or none is valid.

    An anchor whose signature does not verify is skipped and logged, and the search continues to
    the next-newest. Skipping an invalid anchor is not treated as a *failure*, because the ordinary
    cause is a rotated secret rather than an attack — and refusing to verify the chain at all
    because an old anchor no longer validates would take the whole control offline for a rotation.

    **Skipping past an invalid one is not the same as stopping at it**, and the first version did
    the latter: it read exactly one row and returned None when that row failed. So appending a
    single unsigned anchor — no forgery, just an INSERT — made `latest_anchor` return None, which
    made `durable/audit_chain.py` set `held_to = None` and skip its comparison, which made a trail
    truncated to any length verify clean. One junk row disabled the high-water mark the table exists
    to be.
    """
    if not settings.audit_anchor_secret:
        return None
    target = dsn if dsn is not None else settings.postgres_dsn
    async with connection(
        target, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        cursor = await conn.execute(_LATEST, {"limit": _LATEST_CANDIDATES})
        rows = await cursor.fetchall()
    skipped = 0
    for row in rows:
        anchor = Anchor(
            taken_at=str(row[0]),
            row_count=int(str(row[1])),
            max_event_id=int(str(row[2])),
            tip_hash=str(row[3]),
            chain_version=int(str(row[4])),
            signature=str(row[5]),
        )
        if signature_ok(anchor):
            if skipped:
                logger.warning(
                    "skipped %d newer audit anchor(s) whose signatures did not verify before "
                    "reaching a valid one (taken at %s)",
                    skipped,
                    anchor.taken_at,
                )
            return anchor
        skipped += 1
        logger.warning(
            "ignoring an audit anchor (taken at %s): its signature does not verify under the "
            "configured CHEMCLAW_AUDIT_ANCHOR_SECRET — most often a rotated key",
            anchor.taken_at,
        )
    return None
