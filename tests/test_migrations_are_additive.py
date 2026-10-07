"""The schema is forward-only and additive, enforced by checks that fail.

There is no down-path. For an additive schema the rollback is "deploy the previous image": old
code ignores new columns and tables, and the data stays. A scripted down migration is a data-loss
hazard one command away (e.g. on the append-only audit trail), and a never-run down-path drifts.
A wrong column is deprecated, not removed; a genuine removal is a deliberate, reviewed operation.

Two separate questions:

* destroying data is refused outright, with no exemption, since rollback cannot bring rows back;
* breaking the previous image's writes (e.g. `SET NOT NULL`, a replaced key that `ON CONFLICT`
  names) keeps the data but ends the rollback, and may be accepted in
  `_REVIEWED_ROLLBACK_BREAKS` with an ADR stating what an operator does instead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from chemclaw.core.migrate import _statements

_MIGRATIONS = Path(__file__).resolve().parents[1] / "infra" / "sql"
_DECISIONS = Path(__file__).resolve().parents[1] / "docs" / "decisions"
_RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "guides" / "runbook.md"

# How an identifier may be spelled: bare, schema-qualified, quoted, and after `ONLY`/`IF EXISTS`.
# `ALTER TABLE ONLY ...` is the form `pg_dump` emits. Written once and substituted into every
# position naming a table or column, so the two buckets police the same spellings.
_NAME = r"[\w.\"]+"  # `t`, `public.t`, `"t"`, `public."t"`
_TABLE = rf"(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_NAME}"  # ALTER TABLE [IF EXISTS] [ONLY] name

# Statements that destroy data, or the object holding it, matched at statement starts so `drop` in a
# comment or a `WHERE` predicate is not a false positive. `DROP CONSTRAINT` and `DROP INDEX` remove
# no row and belong to the second bucket.
_DESTROYS_DATA = re.compile(
    r"^\s*(?:"
    r"DROP\s+(?:TABLE|SCHEMA|TYPE|VIEW|DATABASE)"
    r"|TRUNCATE"
    r"|DELETE\s+FROM"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+(?:DROP\s+COLUMN|RENAME)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Statements that destroy no data but end the "deploy the previous image" rollback, because a write
# the previous image makes no longer succeeds.
#
# Unconditional breaks only: `SET NOT NULL` rejects every insert omitting the column, and a dropped
# or replaced key makes every `ON CONFLICT` naming it fail to plan. A `CHECK` or `CREATE UNIQUE
# INDEX` rejects only some rows and is tracked in `docs/planning/BACKLOG.md` instead.
# `ADD COLUMN ... NOT NULL` without a default is refused by Postgres on a non-empty table.
#
# `DROP INDEX` can over-flag (only a unique index is an `ON CONFLICT` arbiter); the answer is a
# reviewed exemption, not a looser pattern. `ALTER COLUMN ... TYPE` is here rather than in
# `_DESTROYS_DATA` because a pattern cannot tell a narrowing (which loses data irreversibly and may
# not be exempted) from a widening (which loses nothing).
_BREAKS_PREVIOUS_IMAGE = re.compile(
    r"^\s*(?:"
    rf"ALTER\s+TABLE\s+{_TABLE}\s+ALTER\s+(?:COLUMN\s+)?{_NAME}\s+SET\s+NOT\s+NULL"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+ALTER\s+(?:COLUMN\s+)?{_NAME}\s+TYPE\b"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+DROP\s+CONSTRAINT"
    rf"|ALTER\s+TABLE\s+{_TABLE}\s+ADD\s+(?:CONSTRAINT\s+{_NAME}\s+)?PRIMARY\s+KEY"
    r"|DROP\s+INDEX"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# `ADD CONSTRAINT` has no `IF NOT EXISTS` in Postgres, so it is the one additive-looking statement
# that is not re-runnable on its own. It is re-runnable when dropped `IF EXISTS` first, or added
# inside a `DO` block behind an `IF NOT EXISTS (SELECT ... FROM pg_constraint ...)` probe on the
# same table and name. The guarded form is better for a `NOT VALID` constraint: drop-then-add would
# discard an operator's `VALIDATE CONSTRAINT` and is itself a flagged `DROP CONSTRAINT`. Table names
# are compared bare, so a `pg_dump`-shaped spelling is the same table.
_ADD_CONSTRAINT = re.compile(
    rf"^\s*ALTER\s+TABLE\s+({_TABLE})\s+ADD\s+CONSTRAINT\s+({_NAME})",
    re.IGNORECASE | re.MULTILINE,
)
_DROP_CONSTRAINT_IF_EXISTS = re.compile(
    rf"^\s*ALTER\s+TABLE\s+({_TABLE})\s+DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+({_NAME})",
    re.IGNORECASE | re.MULTILINE,
)
# The guarded form: `IF NOT EXISTS (<probe>) THEN ... END IF`, the probe reading `pg_constraint` for
# one table and one name (names are unique per table, not per schema). The body is captured so only
# an add inside the guard is covered.
_GUARDED_BY_PG_CONSTRAINT = re.compile(
    r"IF\s+NOT\s+EXISTS\s*\((?P<probe>[^;]*?\bpg_constraint\b[^;]*?)\)\s*THEN\b"
    r"(?P<body>.*?)\bEND\s+IF\b",
    re.IGNORECASE | re.DOTALL,
)
_PROBE_TABLE = re.compile(rf"\bconrelid\s*=\s*'({_NAME})'::regclass", re.IGNORECASE)
_PROBE_NAME = re.compile(r"\bconname\s*=\s*'(\w+)'", re.IGNORECASE)


def _bare(identifier: str) -> str:
    """`ONLY public."t"` -> `t`: the name without a modifier, a schema qualifier or quotes."""
    return identifier.strip().rsplit(None, 1)[-1].replace('"', "").rsplit(".", 1)[-1].lower()


def _guarded_spans(sql: str) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """`(table, constraint)` -> the character spans a `pg_constraint` probe for exactly it guards.

    A probe that does not name both the table and the constraint guards nothing, for the reason
    `_GUARDED_BY_PG_CONSTRAINT` gives.
    """
    spans: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for match in _GUARDED_BY_PG_CONSTRAINT.finditer(sql):
        table = _PROBE_TABLE.search(match.group("probe"))
        name = _PROBE_NAME.search(match.group("probe"))
        if table is None or name is None:
            continue
        key = (_bare(table.group(1)), _bare(name.group(1)))
        spans.setdefault(key, []).append((match.start("body"), match.end("body")))
    return spans


def _constraints_re_added_without_a_drop(sql: str) -> tuple[str, ...]:
    """`table.constraint` for every `ADD CONSTRAINT` neither dropped `IF EXISTS` first nor guarded.

    Position-aware: a drop after the add does not help, and a guard covers only the add inside its
    own `THEN ... END IF`. Compared on bare names, so every accepted spelling resolves to one
    object.
    """
    dropped: dict[tuple[str, str], int] = {}
    for match in _DROP_CONSTRAINT_IF_EXISTS.finditer(sql):
        dropped.setdefault((_bare(match.group(1)), _bare(match.group(2))), match.start())
    guarded = _guarded_spans(sql)
    return tuple(
        f"{table}.{constraint}"
        for table, constraint, at in (
            (_bare(m.group(1)), _bare(m.group(2)), m.start()) for m in _ADD_CONSTRAINT.finditer(sql)
        )
        if dropped.get((table, constraint), at + 1) > at
        and not any(start <= at < end for start, end in guarded.get((table, constraint), []))
    )


# Merged migrations that are not re-runnable, each with the recipe an operator runs before a replay
# (the file is immutable, so a preparatory statement is the only fix). Also reported in the
# runbook. `(ADR, the flagged objects, the recipe)`, exact rather than per-file.
_REVIEWED_REPLAY_BREAKS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "046_review_hardening_indexes.sql": (
        "D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated",
        ("session_messages.session_messages_shape_known",),
        # The constraint is `NOT VALID`, so re-adding it costs no scan; dropping it first is free
        # and the file then replays whole.
        "ALTER TABLE session_messages DROP CONSTRAINT IF EXISTS session_messages_shape_known;",
    ),
    "058_note_proposal_superseded.sql": (
        "D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated",
        ("note_proposals.note_proposals_state_known",),
        # 058 drops without `IF EXISTS`, so it fails on a database built without that constraint.
        # The recipe re-adds the post-058 form unconditionally (identical to what the file re-adds),
        # so the operator need not work out which case they are in, and existing rows already
        # satisfy it.
        "ALTER TABLE note_proposals DROP CONSTRAINT IF EXISTS note_proposals_state_known; "
        "ALTER TABLE note_proposals ADD CONSTRAINT note_proposals_state_known "
        "CHECK (state IN ('open', 'merged', 'rejected', 'failed', 'superseded'));",
    ),
}

# Migrations accepted as ending the previous-image rollback, each mapped to the exact statement
# prefixes `_BREAKS_PREVIOUS_IMAGE` matches and to the ADR saying what an operator does instead.
# Exact, so an added break in an exempted file still fails.
_REVIEWED_ROLLBACK_BREAKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "117_exhibit_html_kind.sql": (
        # 116's shape again: a widening of the same kind CHECK. The previous image loses reading
        # an `html` revision (and a revision that binds a value), which the runbook says.
        "D-2026-10-03-model-written-html-runs-in-an-opaque-origin-the-backend-never-serves",
        ("ALTER TABLE session_exhibits DROP CONSTRAINT",),
    ),
    "116_exhibit_geometry_kind.sql": (
        # 058's shape: the drop-and-re-add *widens* the kind CHECK, and the previous image writes
        # only kinds the new constraint still admits. What it loses is reading a `geometry`
        # revision, which the runbook's rollback table states and no statement pattern carries.
        "D-2026-10-03-a-geometry-artefact-cites-the-calc-store-it-does-not-copy",
        ("ALTER TABLE session_exhibits DROP CONSTRAINT",),
    ),
    "106_drop_the_index_nobodys_query_uses.sql": (
        # An over-flag: a plain GIN index on `turn_costs.skills_loaded` that no `ON CONFLICT` names
        # and nothing reads through. The previous image writes as before, so the rollback is still
        # "deploy the previous image"; re-running `105` restores the index.
        "D-2026-09-18-a-control-that-names-a-module-is-a-claim-about-where-somebody-put-the-code",
        ("DROP INDEX",),
    ),
    "094_fingerprint_definition_identity.sql": (
        # The definition joins the key on all three fingerprint tables, so a superseded generation
        # is shelved rather than deleted. This genuinely stops the previous image writing: its `ON
        # CONFLICT` no longer plans against the widened key. Roll forward, or re-add the old key by
        # hand.
        "D-2026-09-09-a-definition-change-shelves-a-row-it-does-not-delete",
        (
            "ALTER TABLE molecule_fingerprints DROP CONSTRAINT",
            "ALTER TABLE molecule_fingerprints ADD PRIMARY KEY",
            "ALTER TABLE reaction_fingerprints DROP CONSTRAINT",
            "ALTER TABLE reaction_fingerprints ADD PRIMARY KEY",
            "ALTER TABLE corpus_reactions DROP CONSTRAINT",
            "ALTER TABLE corpus_reactions ADD PRIMARY KEY",
        ),
    ),
    "093_measurement_source.sql": (
        # Keys the table by its source, like 056 and 063. Nothing is destroyed by restoring the
        # previous image, but rows written with a `source` other than `chemist-reported` are
        # unreachable to its two-column lookup. Run the migration forward again.
        "D-2026-09-09-a-measurement-is-keyed-by-who-measured-it",
        (
            "ALTER TABLE measurements DROP CONSTRAINT",
            "ALTER TABLE measurements ADD PRIMARY KEY",
        ),
    ),
    "088_turn_cost_identity.sql": (
        "D-2026-09-06-an-id-a-caller-chooses-is-not-a-key",
        (
            "ALTER TABLE turn_costs DROP CONSTRAINT",
            "ALTER TABLE turn_costs ADD PRIMARY KEY",
        ),
    ),
    "058_note_proposal_superseded.sql": (
        # Does not end the rollback in practice: the drop-and-re-add widens the state CHECK, and the
        # previous image writes only states still allowed. Listed because the guard matches the
        # text.
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
    "091_reaction_label_confidence_precision.sql": (
        # Does not end the rollback in practice: it widens `confidence` from `REAL` to `DOUBLE
        # PRECISION`, every value survives, and the previous image writes a float as before. Listed
        # because no pattern can read a conversion's direction; a narrowing could not be exempted
        # here.
        "D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated",
        ("ALTER TABLE reaction_labels ALTER COLUMN confidence TYPE",),
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

# Migrations that end the previous-image rollback for a reason no statement shape carries, found by
# review rather than by a pattern. Kept separate from `_REVIEWED_ROLLBACK_BREAKS`, whose rows are
# judgements about statements a regex re-finds.
#
# 089 adds one nullable column, but `claimed_at` is a mutual exclusion: a previous-image pod ignores
# the lease and re-claims rows a new pod is delivering, spending the attempt budget twice. This
# register does not catch the next such case; it records the judgement where an operator planning a
# rollback reads it.
_REVIEWED_SEMANTIC_BREAKS: dict[str, tuple[str, str]] = {
    "092_session_owners_updated_at.sql": (
        "D-2026-09-09-a-sort-key-a-page-cannot-prune-is-a-scan",
        "`updated_at` is the sidebar's sort key. It does not break a rollback — the pre-092 image "
        "derives the order and ignores the column — but it does not *maintain* it either, so a "
        "session taking its first turn during the rollback window comes back with the column NULL "
        "and is missing from the listing until it is spoken in again. The migration's backfill, "
        "re-run by hand, restores it.",
    ),
    "089_result_publication_lease.sql": (
        "D-2026-09-09-a-pattern-that-enumerates-covers-what-it-enumerated",
        "`claimed_at` is a delivery lease. A pre-089 pod's claim ignores it and re-claims a leased "
        "row, spending an attempt on a delivery already in flight.",
    ),
}

# The two migrations whose statements were edited before the immutability check could run, kept as
# named exemptions. `004_fingerprint_definition.sql` documents the edit: a column was added to both
# `CREATE TABLE`s and an `ALTER` written for existing databases.
#
# Reverting is worse: every database created since recorded the current checksum, so restoring the
# old statements would make `make db-migrate` refuse everywhere, while no supported database holds
# the pre-edit version. `test_no_grandfathered_edit_outlives_its_reason` checks each entry is still
# an edit.
_GRANDFATHERED_EDITS: frozenset[str] = frozenset(
    {"002_molecule_fingerprints.sql", "003_reaction_fingerprints.sql"}
)

# Comment stripping is the runner's own `_statements`, imported rather than reimplemented: comments
# discussing what not to drop must not fail the scan, and the runner's drift checksum uses the same
# reduction.


def _sql(path: Path) -> str:
    """The migration's SQL with its comment lines removed, as the runner sees it."""
    return _statements(path.read_text(encoding="utf-8"))


def _migration_files() -> list[Path]:
    """Every migration, in the order the runner applies them (filename order)."""
    return sorted(_MIGRATIONS.glob("*.sql"))


def test_there_are_migrations_to_check() -> None:
    """There are migrations to check; every other assertion is vacuous against an empty glob."""
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
    """A migration leaves the previous image able to write, unless reviewed.

    A migration can keep every row and still end the rollback because the previous image's `INSERT`
    no longer satisfies the table. An exemption must match the flagged statements exactly, so a
    stale entry fails as loudly as a new break, and its ADR must exist and name the migration.
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
        # A type conversion, in both directions and in the spellings the tree does not use. The
        # bucket is the same either way — narrowing is refused by review, not by the pattern, which
        # cannot see which way the conversion goes.
        ("ALTER TABLE t ALTER COLUMN c TYPE REAL;", 0, 1),
        ("ALTER TABLE t ALTER COLUMN c TYPE DOUBLE PRECISION;", 0, 1),
        ("ALTER TABLE ONLY public.t ALTER c TYPE NUMERIC(4,2);", 0, 1),
        ("ALTER TABLE t ALTER COLUMN c TYPE TEXT USING c::text;", 0, 1),
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
    """Each bucket matches its own statements and not the other's.

    Asked of synthetic SQL, since the tree holds one example of each: a `DROP CONSTRAINT` that
    destroys nothing, a `SET NOT NULL` that destroys nothing but ends the rollback, and an additive
    `ADD COLUMN ... NOT NULL DEFAULT` that neither bucket may claim.
    """
    assert len(_DESTROYS_DATA.findall(statement)) == destroys
    assert len(_BREAKS_PREVIOUS_IMAGE.findall(statement)) == breaks


def test_no_exemption_outlives_its_migration() -> None:
    """Every exemption names a migration that exists.

    The check is parametrised over files on disk, so an entry for a missing file is never consulted
    and could drift into a blanket permission.
    """
    on_disk = {p.name for p in _migration_files()}
    registered = (
        set(_REVIEWED_ROLLBACK_BREAKS)
        | set(_REVIEWED_REPLAY_BREAKS)
        | set(_REVIEWED_SEMANTIC_BREAKS)
    )
    orphaned = sorted(registered - on_disk)
    assert not orphaned, f"reviewed exemption(s) naming no migration: {orphaned}"


def test_every_reviewed_break_tells_the_operator_what_it_costs() -> None:
    """Every reviewed break is named in the runbook's rollback-consequences table.

    The registers record that a break was examined; the runbook says what the operator loses, and is
    what they read. The register is the authority and the runbook is checked against it, one
    direction only: the table may also list breaks the patterns catch without review.
    """
    section = _runbook_rollback_section()
    registered = set(_REVIEWED_ROLLBACK_BREAKS) | set(_REVIEWED_SEMANTIC_BREAKS)
    missing = sorted(name for name in registered if name[:3] not in section)
    assert not missing, (
        f"reviewed break(s) the rollback runbook never names: {missing}. A reviewed exemption "
        "records that somebody looked; it does not tell an operator what the restored image "
        f"loses. Add a row to {_RUNBOOK.name}'s 'What is still broken after a successful "
        "rollback' table, or say there why this one costs nothing."
    )


def _runbook_rollback_section() -> str:
    """The runbook prose between the rollback-consequences heading and the next one.

    Scoped because migration numbers appear elsewhere in the runbook (e.g. the replay recipe).
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index("**What is still broken after a successful rollback**")
    end = text.index("**What no rollback undoes**", start)
    return text[start:end]


def test_a_judged_break_is_one_no_pattern_could_have_found() -> None:
    """`_REVIEWED_SEMANTIC_BREAKS` holds only breaks no pattern could have found.

    The two registers are disjoint, and each member here is genuinely unflagged; if a widened
    pattern reaches one, it moves to `_REVIEWED_ROLLBACK_BREAKS`. Each row needs an ADR that exists
    and names the migration.
    """
    both = sorted(set(_REVIEWED_SEMANTIC_BREAKS) & set(_REVIEWED_ROLLBACK_BREAKS))
    assert not both, f"migration(s) in two rollback-break registers at once: {both}"
    for name, (adr, reason) in _REVIEWED_SEMANTIC_BREAKS.items():
        path = _MIGRATIONS / name
        assert not _BREAKS_PREVIOUS_IMAGE.findall(_sql(path)), (
            f"{name} is now flagged by `_BREAKS_PREVIOUS_IMAGE`, so it is a reviewed *statement* "
            "rather than a judgement — move it to `_REVIEWED_ROLLBACK_BREAKS` with the statements "
            "named, where a later edit adding a second break still fails."
        )
        assert reason.strip(), f"{name} is listed as a judged break with no reading recorded"
        assert (_DECISIONS / f"{adr}.md").is_file(), f"{name} is exempted by a missing ADR {adr}"
        assert name in (_DECISIONS / f"{adr}.md").read_text(encoding="utf-8"), (
            f"{adr} records {name} as a judged rollback break without naming it."
        )


@pytest.mark.parametrize(
    ("sql", "flagged"),
    [
        # The shape 046 has: an add with no drop at all.
        ("ALTER TABLE t ADD CONSTRAINT c CHECK (x);", ("t.c",)),
        # The shape 058 has: a drop that omits `IF EXISTS`, which replays against a restore and
        # fails against a database that does not carry the constraint.
        (
            "ALTER TABLE t DROP CONSTRAINT c;\nALTER TABLE t ADD CONSTRAINT c CHECK (x);",
            ("t.c",),
        ),
        # The re-runnable form, including across the line break every migration here writes it on.
        (
            "ALTER TABLE t DROP CONSTRAINT IF EXISTS c;\nALTER TABLE t ADD CONSTRAINT c CHECK (x);",
            (),
        ),
        (
            "ALTER TABLE t DROP CONSTRAINT IF EXISTS c;\n"
            "ALTER TABLE t\n  ADD CONSTRAINT c CHECK (x);",
            (),
        ),
        # The spellings of a table name Postgres accepts, on both halves — a rule that reads only
        # the bare identifier would call the `pg_dump` form a different table and flag a sound file.
        (
            'ALTER TABLE ONLY public."t" DROP CONSTRAINT IF EXISTS c;\n'
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);",
            (),
        ),
        (
            "ALTER TABLE IF EXISTS t DROP CONSTRAINT IF EXISTS c;\n"
            "ALTER TABLE public.t ADD CONSTRAINT c CHECK (x);",
            (),
        ),
        # Order is the property, not co-occurrence: dropping afterwards makes the file worse.
        (
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);\nALTER TABLE t DROP CONSTRAINT IF EXISTS c;",
            ("t.c",),
        ),
        # A different constraint's drop does not cover this one.
        (
            "ALTER TABLE t DROP CONSTRAINT IF EXISTS other;\n"
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);",
            ("t.c",),
        ),
        # A constraint declared inline in a `CREATE TABLE` is created with the table, so it is
        # covered by `IF NOT EXISTS` and is not an `ADD`.
        ("CREATE TABLE IF NOT EXISTS t (a TEXT, CONSTRAINT c CHECK (a <> ''));", ()),
        # The guarded form `108` uses: the add sits inside a probe naming this table and this name.
        (
            "DO $$\nBEGIN\n    IF NOT EXISTS (\n        SELECT 1 FROM pg_constraint\n"
            "         WHERE conrelid = 'public.t'::regclass AND conname = 'c'\n    ) THEN\n"
            "        ALTER TABLE ONLY t\n            ADD CONSTRAINT c CHECK (x) NOT VALID;\n"
            "    END IF;\nEND\n$$;",
            (),
        ),
        # A probe on the name alone guards nothing: names are unique per table, not per schema, so
        # another table's `c` would make it skip this add.
        (
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'c') THEN\n"
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);\nEND IF; END $$;",
            ("t.c",),
        ),
        # A probe for another table's constraint of the same name does not cover this one.
        (
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint\n"
            "WHERE conrelid = 'u'::regclass AND conname = 'c') THEN\n"
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);\nEND IF; END $$;",
            ("t.c",),
        ),
        # A guard covers the add inside it, not one that follows its `END IF`.
        (
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint\n"
            "WHERE conrelid = 't'::regclass AND conname = 'c') THEN NULL; END IF; END $$;\n"
            "ALTER TABLE t ADD CONSTRAINT c CHECK (x);",
            ("t.c",),
        ),
    ],
)
def test_the_replay_rule_reads_the_object_not_the_spelling(
    sql: str, flagged: tuple[str, ...]
) -> None:
    """The replay rule reads the object, not the spelling.

    Synthetic SQL, since the tree's two examples are both exempt: a sound drop-then-add, the same
    across a line break and a schema qualifier, and a drop placed after the add, which does not
    help.
    """
    assert _constraints_re_added_without_a_drop(sql) == flagged


def test_every_migration_is_re_runnable() -> None:
    """Each file creates only with `IF NOT EXISTS`.

    Normally a file applies once, but a restored database whose ledger is older than its tables, or
    a hand-built one, needs every statement to be replayable.
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


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_a_re_added_constraint_is_dropped_first(path: Path) -> None:
    """A re-added constraint is dropped `IF EXISTS` first or guarded.

    A constraint has no `IF NOT EXISTS`, and the run is one transaction, so on a restored database
    the first unguarded `ADD CONSTRAINT` aborts everything. Exempted exactly, with a recipe (the
    statement an operator runs before replay) rather than a rollback procedure, since merged files
    are immutable.
    """
    found = _constraints_re_added_without_a_drop(_sql(path))
    reviewed = _REVIEWED_REPLAY_BREAKS.get(path.name)
    if reviewed is None:
        assert not found, (
            f"{path.name} adds constraint(s) {list(found)} it does not first drop `IF EXISTS`, so "
            "replaying it against a database that already carries them aborts the migration run — "
            "the recovery `test_every_migration_is_re_runnable` exists for. Write `ALTER TABLE t "
            "DROP CONSTRAINT IF EXISTS c;` above the `ADD`, or — if the file is already merged and "
            "so immutable — add it to `_REVIEWED_REPLAY_BREAKS` with the recipe an operator runs "
            "instead, and put that recipe in the runbook."
        )
        return
    adr, expected, recipe = reviewed
    assert found == expected, (
        f"{path.name}'s reviewed replay exemption no longer describes it: flagged {list(found)}, "
        f"reviewed {list(expected)}. Re-review it and update {adr}."
    )
    assert recipe.strip().endswith(";"), f"{path.name}'s recipe is not a statement an operator runs"
    assert (_DECISIONS / f"{adr}.md").is_file(), f"{path.name} is exempted by a missing ADR {adr}"
    assert path.name in (_DECISIONS / f"{adr}.md").read_text(encoding="utf-8"), (
        f"{adr} grants {path.name} an exemption without naming it, so the recipe it is supposed to "
        "carry cannot be found from the migration."
    )


def _git(repo: Path, *args: str) -> str:
    """`git` in `repo`, stdout only. A failure is the empty string — every caller treats it so."""
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


def _shallow_grafts(repo: Path) -> frozenset[str]:
    """The commits git reports as parentless only because the clone was truncated there.

    `git log --diff-filter=A` names a graft as the commit that added every file introduced beyond
    the boundary, which would make the immutability comparison vacuous. Read via `rev-parse
    --git-path shallow`, so worktrees and relocated `$GIT_DIR` work. Empty on a full clone, where
    the true root commit remains a legitimate introducing commit.
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

    Shared by the immutability check and its exemption's staleness check so both ask git the same
    question. `compared` counts only comparisons that span a commit: files introduced by `HEAD` or
    by a shallow graft have no earlier version available and are excluded at the source.
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
    """A merged migration's statements are immutable; its comments are not.

    `core/migrate.py` checksums statements (`_statements`), and this asks the same question of git
    history: what the file contained in the commit that introduced it. Uncommitted files are
    skipped.

    Missing or truncated history would make the check pass having compared nothing (a depth-1 clone
    compares every file with itself), so the count is of comparisons spanning a commit, and a floor
    on it is asserted. Where git reports truncated history the floor becomes a skip naming the fix,
    because that is a CI setting, not a defect; CI sets `fetch-depth: 0`.
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
    """No two migrations claim one number, beyond the grandfathered pairs.

    `037_*` and `043_*` pairs are merged and applied; the runner orders and records by filename, so
    they work, and renaming them would re-apply them everywhere. They are grandfathered by name, so
    a third file claiming either number still fails and new collisions are caught at review.
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
    """Each grandfathered file still exists and is still an edit.

    An exemption whose file no longer differs would keep granting permission for the next edit.
    Asked through `_statements_changed_since_merge`, the check's own walk. Skipped on any truncated
    clone, since edits before the graft boundary are invisible there and would read as stale.
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
    """Truncating history never raises the number of sound comparisons.

    The checks above abstain on a low count only when history is shallow, which is meaningful only
    if the count falls as history is cut. Asserted as an inequality, so it holds as migrations are
    added.
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
