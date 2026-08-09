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

**Two buckets, because the first version of this check had one and it was wrong in both
directions** (D-2026-08-08-a-rollback-that-is-not-a-schema-step). It matched `DROP CONSTRAINT`,
which destroys no data at all, and it was blind to `SET NOT NULL`, which destroys no data either
*and stops the previous image from writing the table*. It therefore refused a primary-key rebuild
for a reason that was not true while passing the statement beside it that actually broke the
rollback. Measured on a scratch database with 000→041 applied, running the previous image's own
statements verbatim: `SET NOT NULL` on `document_files.chunking_key` failed every file write, and
the replaced primary key failed every chunk write with "no unique or exclusion constraint matching
the ON CONFLICT specification". The `DROP CONSTRAINT` the check named cost nothing by itself.

So the two things are asked separately, because they have different answers. **Destroying data is
refused outright** — no exemption exists, rollback cannot bring rows back. **Breaking the previous
image is refusable but reviewable**: the data survives, the previous image simply cannot write, and
whether that is acceptable is a judgement about one migration rather than a rule. A migration in
`_REVIEWED_ROLLBACK_BREAKS` has had that judgement made, in an ADR that states what an operator
does instead of "deploy the previous image".
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from chemclaw.core.migrate import _statements

_MIGRATIONS = Path(__file__).resolve().parents[1] / "infra" / "sql"
_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"

# Statements that destroy data, or the object holding it. Matched on statement *starts* (after
# optional whitespace) rather than anywhere in the file, so the word "drop" in a comment — or
# `DROP` inside a `CREATE INDEX … WHERE` predicate — is not a false positive.
#
# `DROP CONSTRAINT` and `DROP INDEX` are deliberately **not** here: neither removes a row. They
# belong to the second bucket, where they can be reviewed for what they actually cost.
_DESTROYS_DATA = re.compile(
    r"^\s*(?:"
    r"DROP\s+(?:TABLE|SCHEMA|TYPE|VIEW|DATABASE)"
    r"|TRUNCATE"
    r"|DELETE\s+FROM"
    r"|ALTER\s+TABLE\s+\w+\s+(?:DROP\s+COLUMN|RENAME)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Statements that destroy no data and still end the "deploy the previous image" rollback, because
# after them a write the previous image makes no longer succeeds.
#
# **Unconditional breaks only**, and the line is deliberate rather than convenient. `SET NOT NULL`
# on an existing column rejects *every* insert that omits it; dropping or replacing a key makes
# *every* `ON CONFLICT` naming the old one fail to plan. Both fail regardless of what is in the
# table. A `CHECK` constraint or a `CREATE UNIQUE INDEX` narrows what may be written but rejects
# only *some* rows — data-dependent, and four merged migrations (014, 016, 017,
# 037_bo_suggestion_provenance) add unique indexes to tables that already existed. Flagging those
# would be over-reach dressed as rigour; they are named in `docs/planning/BACKLOG.md` instead, so
# what this check does not cover is written down rather than implied.
#
# `ADD COLUMN … NOT NULL` without a `DEFAULT` is absent for a different reason: Postgres refuses it
# on a non-empty table, so it can only appear on a table the previous image does not write anyway.
#
# `DROP INDEX` is the one member that can over-flag: `ON CONFLICT` infers its arbiter from a unique
# index, so dropping one breaks writes exactly as dropping the constraint does — and dropping a
# plain index costs only a plan. A pattern cannot tell them apart, and the previous version of this
# check called every `DROP INDEX` destructive, which is further from the truth than this is. The
# answer to an over-flag is a reviewed exemption naming the statement, not a looser pattern.
_BREAKS_PREVIOUS_IMAGE = re.compile(
    r"^\s*(?:"
    r"ALTER\s+TABLE\s+\w+\s+ALTER\s+(?:COLUMN\s+)?\w+\s+SET\s+NOT\s+NULL"
    r"|ALTER\s+TABLE\s+\w+\s+DROP\s+CONSTRAINT"
    r"|ALTER\s+TABLE\s+\w+\s+ADD\s+(?:CONSTRAINT\s+\w+\s+)?PRIMARY\s+KEY"
    r"|DROP\s+INDEX"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Migrations reviewed and accepted as ending the previous-image rollback, each mapped to the exact
# statement prefixes `_BREAKS_PREVIOUS_IMAGE` matches in it and to the ADR that says what an
# operator does instead. Exact rather than per-file, so a later edit that adds a *fifth* break to
# an exempted migration still fails — an exemption is granted to statements somebody read, not to a
# filename.
_REVIEWED_ROLLBACK_BREAKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "041_document_chunk_identity.sql": (
        "D-2026-08-08-a-rollback-that-is-not-a-schema-step",
        (
            "ALTER TABLE document_files ALTER COLUMN chunking_key SET NOT NULL",
            "ALTER TABLE document_chunks ALTER COLUMN chunking_key SET NOT NULL",
            "ALTER TABLE document_chunks DROP CONSTRAINT",
            "ALTER TABLE document_chunks ADD PRIMARY KEY",
        ),
    ),
}

# Comment stripping is the *runner's* `_statements`, imported rather than reimplemented. Every
# migration here is heavily commented and several comments discuss what they are careful *not* to
# drop, so scanning the prose would fail the check on the files that explain the policy best — and
# the runner needs the identical reduction, because its drift checksum is taken over it. Two
# spellings of "the SQL, without the prose" is how a test and the thing it guards start disagreeing.


def _sql(path: Path) -> str:
    """The migration's SQL with its comment lines removed, as the runner sees it."""
    return _statements(path.read_text(encoding="utf-8"))


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
    """No migration may drop a table or column, rename, truncate or delete. No exemptions.

    Parametrized per file rather than folded into one assertion so a violation names the migration
    that introduced it — the message an author needs is "036 drops a column", not "some file does".
    """
    found = _DESTROYS_DATA.findall(_sql(path))
    assert not found, (
        f"{path.name} contains a destructive statement ({found[0].strip()!r}). The schema is "
        "forward-only and additive (D-2026-08-04-the-schema-only-goes-forward): rollback is "
        "'deploy the previous image', and rows this removes are not there to roll back to. "
        "Deprecate the column instead, or take the removal as a reviewed operation outside the "
        "migration set."
    )


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_a_migration_leaves_the_previous_image_able_to_write(path: Path) -> None:
    """No migration may make a write the previous image performs stop working — unless reviewed.

    The other half of "deploy the previous image", and the half nothing checked: a migration can
    leave every row in place and still end the rollback, because the previous image's `INSERT` no
    longer satisfies the table. That is what 041 does, and what the single-bucket check missed
    while refusing the `DROP CONSTRAINT` beside it.

    An exemption is exact: the statements this file flags must be *exactly* the reviewed set, so a
    stale entry fails as loudly as a new break. And the ADR that granted it must exist and name the
    migration — the exemption's whole content is the rollback procedure that replaces "deploy the
    previous image", so an exemption without one is an exemption nobody wrote down.
    """
    found = tuple(match.strip() for match in _BREAKS_PREVIOUS_IMAGE.findall(_sql(path)))
    reviewed = _REVIEWED_ROLLBACK_BREAKS.get(path.name)
    if reviewed is None:
        assert not found, (
            f"{path.name} makes a write the previous image performs fail ({found[0]!r}), so "
            "'deploy the previous image' is no longer the rollback "
            "(D-2026-08-08-a-rollback-that-is-not-a-schema-step). Either keep the previous "
            "image's writes working, or add the migration to `_REVIEWED_ROLLBACK_BREAKS` with an "
            "ADR stating what an operator does instead."
        )
        return
    adr, expected = reviewed
    assert found == expected, (
        f"{path.name}'s reviewed exemption no longer describes it: flagged {list(found)}, "
        f"reviewed {list(expected)}. An exemption covers statements somebody read, not a "
        f"filename — re-review it and update {adr}."
    )
    assert (_DECISIONS / f"{adr}.md").is_file(), f"{path.name} is exempted by a missing ADR {adr}"
    assert path.name in (_DECISIONS / f"{adr}.md").read_text(encoding="utf-8"), (
        f"{adr} grants {path.name} an exemption without naming it, so the rollback procedure it "
        "is supposed to carry cannot be found from the migration."
    )


@pytest.mark.parametrize(
    ("statement", "destroys", "breaks"),
    [
        # The two the single-bucket pattern got wrong, in both directions.
        ("ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS document_chunks_pkey;", 0, 1),
        ("ALTER TABLE document_files ALTER COLUMN chunking_key SET NOT NULL;", 0, 1),
        # Unambiguous data destruction stays destruction.
        ("ALTER TABLE t DROP COLUMN c;", 1, 0),
        ("DROP TABLE t;", 1, 0),
        ("TRUNCATE t;", 1, 0),
        ("DELETE FROM t WHERE x;", 1, 0),
        ("ALTER TABLE t RENAME COLUMN a TO b;", 1, 0),
        # A key replacement breaks the previous image's `ON CONFLICT` without losing a row.
        ("ALTER TABLE t ADD PRIMARY KEY (a, b);", 0, 1),
        ("ALTER TABLE t ADD CONSTRAINT t_pkey PRIMARY KEY (a, b);", 0, 1),
        # Removing an index removes no row, so it is reviewable rather than refused outright.
        ("DROP INDEX IF EXISTS t_idx;", 0, 1),
        # The additive shapes 004/010/011/026/029/033/036/037 use, and 019's TOAST hint: neither.
        ("ALTER TABLE t ADD COLUMN IF NOT EXISTS c TEXT NOT NULL DEFAULT '';", 0, 0),
        ("ALTER TABLE t ALTER COLUMN data SET STORAGE EXTERNAL;", 0, 0),
        ("UPDATE t SET c = '' WHERE c IS NULL;", 0, 0),
        ("CREATE TABLE IF NOT EXISTS t (a TEXT NOT NULL);", 0, 0),
        # Prose, and `DROP` inside a predicate, are why both patterns anchor to statement starts.
        ("-- this migration is careful not to DROP TABLE anything\n", 0, 0),
        ("CREATE INDEX IF NOT EXISTS i ON t (a) WHERE kind <> 'DROP TABLE';", 0, 0),
    ],
)
def test_the_two_patterns_say_what_they_mean(statement: str, destroys: int, breaks: int) -> None:
    """Each bucket matches its own statements and not the other's — the correction, as a test.

    Asked of synthetic SQL rather than of the tree, because the tree is exactly one example of each
    and a pattern that happens to fit one file is how the previous version passed review. These are
    the cases that decide whether the check is honest: a `DROP CONSTRAINT` that destroys nothing, a
    `SET NOT NULL` that destroys nothing and still ends the rollback, and the additive
    `ADD COLUMN … NOT NULL DEFAULT` that eight merged migrations use and neither bucket may claim.
    """
    assert len(_DESTROYS_DATA.findall(statement)) == destroys
    assert len(_BREAKS_PREVIOUS_IMAGE.findall(statement)) == breaks


def test_no_exemption_outlives_its_migration() -> None:
    """An exemption names a file that exists — otherwise it is a permission nobody can see spent.

    The check above is parametrized over the migrations on disk, so an entry for a file that was
    never added (a typo) or has gone (a rename) is simply never consulted. That is the shape a
    granted exemption drifts into an unnoticed blanket one, so it is asserted here instead.
    """
    orphaned = sorted(set(_REVIEWED_ROLLBACK_BREAKS) - {p.name for p in _migration_files()})
    assert not orphaned, f"reviewed exemption(s) naming no migration: {orphaned}"


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
        sql = _sql(path)
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


def test_no_merged_migration_had_its_statements_changed() -> None:
    """A merged migration's *statements* are immutable. Its comments are not, and that is the fix.

    `core/migrate.py` records a checksum per file and refuses to run when it changes. The
    checksum used to cover the whole file, which made every comment edit an outage: migrations
    refuse on **every database that already applied the file**, while CI stays green because CI
    always starts from an empty one. It happened twice, from two different sessions, and both edits
    were *correct* — `006_audit_events.sql` renaming a module the D-148 package move had moved,
    `031_bo_campaigns.sql` recording that two columns hold the lead objective only.

    So the guard now hashes the statements (`_statements`), and this test asks the same question of
    history that the runner asks of the ledger. One definition of "changed", used by both — the
    alternative is a test and a runtime guard that can disagree about whether a file drifted.

    Asked of git because the question *is* history: what a file contained in the commit that
    introduced it. A file added in the working tree and not yet committed is skipped — it has not
    landed, so it is still free to change.
    """
    repo = _MIGRATIONS.parents[1]
    edited: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        introduced = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "--", path.name],
            cwd=_MIGRATIONS,
            capture_output=True,
            text=True,
        ).stdout.split()
        if not introduced:
            continue  # added in the working tree; not merged, so not yet immutable
        original = subprocess.run(
            ["git", "show", f"{introduced[-1]}:{path.relative_to(repo)}"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if original.returncode != 0:
            continue  # renamed on the way in; `--follow` semantics are not worth the ambiguity
        if _statements(original.stdout) != _statements(path.read_text(encoding="utf-8")):
            edited.append(path.name)
    assert not edited, (
        f"migration(s) whose statements changed after being merged: {edited}. The runner keys on a "
        "checksum of exactly this, so it breaks `make db-migrate` on every database that already "
        "applied them. Put the change in a new migration."
    )
