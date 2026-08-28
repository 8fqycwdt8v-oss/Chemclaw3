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

* A down migration is a data-loss hazard wearing a safety net's clothes. The audit trail is
  append-only precisely so that history cannot be rewritten; a scripted `DROP COLUMN` on
  `audit_events` is the thing that property exists to prevent, and having it in the repository
  makes it one command away.
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

# How an identifier may be spelled. Both patterns below used to say `\w+`, which is the bare
# lower-case spelling every merged migration happens to use and only that one — so a
# schema-qualified, `ONLY`, `IF EXISTS` or quoted table name walked through both checks. `ALTER
# TABLE ONLY …` is the form **`pg_dump` emits**, i.e. the likeliest thing an author pastes out of a
# dump while writing a migration, which made the miss the opposite of academic. Written once and
# substituted into every position that names a table or a column, so the two buckets cannot drift
# into policing different spellings of the same statement.
_NAME = r"[\w.\"]+"  # `t`, `public.t`, `"t"`, `public."t"`
_TABLE = rf"(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_NAME}"  # ALTER TABLE [IF EXISTS] [ONLY] name

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
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+(?:DROP\s+COLUMN|RENAME)"
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
    rf"ALTER\s+TABLE\s+{_TABLE}\s+ALTER\s+(?:COLUMN\s+)?{_NAME}\s+SET\s+NOT\s+NULL"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+DROP\s+CONSTRAINT"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+ADD\s+(?:CONSTRAINT\s+{_NAME}\s+)?PRIMARY\s+KEY"
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
    "058_note_proposal_superseded.sql": (
        # Reviewed, and it does not in fact end the rollback: the drop-and-re-add *widens* the
        # state CHECK, and the previous image only ever writes the old, still-allowed states —
        # the ADR records that reading, and this row exists because the guard matches the DROP
        # CONSTRAINT text, not the semantics.
        "D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed",
        ("ALTER TABLE note_proposals DROP CONSTRAINT",),
    ),
    "063_reaction_fingerprint_source.sql": (
        "D-2026-08-27-a-fingerprint-is-keyed-by-its-source",
        (
            "ALTER TABLE reaction_fingerprints DROP CONSTRAINT",
            "ALTER TABLE reaction_fingerprints ADD PRIMARY KEY",
        ),
    ),
    "056_reaction_record_identity.sql": (
        "D-2026-08-26-a-transcription-is-keyed-by-its-source",
        (
            "ALTER TABLE reaction_records DROP CONSTRAINT",
            "ALTER TABLE reaction_records ADD PRIMARY KEY",
        ),
    ),
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

# The two migrations whose statements were edited *before* the guard below could run, kept as named
# exemptions rather than repaired — because the repair is what would break things now.
#
# **They were found the day `fetch-depth: 0` reached CI.** The check below had never actually
# executed: on `actions/checkout`'s depth-1 default every migration compared
# equal to itself, so it reported no edit across all 45. Turning the checkout on is what asked the
# question for the first time, and this is its first answer — which is the check working, not a
# regression.
#
# **The edit was deliberate and is documented in the tree.** `004_fingerprint_definition.sql` says
# so in its own opening line: "Fresh databases get the column straight from 002/003; this migration
# brings an existing dev database up to date." Someone added `definition` to both `CREATE TABLE`s
# *and* wrote the `ALTER` for databases that had already run them. By today's rule
# (`D-2026-08-04-the-schema-only-goes-forward`) only the second half is allowed. It predates the
# rule.
#
# **Reverting them would break every database that exists to fix one that cannot.** The ledger keys
# on the checksum recorded when a file was applied, so:
#
#   * a database that applied 002 *before* the edit already fails `make db-migrate` today — and it
#     is unreachable anyway, because that version named the column `smiles`, nothing ever renamed it
#     to `label`, and no current query would find it. There is no supported database in that state.
#   * every database created *since* recorded the current checksum. Restoring the old statements
#     would make `make db-migrate` refuse on all of them — CI, every dev sandbox, every deployment.
#
# So the honest move is the one the collision check makes for `037`/`043`: name them, say why, and
# keep the teeth for everything that comes after. Each entry is checked to still *be* an edit
# (`test_no_grandfathered_edit_outlives_its_reason`), so an exemption that stops applying fails
# rather than quietly widening.
_GRANDFATHERED_EDITS: frozenset[str] = frozenset(
    {"002_molecule_fingerprints.sql", "003_reaction_fingerprints.sql"}
)

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
        # The four spellings of a table name Postgres accepts, on the same destructive statement.
        # `ALTER TABLE ONLY` is the one `pg_dump` emits, so it is the likeliest thing an author
        # pastes; a check that reads only the bare identifier misses all four.
        ("ALTER TABLE public.audit_events DROP COLUMN actor;", 1, 0),
        ("ALTER TABLE ONLY audit_events DROP COLUMN actor;", 1, 0),
        ("ALTER TABLE IF EXISTS audit_events DROP COLUMN actor;", 1, 0),
        ('ALTER TABLE "audit_events" DROP COLUMN actor;', 1, 0),
        # …and on the two rollback breaks that are spelled with a table name.
        ("ALTER TABLE public.document_files ALTER COLUMN k SET NOT NULL;", 0, 1),
        ("ALTER TABLE ONLY document_chunks DROP CONSTRAINT document_chunks_pkey;", 0, 1),
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


def _git(repo: Path, *args: str) -> str:
    """`git` in `repo`, stdout only. A failure is the empty string — every caller treats it so."""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


def _shallow_grafts(repo: Path) -> frozenset[str]:
    """The commits git reports as parentless *only* because the clone was truncated there.

    A graft is indistinguishable from a real commit in `git log`: it has a hash, a tree and a date,
    and `--diff-filter=A` will happily name it as the commit that "added" every file whose true
    introduction lies beyond the boundary. That is not a defect in git — the earlier history is
    simply absent — but it makes the comparison below vacuous in a way `compared` cannot see, so the
    walk has to know which commits are boundaries rather than beginnings.

    Read from `.git/shallow` via `rev-parse --git-path`, so it resolves under a worktree or a
    relocated `$GIT_DIR` rather than assuming `repo/.git`. Empty on a full clone, which is what
    makes this a no-op on CI (`fetch-depth: 0`) and keeps the *true* root commit a legitimate
    introducing commit — a migration added in the repository's first commit must still be checked.
    """
    if _git(repo, "rev-parse", "--is-shallow-repository") != "true":
        return frozenset()
    shallow = Path(_git(repo, "rev-parse", "--git-path", "shallow"))
    if not shallow.is_absolute():
        shallow = repo / shallow
    if not shallow.is_file():
        return frozenset()
    return frozenset(shallow.read_text(encoding="utf-8").split())


def _statements_changed_since_merge(migrations: Path | None = None) -> tuple[list[str], int]:
    """Which merged migrations differ from the commit that added them, and how many were compared.

    Extracted so the immutability check and its exemption's staleness check ask git the *same*
    question. A shared module-level cache would make them order-dependent, and a second copy of the
    walk would let the exemption be validated against a rule the check no longer applies — which is
    precisely how an exemption outlives its reason.

    `compared` counts only comparisons that **span a commit**, and there are two ways for one not
    to. A file introduced by `HEAD` itself has nothing earlier to differ from. And a file whose
    introducing commit is a **shallow graft** has nothing earlier *available*: git names the
    boundary as the adding commit, `git show <graft>:file` returns the content as of the boundary,
    and a file untouched since then compares equal to itself while any edit made before the boundary
    is invisible.

    **The second case is the one that was missing, and it was measured rather than reasoned about.**
    The docstring below used to say this repository's shallow checkout "still spans every
    migration, so the skip is narrow". That stopped being true: on a 171-commit shallow clone whose
    graft is `4ee6056`, `002_molecule_fingerprints.sql` reported that graft as its adding commit and
    compared equal, so all 47 migrations "compared" — clearing the `>= 30` floor — while both
    genuinely-edited exemptions looked stale and
    `test_no_grandfathered_edit_outlives_its_reason` failed. A truncation deep enough to clear the
    floor is exactly the case the floor was meant to catch, so the boundary has to be excluded at
    the source rather than absorbed by a larger threshold: a bigger number would only move the depth
    at which the same silence returns.
    """
    migrations = migrations if migrations is not None else _MIGRATIONS
    repo = migrations.parents[1]
    head = _git(repo, "rev-parse", "HEAD")
    grafts = _shallow_grafts(repo)
    edited: list[str] = []
    compared = 0
    for path in sorted(migrations.glob("*.sql")):
        introduced = _git(
            migrations, "log", "--diff-filter=A", "--format=%H", "--", path.name
        ).split()
        if not introduced:
            continue  # added in the working tree; not merged, so not yet immutable
        if introduced[-1] == head:
            continue  # introduced by the commit under test — there is no earlier version to differ
        if introduced[-1] in grafts:
            continue  # the clone stops here; the real introduction is beyond the boundary
        original = subprocess.run(
            ["git", "show", f"{introduced[-1]}:{path.relative_to(repo)}"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if original.returncode != 0:
            continue  # renamed on the way in; `--follow` semantics are not worth the ambiguity
        compared += 1
        if _statements(original.stdout) != _statements(path.read_text(encoding="utf-8")):
            edited.append(path.name)
    return edited, compared


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

    **Two ways this answers without having looked, and only one of them is the empty glob.**

    *No history at all* — a tarball, or `git` absent — makes every `git log` empty, so every file
    takes the not-yet-merged branch and the check passes having compared nothing. Measured: on a
    copy of the tree with `.git` removed, this passed green.

    *Truncated history* is the one a count cannot see, because the count stays healthy while the
    comparison stops spanning anything. On a `git clone --depth=1` every file looks introduced by
    the graft commit, and the graft commit **is** `HEAD`, so `git show <introduced>:file` returns
    the working tree's own content and each file is compared against itself. Measured on a
    depth-1 clone whose `HEAD` already carried a `smuggled` `ALTER TABLE` appended to a merged
    `006_audit_events.sql`: reported as no edit, 42 files "compared". `actions/checkout` defaults
    to `fetch-depth: 1`, so that is exactly the CI checkout.

    So what is counted is not "files looked at" but **comparisons that span a commit** — the
    introducing commit is neither `HEAD` nor a shallow graft. That one number distinguishes all
    three cases without a second mechanism: 42 here, 0 on a depth-1 clone, 0 with no `.git`. A
    migration genuinely added in `HEAD` is excluded from it and from the check, which is right: it
    has nothing earlier to differ from.

    **A partial clone is not only a depth-1 clone**, and excluding the graft is what makes the
    count honest about the difference. A truncation *above* the migrations leaves plenty of visible
    history — enough to clear any floor — while every comparison still lands on the boundary rather
    than on a real earlier version. `_shallow_grafts` says how that is detected and what it
    measured; the consequence here is that the count now falls to the comparisons that are real, so
    the skip below fires on a truncated clone of *any* depth instead of only the shallowest one.

    The floor is an assertion, except where git says the history is truncated — then it is a skip
    naming the fix, because a truncated checkout is a CI setting rather than a defect in the tree
    and a red build would say the wrong thing about it. CI sets `fetch-depth: 0`
    (`.github/workflows/ci.yml`), so on CI there are no grafts, nothing is excluded, and the check
    asks its question of every merged migration.
    """
    repo = _MIGRATIONS.parents[1]
    edited, compared = _statements_changed_since_merge()
    if compared < 30 and _git(repo, "rev-parse", "--is-shallow-repository") == "true":
        pytest.skip(
            f"truncated history: only {compared} migration(s) could be compared against an "
            "earlier commit, so this check would compare files against themselves and pass "
            "whatever was edited. Set `fetch-depth: 0` on actions/checkout to run it."
        )
    assert compared >= 30, (
        f"only {compared} of {len(list(_MIGRATIONS.glob('*.sql')))} migrations were compared "
        "against the commit that introduced them; the rest had no earlier version to compare "
        "with. This test has just passed without asking its question of anything."
    )
    assert not set(edited) - _GRANDFATHERED_EDITS, (
        f"migration(s) whose statements changed after being merged: "
        f"{sorted(set(edited) - _GRANDFATHERED_EDITS)}. The runner keys on a checksum of exactly "
        "this, so it breaks `make db-migrate` on every database that already applied them. Put the "
        "change in a new migration."
    )


def test_no_two_migrations_claim_one_number() -> None:
    """Two files with the same prefix are two migrations one number cannot name.

    **This does not ask for the existing collisions to be fixed, and that is the decision it
    encodes.** `037_bo_suggestion_provenance` / `037_document_index` and `043_session_listing` /
    `043_session_message_shape` are already merged and applied. The runner orders and records by
    *filename*, so nothing about them is broken — and renaming a merged migration is exactly the
    destructive edit `test_no_merged_migration_had_its_statements_changed` refuses, which would also
    leave every database that already recorded the old name applying the new one a second time.

    So the four are grandfathered by name, and the check exists for the *next* one — caught at
    review, when a rename is still free. The exemption list is what makes that honest: adding a
    fifth name to it is a visible act in a diff, where a check that simply excluded duplicates
    would let the number space keep colliding in silence.

    Grandfathered pairwise rather than by number, so a *third* file claiming `037` still fails.
    """
    grandfathered = {
        frozenset({"037_bo_suggestion_provenance.sql", "037_document_index.sql"}),
        frozenset({"043_session_listing.sql", "043_session_message_shape.sql"}),
    }
    by_number: dict[str, list[str]] = {}
    for path in _migration_files():
        number = path.name.split("_", 1)[0]
        assert number.isdigit(), f"{path.name} does not begin with a migration number"
        by_number.setdefault(number, []).append(path.name)

    collisions = {
        number: names
        for number, names in by_number.items()
        if len(names) > 1 and frozenset(names) not in grandfathered
    }
    assert not collisions, (
        f"two migrations claim one number: {collisions}. Renumber the new one before merging — "
        "after it is merged and applied, renaming it is a destructive edit and the number is "
        "permanently ambiguous in `schema_migrations`"
    )


def test_no_grandfathered_edit_outlives_its_reason() -> None:
    """Each grandfathered file must still exist and still *be* an edit.

    The sibling of `test_no_exemption_outlives_its_migration`, and it checks the stronger of the two
    properties an exemption can lose. A name that no longer matches a file is one failure; a name
    whose file no longer differs from its introducing commit is the quieter one — the exemption
    stops doing anything and stays granted, so the next edit to *that* file passes unexamined. Both
    are "a permission nobody can see spent".

    Asked through `_statements_changed_since_merge`, the same walk the check itself uses, so the
    exemption cannot be validated against a rule the check no longer applies.

    Skipped rather than failed on *any* truncated clone, and deliberately not behind the sibling's
    `compared < 30` conjunct — which is the calibration this test was actually failing on. A
    truncated clone still compares plenty of files, so that count stays well above 30; what it
    cannot see is an edit made *before* the graft boundary, because the "original" it diffs against
    is the grafted version. Both grandfathered edits are early migrations, so they compared equal to
    themselves and the check reported two live exemptions as stale — a red build about the clone
    depth rather than about the tree, which is exactly what the skip exists to prevent.
    """
    repo = _MIGRATIONS.parents[1]
    edited, compared = _statements_changed_since_merge()
    if _git(repo, "rev-parse", "--is-shallow-repository") == "true":
        pytest.skip(
            f"truncated history: {compared} migration(s) compared, but an edit made *before* the "
            "graft boundary is invisible — the pre-graft version is the grafted one, so the file "
            "compares equal to itself and a live exemption looks stale. Needs `fetch-depth: 0`."
        )

    on_disk = {path.name for path in _migration_files()}
    assert not (_GRANDFATHERED_EDITS - on_disk), (
        f"grandfathered edit(s) naming no migration: {sorted(_GRANDFATHERED_EDITS - on_disk)}"
    )
    assert not (_GRANDFATHERED_EDITS - set(edited)), (
        f"grandfathered edit(s) that no longer differ from the commit that introduced them: "
        f"{sorted(_GRANDFATHERED_EDITS - set(edited))}. The exemption has nothing left to permit, "
        "so delete it — leaving it granted means the next edit to that file goes unexamined."
    )


def test_truncating_history_never_raises_the_number_of_sound_comparisons(tmp_path: Path) -> None:
    """`compared` must fall when history is cut away, because that is the only reason to trust it.

    Both checks above abstain on `compared < 30` when git reports a shallow repository, and that
    threshold is only meaningful if the number actually tracks how much history is present. It did
    not. Measured on this repository before the graft exclusion was added: a 171-commit clone
    reported **47** comparisons against the **44** of a complete one, because truncation gives
    *more* files an earliest-commit that is not `HEAD` — the graft stands in for the real
    introduction. The skip therefore never fired above depth 1, and it had been unreachable since
    the tree crossed thirty migrations.

    What that cost was not hypothetical. On such a clone the immutability check compared every
    migration against its graft-boundary content and passed having verified nothing about any edit
    made earlier, while its sibling failed and told the reader to delete two exemptions that are
    live on full history — an instruction that would have removed the control it exists to keep.

    Asserted as an inequality rather than a fixed number so it keeps holding as migrations are
    added: cutting history away can only remove comparisons, never invent them.
    """
    repo = _MIGRATIONS.parents[1]
    if _git(repo, "rev-parse", "--is-shallow-repository") != "false":
        pytest.skip("this checkout is itself truncated, so there is no complete run to compare to")

    _, complete = _statements_changed_since_merge()

    clone = tmp_path / "truncated"
    cloned = subprocess.run(
        # Deeper than 1 on purpose: at depth 1 the graft *is* `HEAD`, which the walk already
        # excludes, so a depth-1 clone cannot tell the graft exclusion from its absence.
        ["git", "clone", "--quiet", "--depth", "50", f"file://{repo}", str(clone)],
        capture_output=True,
        text=True,
    )
    if cloned.returncode != 0:
        pytest.skip(f"could not build a truncated clone: {cloned.stderr.strip()}")

    _, truncated = _statements_changed_since_merge(clone / "infra" / "sql")

    assert truncated <= complete, (
        f"a 50-commit clone reported {truncated} sound comparisons against {complete} on the "
        f"complete history. `compared` is what both checks above read to decide whether they are "
        "looking at real history, so a number that rises as history is removed makes that decision "
        "backwards: the checks run, compare each migration against a graft-boundary version of "
        "itself, and report success having asked nothing."
    )
