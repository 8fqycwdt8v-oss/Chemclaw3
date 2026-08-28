"""Two guards a reviewer found were claimed rather than held, in the modules that claim them.

Both are the same shape: a rule stated in prose beside code that does not implement it.

- `core/tracing.SpanHandle.failed` swallows because "a tracing failure must not replace the failure
  being reported", and it is called from `except` blocks on the tool path. `set_attribute` is
  called from those same blocks — `agent/audit.py` stamps the outcome one line before it marks the
  span — and did not swallow.
- `infra/sql/059` builds an index on `audit_events`, the one table `durable/retention.py` refuses
  to prune, with a lock that blocks every audit INSERT for the build. `CONCURRENTLY` cannot run in
  `core/migrate.py`'s single transaction, so the deployment escape hatch is to build it
  concurrently *before* deploying and let `IF NOT EXISTS` no-op the migration. That hatch only
  exists while every index in this directory is `IF NOT EXISTS`, which is what this pins.

Here rather than in `tests/test_agent_observability_*.py` because neither belongs to an
observability surface: one is the tracing seam every layer shares, the other is the schema.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.tracing import SpanHandle

_MIGRATIONS = Path("infra/sql")

# `CREATE [UNIQUE] INDEX` up to the point where `IF NOT EXISTS` would have to appear. Matched over
# the file rather than per line because these statements wrap.
_CREATE_INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY\b)(\w+)?", re.I)


class _DeadSpan:
    """A span whose provider has gone — the shape both methods have to survive."""

    def set_attribute(self, key: str, value: Any) -> None:
        """Raise the way a torn-down exporter or a rejected value type does."""
        raise RuntimeError("the tracer provider has been shut down")

    def set_status(self, status: Any) -> None:
        """Raise for the same reason, so both halves of the pairing are driven."""
        raise RuntimeError("the tracer provider has been shut down")


def test_stamping_an_attribute_cannot_replace_the_failure_being_reported() -> None:
    """The rule `failed` states and `set_attribute` did not follow.

    `agent/audit.py` calls both from one `except` block, one line apart, so a raising
    `set_attribute` would surface a `RuntimeError` about tracing in place of the tool failure the
    handler was written to record — losing the audit row, the metric and the real fault together.
    """
    handle = SpanHandle(_DeadSpan())
    handle.set_attribute("chemclaw.outcome", "error")
    handle.failed("the tool returned an error")


def test_the_untraced_path_still_costs_one_check() -> None:
    """The guard must not turn the no-op path into a `try`/`except` on every tool call."""
    SpanHandle(None).set_attribute("chemclaw.outcome", "ok")


@pytest.mark.parametrize("migration", sorted(_MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
def test_every_index_is_if_not_exists_so_it_can_be_pre_built_concurrently(
    migration: Path,
) -> None:
    """The escape hatch `059`'s header offers a deployment, held open for every migration.

    `core/migrate.py` runs the whole set inside one transaction (`pg_advisory_xact_lock`), and
    Postgres refuses `CREATE INDEX CONCURRENTLY` inside a transaction block — so an index on a
    table that grows forever can only be built without blocking writes if an operator builds it
    concurrently *ahead of the deploy* and the migration then finds it already there. Measured on
    this repository's Postgres image over `audit_events`' shape: **1.24 s per million rows**, and
    that table is never pruned.

    A bare `CREATE INDEX` would fail on the second run with a duplicate-index error, taking the
    hatch away for that migration and, worse, doing it silently until the day it is needed.
    """
    text = migration.read_text(encoding="utf-8")
    for match in _CREATE_INDEX.finditer(text):
        tail = text[match.start() : match.start() + 200]
        assert "IF NOT EXISTS" in tail.upper(), (
            f"{migration.name} creates an index without IF NOT EXISTS, so it cannot be pre-built "
            f"concurrently on a large table: {tail.splitlines()[0]}"
        )


def test_the_index_this_was_written_for_is_still_the_one_on_the_unpruned_table() -> None:
    """The guard on the guard: the rule above is only worth having while that index exists.

    If `audit_events_tool_outcome_ts_idx` were ever dropped or moved, the header explaining its
    cost would be describing nothing — the failure mode this repository calls a claim rather than a
    control.
    """
    header = (_MIGRATIONS / "059_audit_plan_step.sql").read_text(encoding="utf-8")
    assert "audit_events_tool_outcome_ts_idx" in header
    assert "CONCURRENTLY" in header, (
        "059 no longer states what a deployment with a large audit_events has to do before "
        "applying it"
    )
