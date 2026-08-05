"""The schema is forward-only and additive — a policy rather than a habit, because this fails.

`infra/sql/` has 35 migrations, no down-path, and — until this file — nothing saying whether that
was a decision or an omission. Measured before deciding: **not one destructive statement exists in
any of them.** No `DROP`, no `ALTER … DROP COLUMN`, no `RENAME`, no `TRUNCATE`, no `DELETE`. The
only `ALTER TABLE` in the tree is `artifact_blobs ALTER COLUMN data SET STORAGE EXTERNAL`, a
TOAST hint that moves no data.

So the policy was already being followed and was written nowhere, which is the state in which
migration 036 drops a column and nobody notices until the restore. This file writes it down in the
only form that holds — a check that fails.

**Why additive-forward rather than a tested down-path** (D-2026-08-04-the-schema-only-goes-forward):

* A down migration for a GxP system is a compliance hazard wearing a safety net's clothes. The
  audit trail is append-only and hash-chained precisely so that history cannot be rewritten; a
  scripted `DROP COLUMN` on `audit_events` is the thing that control exists to prevent, and having
  it in the repository makes it one command away.
* For an additive schema the rollback already exists and needs no script: **deploy the previous
  image**. The old code ignores the new column, the new table sits unread, and the data stays.
  That property is exactly what "additive" buys, and it is worth more than a down-path because it
  is the one that works under pressure.
* A down-path that is never run is not a rollback, it is a second schema definition that drifts.
  The migrations here are `IF NOT EXISTS` and re-runnable; their inverse would be neither.

The cost is stated plainly rather than hidden: a column that turns out to be wrong is *deprecated*,
not removed, and the tree grows. A genuine removal is a deliberate, reviewed operation — which is
what an explicit refusal here forces it to be, instead of a line in a file that ran at deploy time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MIGRATIONS = Path(__file__).resolve().parents[1] / "infra" / "sql"

# Statements that destroy schema or data. Matched on statement *starts* (after optional whitespace)
# rather than anywhere in the file, so the word "drop" in a comment — or `DROP` inside a
# `CREATE INDEX … WHERE` predicate — is not a false positive.
_DESTRUCTIVE = re.compile(
    r"^\s*(?:"
    r"DROP\s+(?:TABLE|COLUMN|INDEX|SCHEMA|TYPE|VIEW|DATABASE|CONSTRAINT)"
    r"|TRUNCATE"
    r"|DELETE\s+FROM"
    r"|ALTER\s+TABLE\s+\w+\s+(?:DROP|RENAME)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# `--` line comments, stripped before the scan. Every migration in this tree is heavily commented
# and several comments discuss what they are careful *not* to drop; scanning the prose would fail
# the check on the files that explain the policy best.
_COMMENT = re.compile(r"--[^\n]*")


def _statements(path: Path) -> str:
    """The migration's SQL with `--` comments removed."""
    return _COMMENT.sub("", path.read_text(encoding="utf-8"))


def _migration_files() -> list[Path]:
    """Every migration, in the order the runner applies them (filename order)."""
    return sorted(_MIGRATIONS.glob("*.sql"))


def test_there_are_migrations_to_check() -> None:
    """The scan below is worthless against an empty glob — so the glob is asserted first.

    A check that silently examines nothing is the vacuous-pass shape this repository has hit
    repeatedly: an audit chain verifying over zero rows, a probe suite grading zero probes. A
    renamed directory would turn every assertion in this file into a tautology, and this is the
    one line that would notice.
    """
    files = _migration_files()
    assert len(files) >= 30, f"only {len(files)} migrations found under {_MIGRATIONS}"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_a_migration_destroys_nothing(path: Path) -> None:
    """No migration may drop, rename, truncate or delete. The policy, as a failure.

    Parametrized per file rather than folded into one assertion so a violation names the migration
    that introduced it — the message an author needs is "036 drops a column", not "some file does".
    """
    found = _DESTRUCTIVE.findall(_statements(path))
    assert not found, (
        f"{path.name} contains a destructive statement ({found[0].strip()!r}). The schema is "
        "forward-only and additive (D-2026-08-04-the-schema-only-goes-forward): rollback is "
        "'deploy the previous image', which only works while old columns still exist. Deprecate "
        "the column instead, or take the removal as a reviewed operation outside the migration set."
    )


def test_every_migration_is_re_runnable() -> None:
    """Each file creates only with `IF NOT EXISTS` — the property the ledger's drift check assumes.

    The runner records each file's hash and refuses a changed one, so a migration is applied
    exactly once in the normal path. `IF NOT EXISTS` is what covers the abnormal ones: a restored
    database whose `schema_migrations` ledger is older than its tables, or an operator re-pointing
    the runner at a database that was built by hand. Without it the recovery is "work out which
    statements already ran", by hand, under pressure.
    """
    offenders: list[str] = []
    for path in _migration_files():
        sql = _statements(path)
        for match in re.finditer(
            r"^\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|SCHEMA|TYPE|VIEW)\b(.*?)$",
            sql,
            re.IGNORECASE | re.MULTILINE,
        ):
            kind, rest = match.group(1).upper(), match.group(2)
            # `CREATE TYPE` has no `IF NOT EXISTS` in Postgres; those are written as `DO $$ …
            # EXCEPTION WHEN duplicate_object` blocks instead, which is the same guarantee.
            if kind == "TYPE":
                continue
            if "IF NOT EXISTS" not in rest.upper():
                offenders.append(f"{path.name}: CREATE {kind}{rest[:60]}")
    assert not offenders, "migrations must be re-runnable:\n" + "\n".join(offenders)
