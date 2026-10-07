"""What an operator sees of artefact writes: one `exhibit.*` event and two counters, for every path.

One function per fact, called by every writer (`api/routes/exhibits.py`, `agent/exhibit_tools.py`,
the report activity), so events and counters agree. Labels are closed sets fixed here and in
`AuthorKind`; artefact, session and author are log fields, never labels.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.exhibits.models import ExhibitLimit, ExhibitView, StaleRevision, UnknownExhibit

logger = logging.getLogger(__name__)

#: What a write did to the artefact — the `exhibit` event's own `op`.
WriteOp = Literal["created", "revised"]
#: Why a write was refused: the 422 family (and every other worded refusal a writer can correct),
#: a stale base, a cap on how many, or an artefact the session does not hold.
RefusalReason = Literal["invalid", "stale_revision", "exhibit_limit", "not_found"]


def record_write(view: ExhibitView, op: WriteOp) -> None:
    """Log the revision just written as `exhibit.<op>` and count it by author kind and op."""
    log_event(
        logger,
        f"exhibit.{op}",
        "artefact %s revision %d %s by %s (%s)",
        view.exhibit_id,
        view.revision,
        op,
        view.author,
        view.author_kind,
        actor=view.author,
        session=view.session_id,
        exhibit_id=view.exhibit_id,
        revision=view.revision,
        kind=view.kind,
        author_kind=view.author_kind,
    )
    record_metric(
        lambda metrics: metrics.increment(
            "chemclaw_exhibit_writes_total", labels={"author_kind": view.author_kind, "op": op}
        )
    )


def record_refusal(reason: RefusalReason) -> None:
    """Count one refused artefact write."""
    record_metric(
        lambda metrics: metrics.increment(
            "chemclaw_exhibit_refusals_total", labels={"reason": reason}
        )
    )


@contextmanager
def refusals_counted() -> Iterator[None]:
    """Count a refusal this package raises inside the block, by its class, and re-raise it."""
    try:
        yield
    except StaleRevision:
        record_refusal("stale_revision")
        raise
    except ExhibitLimit:
        record_refusal("exhibit_limit")
        raise
    except UnknownExhibit:
        record_refusal("not_found")
        raise
    except ChemclawError:
        # `InvalidExhibit` and the tools' own refusals — each a write the writer can correct.
        record_refusal("invalid")
        raise
