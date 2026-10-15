"""No batch claim picks its rows with `IN (subselect LIMIT n FOR UPDATE)`.

Postgres may join that subselect with a nested loop that runs it again for every outer row, and each
re-run skips the rows the statement already updated and takes the next `n`, so one claim hands out
the whole queue (`tests/test_publish_outbox.py` forces the plan). A claim with a limit picks its
rows in a `MATERIALIZED` CTE, which is evaluated once under every plan.
"""

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "chemclaw"

# `IN (SELECT ... LIMIT ... FOR UPDATE ...)` with no statement end between the three.
_LIMITED_LOCKING_SUBSELECT = re.compile(
    r"\bIN\s*\(\s*SELECT\b[^;]{0,400}?\bLIMIT\b[^;]{0,200}?\bFOR\s+UPDATE\b",
    re.IGNORECASE | re.DOTALL,
)


def test_the_pattern_recognises_the_claim_shape_that_over_claimed() -> None:
    """The scan is not vacuous: it matches the statement `_CLAIM` used to be."""
    unsafe = """
        UPDATE result_publications SET attempts = attempts + 1
        WHERE id IN (
            SELECT id FROM result_publications WHERE state = 'pending'
            ORDER BY enqueued_at LIMIT %s FOR UPDATE SKIP LOCKED
        ) RETURNING id
    """
    safe = """
        WITH picked AS MATERIALIZED (
            SELECT id FROM result_publications WHERE state = 'pending'
            ORDER BY enqueued_at LIMIT %s FOR UPDATE SKIP LOCKED
        )
        UPDATE result_publications AS p SET attempts = p.attempts + 1
        FROM picked WHERE p.id = picked.id RETURNING p.id
    """
    assert _LIMITED_LOCKING_SUBSELECT.search(unsafe)
    assert not _LIMITED_LOCKING_SUBSELECT.search(safe)


def test_no_statement_in_src_claims_a_limited_batch_through_an_in_subselect() -> None:
    """A limited, locking `IN (subselect)` is a defect wherever it is written."""
    offenders = [
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if _LIMITED_LOCKING_SUBSELECT.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{offenders} pick a limited batch with `IN (SELECT ... LIMIT n FOR UPDATE)`; use "
        "`WITH picked AS MATERIALIZED (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED) "
        "UPDATE ... FROM picked`"
    )
