"""The grant matrix is derived from the code, not maintained beside it.

`infra/sql/grants/app_privileges.sql` says what the runtime role may write. This derives the same
answer from the SQL literals in `src/` and fails if the two disagree in **either** direction
(D-2026-08-05-append-only-by-grant-not-by-contract):

- A verb the code uses and the grant withholds is an outage — the application hits
  `InsufficientPrivilege` on a path nobody exercised before the deploy.
- A verb the grant allows and the code never uses is the boundary quietly widening back out. That
  direction is the one this file exists for: `audit_events` was called "append-only by contract"
  for a year while nothing enforced it, and a grant that drifts is how the contract stops being
  true again without anyone editing the sentence that claims it.

This is the same shape as `connector-validate` and `datasource-validate`: a declaration checked
against the live surface rather than a second definition of it. It needs no database — the check is
between two files in the repository, which is what makes it run in every environment.
"""

import ast
import re
from pathlib import Path

from chemclaw.durable.retention import _PRUNABLE

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "chemclaw"
_SQL = _ROOT / "infra" / "sql"
_GRANTS = _SQL / "grants" / "app_privileges.sql"

# Anything that reads like SQL. Docstrings and comments are excluded by construction: `ast` only
# yields string *constants*, and the SQL filter drops the prose ones — which matters, because this
# repository's docstrings discuss `DELETE` and `UPDATE` at length.
_LOOKS_LIKE_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|TRUNCATE)\b", re.I)

_INSERT = re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.I)
_UPDATE = re.compile(r"\bUPDATE\s+(\w+)\s+SET", re.I)
_DELETE = re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.I)
_UPSERT = re.compile(r"\bINSERT\s+INTO\s+(\w+).*?\bON CONFLICT\b.*?\bDO UPDATE\b", re.I)

# The two places a table name reaches a statement through a variable rather than a literal, so no
# scan of the SQL text can see them. Named here with their authority rather than hardcoded, so each
# stays true if its source changes.
#
# - The fingerprint stores build `INSERT INTO {table} ... ON CONFLICT DO UPDATE` in `__init__`
#   (`science/fingerprints/store.py`), with the table from `default_molecule_store` /
#   `default_reaction_store` / `science.labels.reactions.corpus_reactions`. `corpus_molecules` is
#   deliberately *not* here: it has an extra column, so `CorpusMolecules` writes its own literal
#   statement and the ordinary scan sees it.
# - The retention sweep builds `DELETE FROM {table}` over the closed `_PRUNABLE` map.
#
# - The LangGraph checkpointer and store issue their own SQL from inside the installed package, so
#   *no* first-party literal names them at all. Their verbs are **not** listed here: they are read
#   off those packages by `_upstream_verbs()` below, for the reason `_upstream_tables()` derives the
#   names rather than listing them. The DELETEs on the checkpoint tables and on
#   `store`/`store_vectors` are ours (retention by thread, erasure by subject) and *are* visible as
#   literals — they are folded in by the ordinary scan.
_DYNAMIC: dict[str, set[str]] = {
    "molecule_fingerprints": {"INSERT", "UPDATE"},
    "reaction_fingerprints": {"INSERT", "UPDATE"},
    "corpus_reactions": {"INSERT", "UPDATE"},
    # Every verb here is *added* to what the scans find (`note()` unions into a set), so the
    # retention sweep's DELETE and upstream's own matrix compose rather than one replacing the
    # other. An earlier `**` expansion overwrote instead, and it read as "the grant allows an
    # INSERT nobody performs".
    **{table: {"DELETE"} for table in _PRUNABLE},
}

# Written by the migrator alone: the ledger of its own work. A runtime credential that could write
# it could mark a migration applied that never ran, so `migrate.py`'s INSERT is deliberately not
# part of the application's matrix.
_MIGRATOR_ONLY = {"schema_migrations"}

# Modules whose statements belong to an **operator**, not to the running application, and are
# therefore not part of the runtime role's matrix. Named by path with the reason, in the shape
# `_DYNAMIC` uses, because the scan below reads SQL text and cannot see who runs it.
#
# `cli/rekey_campaigns.py` re-keys recorded BO campaigns after a change to how a campaign id is
# derived (D-2026-08-21). It is a schema-class operation that happens to need Python — the new id is
# computed from a stored `OptimizationProblem`, which SQL cannot do — and it runs beside
# `make db-migrate`, under the same principal that owns the tables.
#
# **Excluding it is the narrower answer, and the alternative is what makes it right.** Granting the
# runtime role what this module uses would mean DELETE on `bo_campaigns` and UPDATE on
# `bo_suggestions`, and the grant file withholds both deliberately: a campaign's suggestions are its
# history and "the sequence *is* the history" (031), so an UPDATE the chat service could issue is
# exactly the boundary this file exists to keep shut. A one-off run by an operator is not a reason
# to hand a chat turn that privilege for the rest of the deployment's life.
_ADMIN_ONLY_MODULES = {"cli/rekey_campaigns.py"}

# Modules that build a statement around an **interpolated** table name, mapped to every table they
# can target. `_joined` renders an interpolation as `?`, and every verb pattern below matches
# `(\w+)`, so `DELETE FROM {table}` is invisible to the scan — it is not merely unattributed, it
# does not register as a write at all.
#
# That is not hypothetical. A `DELETE FROM {table} WHERE source = ''` was added to the fingerprint
# store on this branch and the whole suite stayed green, while the runtime role holds INSERT and
# UPDATE on `reaction_fingerprints` and nothing else — so every ELN and corpus ingest would have
# failed `permission denied` on any deployment that runs `make db-grants`, and taken the upsert
# down with it, since both share one transaction. Nothing in this tree connects as the runtime
# role, so no test could have noticed downstream either.
_INTERPOLATED_TARGETS: dict[str, set[str]] = {
    "science/fingerprints/store.py": {"molecule_fingerprints", "reaction_fingerprints"},
}


def _upstream_tables() -> set[str]:
    """Every table LangGraph's `setup()` creates, derived from the installed distributions.

    These exist in the same database and are declared by no file in `infra/sql`, because the
    checkpointer and the store build their own schema lazily on first use. Derived rather than
    listed for the reason the rest of this module is derived: a table upstream adds in a minor bump
    must fail the grant check, not inherit `GRANT SELECT` and be discovered as a write outage.

    The two version ledgers the store writes are named here instead of parsed. Upstream spells them
    inline in `setup()` (`_get_version(cur, table="store_migrations")`) rather than in the
    `MIGRATIONS` lists, so there is no statement to read them out of — `tests/test_upstream_surface`
    pins the names so a rename turns red here rather than silently un-granting them.
    """
    from langgraph.checkpoint.postgres import base as checkpoint_base
    from langgraph.store.postgres import base as store_base

    created = {"store_migrations", "vector_migrations"}
    for statements in (
        checkpoint_base.MIGRATIONS,
        store_base.MIGRATIONS,
        store_base.VECTOR_MIGRATIONS,
    ):
        for statement in statements:
            if match := re.search(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", str(statement), re.I):
                created.add(match.group(1).lower())
    return created


def _upstream_modules() -> list[Path]:
    """The installed modules whose SQL the two `setup()`s and their writers actually issue.

    The same four `_upstream_tables()` reads its `MIGRATIONS` out of, plus each one's `aio` half,
    because the DDL lives in `base` and the DELETEs and the version-ledger INSERTs live beside the
    async savers this repository imports (`agent/checkpointer.py`, `agent/scratchpad.py`).

    `langgraph.checkpoint.postgres.shallow` is deliberately absent. `ShallowPostgresSaver` writes
    `checkpoint_blobs` with `DO UPDATE` where the saver this repository runs writes it with
    `DO NOTHING`, so scanning it would derive — and this file would then require the grant file to
    hand out — an UPDATE no process here performs. The basis is the code that runs, which is the
    same rule `_ADMIN_ONLY_MODULES` applies to `src/`.
    """
    from langgraph.checkpoint.postgres import aio as checkpoint_aio
    from langgraph.checkpoint.postgres import base as checkpoint_base
    from langgraph.store.postgres import aio as store_aio
    from langgraph.store.postgres import base as store_base

    return [
        Path(str(module.__file__))
        for module in (checkpoint_base, checkpoint_aio, store_base, store_aio)
    ]


def _verbs_in(paths: list[Path]) -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` for every write the SQL in `paths` performs.

    The same scan `verbs_the_code_uses()` runs over `src/`, factored out so it can be pointed at
    the installed distributions — and at a synthetic module, which is how the test proves the
    derivation reacts to upstream's statement changing rather than to this file being edited.
    """
    found: dict[str, set[str]] = {}
    for path in paths:
        for statement in _sql_literals(path):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                for match in pattern.finditer(statement):
                    found.setdefault(match.group(1).lower(), set()).add(verb)
            for match in _UPSERT.finditer(statement):
                found.setdefault(match.group(1).lower(), set()).add("UPDATE")
    return found


def _upstream_verbs() -> dict[str, set[str]]:
    """How LangGraph writes each table it creates, read off the distributions that issue the SQL.

    This was a hand-written map for as long as it existed, and the hazard is the one
    `_upstream_tables()` was derived to close, one column over. A minor bump that turns
    `checkpoint_blobs`' `ON CONFLICT … DO NOTHING` into a `DO UPDATE` needs UPDATE on that table;
    the map said INSERT and DELETE, the grant file agreed with the map, and every check in this
    repository would have stayed green until two writers raced on one key and met
    `permission denied`. Derived, an upstream bump moves this and the grant file has to move with
    it.

    Narrowed to the tables upstream creates, because these modules also name `schema_migrations`-
    shaped things this repository does not run and, more to the point, the tables are the closed
    set the grant file's `to_regclass` guards enumerate.
    """
    created = _upstream_tables()
    return {
        table: verbs for table, verbs in _verbs_in(_upstream_modules()).items() if table in created
    }


def _tables() -> set[str]:
    """Every table this database holds: the migrations' and LangGraph's alike.

    The upstream half used to be absent, and its absence was not cosmetic. `note()` below drops any
    table it does not recognise, so `_DYNAMIC`'s entry naming `checkpoints` was discarded before it
    could assert anything and this file reported "the code writes what the grant withholds: {}"
    while the grant withheld every write on five tables.
    """
    names: set[str] = set()
    for path in sorted(_SQL.glob("*.sql")):
        names |= {
            match.lower()
            for match in re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+(\w+)", path.read_text(encoding="utf-8"), re.I
            )
        }
    return names | _upstream_tables()


def _joined(node: ast.JoinedStr) -> str:
    """An f-string's literal parts, with each interpolation standing in as a placeholder.

    Needed because a statement containing **one** interpolation is a `JoinedStr`, and walking for
    `ast.Constant` alone sees its literal pieces as separate strings — which splits `INSERT INTO x`
    away from its `ON CONFLICT ... DO UPDATE` and silently loses the UPDATE the upsert requires.
    Both real cases are exactly this shape: `note_index` interpolates the embedding width into
    `::vector(N)`, and `job_records` interpolates its column list.
    """
    return "".join(
        part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else " ? "
        for part in node.values
    )


def _docstrings(tree: ast.Module) -> set[int]:
    """The `id()` of every docstring constant in `tree` — module, class, function and async.

    The header of this file says docstrings are "excluded by construction: `ast` only yields string
    *constants*". That sentence is about *comments*, and it was being read as covering docstrings,
    which are constants like any other. It held only because this repository's prose about SQL
    rarely also spells a statement — and it stops holding the moment a scan looks for DDL:
    `durable/retention.py`'s module docstring explains at length why a `CREATE INDEX` on the
    checkpoint tables is rejected, and the word "deleted" three lines up is enough to get the whole
    paragraph past `_LOOKS_LIKE_SQL`.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _sql_literals(path: Path) -> list[str]:
    """Every string in a module that looks like SQL, whitespace-flattened, docstrings excluded.

    Flattened because these statements are assembled from adjacent string literals across several
    lines, so `INSERT INTO x` and its `ON CONFLICT` clause are rarely on one line — Python has
    already concatenated the plain ones by the time `ast` sees them, and `_joined` does the same
    for the interpolated ones.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = _docstrings(tree)
    texts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose:
                texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            texts.append(_joined(node))
    return [re.sub(r"\s+", " ", text) for text in texts if _LOOKS_LIKE_SQL.search(text)]


def verbs_the_code_uses() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` for every write `src/` performs.

    An upsert counts as both: Postgres requires UPDATE on the target of
    `ON CONFLICT ... DO UPDATE`, and getting that wrong is an outage the first time two callers
    race on the same key rather than at deploy time.
    """
    known = _tables()
    used: dict[str, set[str]] = {}

    def note(table: str, verb: str) -> None:
        if table in known and table not in _MIGRATOR_ONLY:
            used.setdefault(table, set()).add(verb)

    for path in sorted(_SRC.rglob("*.py")):
        if path.relative_to(_SRC).as_posix() in _ADMIN_ONLY_MODULES:
            continue
        for statement in _sql_literals(path):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                for match in pattern.finditer(statement):
                    note(match.group(1).lower(), verb)
            for match in _UPSERT.finditer(statement):
                note(match.group(1).lower(), "UPDATE")
    for source in (_DYNAMIC, _upstream_verbs()):
        for table, verbs in source.items():
            for verb in verbs:
                note(table, verb)
    return used


def verbs_the_grant_allows() -> dict[str, set[str]]:
    """`{table: {INSERT, UPDATE, DELETE}}` as written in the grant file.

    `SELECT` is granted table-wide (`ON ALL TABLES`) and is deliberately not modelled: read is
    uniform, and the boundary worth checking is write.
    """
    # Quotes stripped and whitespace flattened first: a `GRANT` long enough to matter is written as
    # adjacent SQL string literals across several lines, so the statement Postgres assembles is not
    # the text any line-oriented match would see. (Written after a version of this test that read
    # only the single-line grants and reported every other table as ungranted.)
    text = re.sub(r"\s+", " ", _GRANTS.read_text(encoding="utf-8").replace("'", ""))
    allowed: dict[str, set[str]] = {}
    for verbs, tables in re.findall(
        r"GRANT\s+((?:INSERT|UPDATE|DELETE|,|\s)+?)\s+ON\s+((?:\w|,|\s)+?)\s+TO\b", text, re.I
    ):
        granted = {verb.strip().upper() for verb in verbs.split(",") if verb.strip()}
        if not granted <= {"INSERT", "UPDATE", "DELETE"}:
            continue  # `GRANT SELECT ON ALL TABLES` and friends — not part of the write matrix
        for table in (name.strip().lower() for name in tables.split(",")):
            if table:
                allowed.setdefault(table, set()).update(granted)
    return allowed


def test_an_upstream_upsert_that_starts_updating_is_seen(tmp_path: Path) -> None:
    """A `DO NOTHING` that becomes a `DO UPDATE` upstream fails here, not at a concurrent write.

    `_upstream_tables()` derives the table *names* from the installed `MIGRATIONS`, so a ninth
    table turns CI red. Nothing did that for the **verbs**: they were a hand-written map, and a
    minor bump that turned `checkpoint_blobs`' `ON CONFLICT … DO NOTHING` into a `DO UPDATE` would
    pass every check in this repository and then meet `permission denied for table
    checkpoint_blobs` the first time two writers raced on one key — the map says the grant file is
    right, and the grant file says the map is right.

    Driven against a synthetic module rather than the installed one, because the assertion is that
    the derivation *reacts*, and upstream's real statement is the thing that must not have to
    change for this to be provable.
    """
    module = tmp_path / "upstream_probe.py"
    do_nothing = (
        "UPSERT = '''\n"
        "    INSERT INTO checkpoint_blobs (thread_id, channel, version, blob)\n"
        "    VALUES (%s, %s, %s, %s)\n"
        "    ON CONFLICT (thread_id, channel, version) DO NOTHING\n"
        "'''\n"
    )
    module.write_text(do_nothing, encoding="utf-8")
    assert _verbs_in([module]) == {"checkpoint_blobs": {"INSERT"}}

    module.write_text(do_nothing.replace("DO NOTHING", "DO UPDATE SET blob = EXCLUDED.blob"))
    assert _verbs_in([module]) == {"checkpoint_blobs": {"INSERT", "UPDATE"}}


def test_the_upstream_verbs_are_read_off_the_distributions_that_issue_them() -> None:
    """Every table upstream creates is a table upstream's own SQL says how it writes.

    The two halves must be derived from the same place or the pair drifts: a table named by
    `_upstream_tables()` with no verb behind it is a grant nobody can check, and a verb attributed
    to a table upstream no longer creates is a privilege on nothing.
    """
    derived = _upstream_verbs()
    assert set(derived) == _upstream_tables(), sorted(set(derived) ^ _upstream_tables())
    assert all("INSERT" in verbs for verbs in derived.values()), derived


def test_a_write_against_an_interpolated_table_is_declared_for_every_table_it_can_hit() -> None:
    """The scan cannot read a table out of `DELETE FROM {table}`, so the verb must be declared.

    `_DYNAMIC` is hand-maintained, which makes it exactly as current as whoever last edited the
    module beside it — and a verb added to an interpolated statement changes no line in it. This
    closes that by reading the verbs back out of the module and requiring `_DYNAMIC` to allow each
    one for *every* table the module can target, since the scan cannot tell which it was.

    A module may legitimately issue fewer verbs than it is granted; the direction that matters is
    a verb the code performs and the grant withholds.
    """
    for module, tables in _INTERPOLATED_TARGETS.items():
        verbs: set[str] = set()
        for statement in _sql_literals(_SRC / module):
            for pattern, verb in ((_INSERT, "INSERT"), (_UPDATE, "UPDATE"), (_DELETE, "DELETE")):
                if pattern.search(statement.replace(" ? ", " x ")):
                    verbs.add(verb)
            if _UPSERT.search(statement.replace(" ? ", " x ")):
                verbs.add("UPDATE")
        for table in sorted(tables):
            missing = verbs - _DYNAMIC.get(table, set())
            assert not missing, (
                f"{module} performs {sorted(missing)} against an interpolated table name, and "
                f"`_DYNAMIC[{table!r}]` does not allow it. Either the grant file must grant it on "
                f"{table} and `_DYNAMIC` record it, or the statement must go. Note the scan cannot "
                "see which table an interpolated statement hit, which is why this is checked "
                "against every table the module can target."
            )


def test_the_grant_matches_the_writes_the_code_actually_performs() -> None:
    """Neither an outage waiting to happen nor a boundary that has quietly widened."""
    used = verbs_the_code_uses()
    allowed = verbs_the_grant_allows()

    missing = {
        table: sorted(verbs - allowed.get(table, set()))
        for table, verbs in used.items()
        if verbs - allowed.get(table, set())
    }
    assert not missing, (
        f"the code writes what the grant withholds: {missing}. The application would fail with "
        "InsufficientPrivilege on these paths under a split-principal deployment"
    )

    excess = {
        table: sorted(verbs - used.get(table, set()))
        for table, verbs in allowed.items()
        if verbs - used.get(table, set())
    }
    assert not excess, (
        f"the grant allows writes the code never performs: {excess}. A privilege nobody uses is a "
        "privilege that only matters when someone else uses it"
    )


def test_the_audit_trail_is_append_only_by_grant() -> None:
    """The claim `infra/sql/006` has made since it was written, now checked.

    "Append-only by contract" was enforced by nothing — no GRANT, no REVOKE, no trigger, no second
    role in any migration — while the same DSN that ran a chat turn could rewrite the trail
    recording it. A hash chain over the rows used to detect that after the fact; it is gone, so this
    grant is now the whole of the guarantee. Asserted separately from the derivation above because
    it must hold *whatever* the derivation concludes: if a future writer starts issuing
    `UPDATE audit_events`, the right outcome is this test failing, not the grant widening to match.
    """
    allowed = verbs_the_grant_allows()
    assert allowed.get("audit_events") == {"INSERT"}, (
        f"audit_events is granted {sorted(allowed.get('audit_events', set()))}; the trail's whole "
        "integrity claim is that the credential writing a row cannot rewrite it"
    )
    # `audit_anchors` was checked here too while the chain wrote it. The table survives the chain's
    # removal because the schema is forward-only, but nothing writes it, so the correct grant is
    # none at all — asserted rather than merely dropped, because a privilege silently reappearing on
    # a table nobody writes is exactly what the derivation above exists to catch.
    assert "audit_anchors" not in allowed, (
        f"audit_anchors is granted {sorted(allowed.get('audit_anchors', set()))} and no code "
        "writes it; the retired table should carry no privilege"
    )


def test_the_migration_ledger_is_never_granted_a_write_verb() -> None:
    """A role that can write the ledger can mark a migration applied that never ran.

    **A write verb, and the name of this test used to say more than it checks.** It read "never
    granted", over a derivation that models INSERT/UPDATE/DELETE and deliberately not SELECT — so
    it could not see, and was read as excluding, the blanket `GRANT SELECT ON ALL TABLES IN SCHEMA
    public` that does reach the ledger. Measured as the role: `SELECT` allowed, `42501` on INSERT
    and UPDATE. The read is intended and `app_privileges.sql` now says so; the live half of the
    claim is `tests/test_runtime_ddl_privilege.py`, which asks the ACL instead of this file's text.
    """
    assert "schema_migrations" not in verbs_the_grant_allows()


def test_the_grants_are_not_numbered_migrations() -> None:
    """They must re-apply on every deploy, which the tracked, run-once set cannot do.

    A grant is a reconciliation between a schema that keeps growing and a role that may be created
    at any time. As a numbered migration it would apply once: a deployment creating its runtime
    role afterwards would never be granted anything, and every table added by a later migration
    would ship ungranted and break on first use. The runner globs `infra/sql/*.sql`
    non-recursively, so the subdirectory is what keeps them apart.
    """
    from chemclaw.core.grants import grant_files
    from chemclaw.core.migrate import _read_sql_files

    assert _GRANTS.exists()
    assert _GRANTS.name not in _read_sql_files(), (
        "the grant file is inside the tracked migration set, so it would be applied exactly once"
    )
    assert [path.name for path in grant_files()] == [_GRANTS.name]


# The chart document that applies the reconciliation. Read as text rather than rendered, because
# what is asserted below is which points in a release's life the Job is attached to, and that is an
# annotation Helm reads off the manifest rather than anything a rendered value can show.
_MIGRATE_JOB = _ROOT / "deploy" / "helm" / "chemclaw" / "templates" / "migrate-job.yaml"


def test_a_rolled_back_release_re_applies_its_own_grant_file() -> None:
    """The reconciliation is a full restatement, so it **narrows**, and a rollback must undo that.

    `app_privileges.sql` states the whole matrix and revokes first, which is what makes a verb
    removed from the file a verb *revoked* from the role. Measured on `7654cfb0`, the commit that
    dropped `note_proposals` from the writer list: `note_proposals INSERT | t` under release N,
    `| f` after release N+1's hook, and `permission denied for table note_proposals` as the role.

    `helm rollback` restores the previous release's manifest and its image — and runs neither the
    `pre-upgrade` nor the `post-upgrade` hooks, because rollback has hook points of its own. So
    without `pre-rollback` here the older image comes back against the newer release's ACL and
    stays there until the next successful deploy, which is the one window in this whole file that
    is not bounded by a rollout. `pre-` rather than `post-`, deliberately: the restored pods must
    find their own ACL already in place, and the release that loses verbs in the meantime is the
    one being abandoned.

    Asserted here rather than in `tests/test_helm_chart.py` because it is a claim about the grant
    lifecycle — the same claim `test_the_grants_are_not_numbered_migrations` above makes about the
    other end of it — and it fails with the reason rather than as a diff in an annotation string.
    """
    migrate = _MIGRATE_JOB.read_text(encoding="utf-8").split("\n---\n")[0]
    # The guard that picks the right document, and it had to change with the thing it selects. It
    # used to look for `python -m chemclaw.core.grants` in the Job's own `command:`; W21 moved that
    # command into `deploy/entrypoint.sh` so the Job reaches the image ENTRYPOINT and is therefore
    # covered by the compiled egress layer, which a `command:` override bypasses entirely. So the
    # document is now identified by the component it dispatches, and the claim the old assertion
    # actually carried — that grants run after the migrations, in that order — is asserted below
    # against the script that now owns it. Selecting by `command:` again would pass while the
    # sequence had moved somewhere unexecuted, which is the shape this whole wave is about.
    assert re.search(r'value:\s*"?migrate"?', migrate), "wrong document: this one is not the migrate Job"
    entrypoint = (_ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    case = entrypoint.split("migrate)", 1)[-1].split(";;", 1)[0]
    assert "python -m chemclaw.core.migrate" in case and "python -m chemclaw.core.grants" in case, (
        "the migrate component no longer runs both halves, so a release applies schema without "
        "reconciling the runtime role's grants"
    )
    assert case.index("chemclaw.core.migrate") < case.index("chemclaw.core.grants"), (
        "grants run before the migrations that create the tables they name, and a grant applied "
        "before its table exists fails"
    )
    assert re.search(r'"helm\.sh/hook":[^\n]*\bpre-rollback\b', migrate), (
        "the Job that reconciles the grants does not run on rollback, so `helm rollback` restores "
        "the older image against the newer release's ACL — and the grant set contracts, so that "
        "ACL can be strictly narrower than the restored image needs"
    )


def test_the_chart_does_not_claim_the_grants_only_widen() -> None:
    """An absence test, because the claim was false in the file that made it.

    `migrate-job.yaml` justified its `pre-upgrade` hook with "the grants only widen", and
    `app_privileges.sql` advertises the opposite in the same tree: "re-running it after a verb is
    *removed* from the code narrows the grant". Both are true within one generation of the file and
    the pair is what makes a contraction land on the still-serving release. The sentence is
    corrected; this fails whoever writes it again.
    """
    for path in (_MIGRATE_JOB, _GRANTS):
        text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))
        assert "grants only widen" not in text, (
            f"{path.name} says the grants only widen. They do not: the file is a full restatement, "
            "so a verb removed from it is revoked from the role on the next deploy"
        )


# Modules whose SQL literals are the *migrator's*, not a runtime process's, plus the one module
# that only discusses DDL in prose. `core/migrate.py` is what `make db-migrate` runs under the
# owning principal; `core/grants.py` applies `app_privileges.sql` beside it. Anything else issuing
# DDL is a runtime process doing it, which is the thing the guard below is about.
_MIGRATOR_MODULES = {"core/migrate.py", "core/grants.py"}

# DDL a *runtime* process must never issue. `CREATE INDEX` is deliberately absent from the pattern's
# own vocabulary only in the sense that it is covered: the point is the schema-level right, and a
# first-party `CREATE INDEX` on a table it does not own needs the same conversation.
_DDL = re.compile(
    r"\b(CREATE|DROP)\s+(TABLE|INDEX|SCHEMA|EXTENSION|FUNCTION|VIEW|SEQUENCE)\b|\bALTER\s+TABLE\b",
    re.I,
)


def test_the_only_ddl_a_runtime_process_issues_is_upstreams_setup() -> None:
    """`D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns`'s premise, as a guard.

    That ADR keeps `GRANT CREATE ON SCHEMA public` deliberately, and its whole argument is that the
    only DDL a runtime process issues is upstream's `AsyncPostgresSaver.setup()` and
    `AsyncPostgresStore.setup()` — eight tables LangGraph keeps its own turn state in, migrated
    under an advisory lock by `agent/checkpointer.py::_setup_once`. The privilege is therefore
    "what upstream's checkpointer needs", which is a bounded thing to grant.

    **If a first-party module ever issues DDL, that stops being true**, and the privilege becomes
    "what this application does" — a different decision, taken by whoever writes the statement
    rather than by anyone reading the grant file. Measured when the ADR was written: no such
    literal existed outside the migrator. This is what makes the premise fail loudly instead of
    quietly ceasing to hold.

    It does **not** forbid the DDL. It forbids it arriving without the ADR being revisited, which
    is the same shape as `_ADMIN_ONLY_MODULES` one function up: a declared exception, not a ban.
    """
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative in _MIGRATOR_MODULES:
            continue
        for statement in _sql_literals(path):
            if _DDL.search(statement):
                offenders.append(f"{relative}: {statement[:90]}")
    assert offenders == [], (
        "a runtime module issues DDL, so the runtime role's `CREATE ON SCHEMA public` is no longer "
        "bounded by what upstream's checkpointer needs — revisit "
        "D-2026-09-07-the-app-is-its-own-migrator-for-the-tables-it-owns before adding it:\n  "
        + "\n  ".join(offenders)
    )
