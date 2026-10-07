"""The suite's own isolation: every Postgres DSN it can reach points at the throwaway schema.

`tests/conftest.py::isolated_postgres_schema` is what lets a destructive suite run against a
developer's database: nothing the run writes may land outside `tests.pg.TEST_SCHEMA`. An unlisted
DSN setting escapes that silently, so every `*_dsn` field must be named in
`_ISOLATED_DSN_SETTINGS`. Database-free, so it runs in every environment.
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

    The check runs over `Settings`' own field names, so a new setting cannot be missed.
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
    """The DSN `migrate()` resolves must name the schema the tests then read.

    Asserted through `migration_dsn()`, which returns `postgres_migration_dsn or postgres_dsn`:
    redirecting only the second would isolate the stores and run the DDL elsewhere.
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
    """An empty optional DSN is left empty, since it falls back to the already-redirected one.

    Rewriting `""` would invent a target rather than isolate one.
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
