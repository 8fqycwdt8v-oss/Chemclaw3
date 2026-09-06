"""The suite's own isolation: every Postgres DSN it can reach points at the throwaway schema.

`tests/conftest.py::isolated_postgres_schema` is the reason a destructive suite (`test_vector_index`
truncates `note_index`; a third of these tests issue `DELETE`s) can run against a developer's own
database at all. Its guarantee is one sentence — *nothing the run writes lands outside
`tests.pg.TEST_SCHEMA`* — and until this file nothing checked it.

It was false. The fixture redirected `postgres_dsn` and `session_store_dsn` by hand, a line each,
and `postgres_migration_dsn` — added later for the split-principal posture of
`D-2026-08-05-append-only-by-grant-not-by-contract`, and the setting `migrate()` and
`apply_grants()` actually resolve — had no line. Measured with one configured: the isolation schema
received **0** tables, the migration target received 44, and an "isolated" connection then read
`public`'s live rows through the search_path's second entry. Nothing failed; the run passed, on the
deployment's own data.

So this file guards the *list* rather than the fixture's behaviour on the settings that happen to
exist today: a fourth `*_dsn` field must be named in `_ISOLATED_DSN_SETTINGS` or fail here. An
allowlist somebody has to extend is worse than a rule that needs no maintenance and better than the
alternative that shipped, which was three lines and no rule — the two settings that were listed are
the two somebody thought of.

Deliberately database-free: this is a claim about which DSN a run resolves, not about what a
database does with it, so it runs in every environment including the one where the escape is worst
(a configured split principal, whose suite would otherwise never be run under isolation at all).
"""

import pytest

from chemclaw.core.config import settings
from chemclaw.core.migrate import migration_dsn
from tests.conftest import _ISOLATED_DSN_SETTINGS, redirect_dsns_to_test_schema
from tests.pg import TEST_SCHEMA

# A DSN no test may reach, spelled so a redirect that silently did nothing is visible in the
# assertion message rather than hidden behind the real one the session fixture already rewrote.
_BASE = "postgresql://someone:secret@db.invalid:5432/production"


def test_every_postgres_dsn_setting_is_redirected_into_the_isolation_schema() -> None:
    """A new `*_dsn` setting is named in the allowlist, or this fails naming it.

    The check is over `Settings`' own field names rather than over a list written twice, so the
    thing that has to be updated is the thing the escape came from. `postgres_migration_dsn`
    existed for weeks before anyone noticed it was unlisted, because nothing anywhere related the
    fixture's three lines to the config's three fields.
    """
    declared = {name for name in type(settings).model_fields if name.endswith("_dsn")}
    unlisted = sorted(declared - set(_ISOLATED_DSN_SETTINGS))
    assert not unlisted, (
        f"{unlisted} name Postgres databases and `tests/conftest.py::_ISOLATED_DSN_SETTINGS` does "
        "not redirect them, so a run with one configured writes outside the isolation schema — "
        "against whatever database it names, with the suite's own truncations and deletes"
    )
    stale = sorted(set(_ISOLATED_DSN_SETTINGS) - declared)
    assert not stale, (
        f"{stale} are redirected and are no longer `Settings` fields; a redirect of a setting "
        "nobody reads is a guarantee about nothing"
    )


def test_a_configured_migration_dsn_is_migrated_into_the_isolation_schema() -> None:
    """The setting `migrate()` resolves must name the schema the tests then read.

    Asserted through `migration_dsn()` rather than through the raw setting, because that resolver
    is the seam the escape ran through: `migrated_db_or_skip` calls `migrate()` with no argument,
    `migrate()` calls `migration_dsn()`, and `migration_dsn()` returns
    `postgres_migration_dsn or postgres_dsn` — so redirecting only the second isolates the stores
    and leaves the DDL somewhere else entirely.
    """
    patch = pytest.MonkeyPatch()
    try:
        # Named one by one rather than looped over `_ISOLATED_DSN_SETTINGS`, so this test is not
        # parameterised by the very list it exists to be independent of: driven off that tuple, a
        # setting dropped from it would stop being *configured* here and the assertion would pass.
        patch.setattr(settings, "postgres_dsn", _BASE)
        patch.setattr(settings, "postgres_migration_dsn", _BASE)
        patch.setattr(settings, "session_store_dsn", _BASE)
        redirect_dsns_to_test_schema(patch)
        for name in ("postgres_dsn", "postgres_migration_dsn", "session_store_dsn"):
            assert TEST_SCHEMA in getattr(settings, name), (
                f"{name} still points outside the isolation schema after the redirect"
            )
        assert TEST_SCHEMA in migration_dsn(), (
            "the migrations land outside the isolation schema, so it stays empty and every store "
            "resolves through the search_path to the real one"
        )
    finally:
        patch.undo()


def test_an_unconfigured_dsn_is_left_to_its_fallback() -> None:
    """Rewriting an empty optional DSN would invent a target rather than isolate one.

    `session_store_dsn` and `postgres_migration_dsn` both mean "fall back to `postgres_dsn`" while
    empty, and `postgres_dsn` is already redirected — so the correct handling of `""` is to leave
    it alone. Asserted because the loop's `if configured` is the one branch a reader is likeliest
    to simplify away.
    """
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(settings, "postgres_dsn", _BASE)
        patch.setattr(settings, "postgres_migration_dsn", "")
        patch.setattr(settings, "session_store_dsn", "")
        redirect_dsns_to_test_schema(patch)
        assert settings.postgres_migration_dsn == ""
        assert settings.session_store_dsn == ""
        assert TEST_SCHEMA in migration_dsn()
    finally:
        patch.undo()
