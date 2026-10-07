"""Two guards held by tests in the modules that state them.

- `core/tracing.SpanHandle.set_attribute`, like `failed`, must swallow: both are called from
  `except` blocks on the tool path, where a tracing failure must not replace the real failure.
- Every migration index is `IF NOT EXISTS`, so an operator can pre-build one `CONCURRENTLY`
  (which `core/migrate.py`'s single transaction cannot) before deploying, as `infra/sql/059`'s
  header advises for the unpruned `audit_events` table.
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
    """Stamping an attribute cannot replace the failure being reported.

    `agent/audit.py` calls `set_attribute` and `failed` from one `except` block; a raise there would
    lose the audit row, the metric and the real fault together.
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
    """Every index is `IF NOT EXISTS`, so it can be pre-built concurrently.

    Migrations run in one transaction, where `CREATE INDEX CONCURRENTLY` is refused, so an index on
    a large table can only be built without blocking writes ahead of the deploy. A bare `CREATE
    INDEX` would then fail as a duplicate.
    """
    text = migration.read_text(encoding="utf-8")
    for match in _CREATE_INDEX.finditer(text):
        tail = text[match.start() : match.start() + 200]
        assert "IF NOT EXISTS" in tail.upper(), (
            f"{migration.name} creates an index without IF NOT EXISTS, so it cannot be pre-built "
            f"concurrently on a large table: {tail.splitlines()[0]}"
        )


def test_the_index_this_was_written_for_is_still_the_one_on_the_unpruned_table() -> None:
    """The index the rule above was written for still exists on the unpruned table.

    If `audit_events_tool_outcome_ts_idx` were dropped or moved, `059`'s header would describe
    nothing.
    """
    header = (_MIGRATIONS / "059_audit_plan_step.sql").read_text(encoding="utf-8")
    assert "audit_events_tool_outcome_ts_idx" in header
    assert "CONCURRENTLY" in header, (
        "059 no longer states what a deployment with a large audit_events has to do before "
        "applying it"
    )
