"""The warehouse's own trust boundary: what it writes into SQL, into a path, and how it fails.

Three findings from the security sweep's data-plane lane, all in the engine that reads a site's ELN
out of its own warehouse (D-2026-08-04-the-schema-is-a-file). Two are boundary violations the
module's own prose denied; the third is a classification that turned a typo'd password into a
retry storm.

**The Snowflake module had no executed test at all** — the sweep's finding, and the reason
`_connect`'s behaviour could be wrong in the first place. It imports a client this repository does
not depend on, so the tests here drive it through the `_client()` seam with a fake, which is the
same shape the engine was proven with.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from chemclaw.ingest.eln.warehouse.binding import BindingError, VectorBinding
from chemclaw.ingest.eln.warehouse.driver import WarehouseQueryError
from chemclaw.kg.note import is_note_slug, note_id_for_reaction, note_relative_path


def _vector(**overrides: Any) -> VectorBinding:
    """A minimal valid vector binding, with the field under test overridden."""
    fields: dict[str, Any] = {
        "relation": "REACTIONS",
        "key": "ID",
        "vector_column": "EMBEDDING",
        "content_columns": ["NOTES"],
    }
    fields.update(overrides)
    return VectorBinding(**fields)


def test_a_server_embed_function_must_be_an_identifier() -> None:
    """The one value the module interpolated into SQL without checking it.

    `sql.py`'s header states the invariant — "Every value is bound; only checked identifiers are
    written" — and this made it false: `server_embed_function` is written straight into the
    statement text, so a binding could carry `(SELECT secret FROM credentials) -- ` and it would be
    executed. Reproduced before the fix by constructing exactly that.

    Distinct from the `where:` trust boundary, which is deliberately raw SQL an operator writes.
    The difference is that `where:` announces itself as such and this announced itself as an
    identifier.
    """
    with pytest.raises((BindingError, ValueError)):
        _vector(embedding="server", server_embed_function="(SELECT secret FROM credentials) -- ")


@pytest.mark.parametrize(
    "function", ["EMBED", "SNOWFLAKE.CORTEX.EMBED_TEXT_768", "my_db.public.embed_v2"]
)
def test_a_real_embedder_name_is_still_accepted(function: str) -> None:
    """The check must not cost the feature: a dotted function name is what a site actually binds."""
    binding = _vector(embedding="server", server_embed_function=function)
    assert binding.server_embed_function == function


def test_a_warehouse_key_cannot_reach_outside_the_knowledge_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row key became a filesystem path with no validation.

    The retriever asks "has this row already been merged as a note?" by `stat`-ing a path built
    from the raw warehouse key. Measured before the fix: a key of `../../../../etc/passwd` resolved
    to `/var/lib/chemclaw/note-repo/etc/passwd.md` — outside the knowledge tree entirely.

    Only a `stat`, so the consequence is an existence probe rather than a read — and a way to make
    a genuine reaction look already-ingested and disappear from retrieval, which is the quieter
    half.

    **The escape is conditional, and the condition is set up here rather than assumed.** The first
    version of this test asserted `False` against paths that do not exist, which proves nothing:
    "refused" and "stat'd and not found" are the same answer. The second created a file at the
    `.resolve()` target and still passed under mutation — because `.resolve()` is *lexical* while
    `is_file()` asks the OS, and the OS will not walk `..` through a component that does not exist.

    Measured, finally, on the primitive the code actually calls: with a real directory under
    `knowledge/reaction/` for the traversal to stand on, `is_file()` returns **True** for a file
    outside the knowledge tree. That is the reachable form of this finding, and it is why the row
    is [L] rather than higher — it needs a directory there, and notes are files.
    """
    from chemclaw.ingest.eln.warehouse.retriever import _is_merged_note

    knowledge = tmp_path / "note-repo" / "knowledge"
    knowledge.mkdir(parents=True)
    # `knowledge_path` is a derived property, so the two fields it is composed from are what a test
    # sets — which is also what a deployment sets.
    monkeypatch.setattr("chemclaw.core.config.settings.note_repo_dir", str(tmp_path / "note-repo"))
    monkeypatch.setattr("chemclaw.core.config.settings.knowledge_dir", "knowledge")

    # The stepping stone the OS needs to walk `..` at all, plus the file outside the tree that a
    # successful traversal would find. Both asserted to exist first, so a pass here cannot be the
    # accident of a missing file.
    (knowledge / "reaction" / "reaction-x").mkdir(parents=True)
    outside = tmp_path / "note-repo" / "secret.md"
    outside.write_text("---\ntype: reaction\n---\n", encoding="utf-8")
    hostile = "x/../../../secret"
    assert (knowledge / f"reaction/reaction-{hostile}.md").is_file(), (
        "the traversal does not reach the probe file, so this case would prove nothing"
    )

    assert _is_merged_note(hostile) is False, "a key walked out of the knowledge tree"
    for other in ("../../../../etc/passwd", "../../.git/config", "a/../../b"):
        assert _is_merged_note(other) is False


def test_the_path_builder_refuses_a_segment_no_note_could_have() -> None:
    """The barrier below `Note`'s own, for callers that build a path without building a note.

    `Note` validates `id` and `type` as slugs, which covers every *write*. The warehouse retriever
    bypassed that by constructing a path directly — so the same rule now guards the path builder,
    which is the choke point both the PR-gate and every reader share.
    """
    assert note_relative_path("reaction", "reaction-e-1041") == "reaction/reaction-e-1041.md"
    for bad_id in ("../escape", "a/b", ".hidden"):
        with pytest.raises(ValueError, match="plain slug"):
            note_relative_path("reaction", bad_id)
    with pytest.raises(ValueError, match="plain slug"):
        note_relative_path("../etc", "reaction-e-1041")


def test_the_predicate_and_the_raise_agree() -> None:
    """One rule, two shapes: the retriever wants an answer, the path builder owes an exception."""
    for value in ("reaction-e-1041", "compound-thf", "bo-reizman_suzuki-ab12"):
        assert is_note_slug(value)
        assert note_relative_path("reaction", value)
    for value in ("../escape", "a/b", ".hidden", ""):
        assert not is_note_slug(value)


class _FakeErrors:
    """The DB-API 2.0 exception hierarchy, as the client exposes it under `client.errors`."""

    class Error(Exception):
        """Base of the client's hierarchy (PEP 249)."""

    class InterfaceError(Error):
        """A fault in the client or in how it was called."""

    class DatabaseError(Error):
        """Base for errors the server produced."""

    class OperationalError(DatabaseError):
        """Transport, service, timeout — the retryable family."""

    class ProgrammingError(DatabaseError):
        """A request the server understood and refused: bad credentials, unknown role."""


class _FakeClient:
    """A stand-in for `snowflake.connector`, raising whatever the test asks for on connect."""

    errors = _FakeErrors

    def __init__(self, raises: Exception | None = None) -> None:
        """Bind the failure this client should raise from `connect`, or `None` to succeed."""
        self._raises = raises
        self.connected_with: dict[str, Any] | None = None

    def connect(self, **options: Any) -> Any:
        """Record the options and either fail as configured or return a sentinel connection."""
        self.connected_with = options
        if self._raises is not None:
            raise self._raises
        return object()


def _warehouse(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> Any:
    """A `SnowflakeWarehouse` whose `_client()` seam yields `client`."""
    from chemclaw.ingest.eln.warehouse import snowflake as module

    monkeypatch.setattr(module, "_client", lambda: client)
    return module.SnowflakeWarehouse(
        account="acct", user="svc", password="pw", query_timeout_seconds=30
    )


def test_a_refused_credential_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding: every client error became a retryable `ConnectionError`.

    A wrong password, an unknown account and a role the user does not hold all fail identically on
    every attempt. Classifying them as "the warehouse is unreachable" burned the sync's whole
    Temporal retry budget before an operator saw a message — which then said "cannot connect" about
    a credential problem.
    """
    client = _FakeClient(_FakeErrors.ProgrammingError("Incorrect username or password"))
    warehouse = _warehouse(monkeypatch, client)
    with pytest.raises(WarehouseQueryError, match="refused this connection"):
        asyncio.run(warehouse._connect())


def test_a_client_side_fault_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`InterfaceError` is the client or the call, never the warehouse — retrying repeats it."""
    client = _FakeClient(_FakeErrors.InterfaceError("bad option"))
    warehouse = _warehouse(monkeypatch, client)
    with pytest.raises(WarehouseQueryError):
        asyncio.run(warehouse._connect())


def test_an_unreachable_warehouse_stays_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction, or the tests above would pass on a driver that never retries anything.

    This is the case `ConnectionError` was always for, and the split must not cost it.
    """
    client = _FakeClient(_FakeErrors.OperationalError("connection reset"))
    warehouse = _warehouse(monkeypatch, client)
    with pytest.raises(ConnectionError, match="cannot connect"):
        asyncio.run(warehouse._connect())


def test_the_connection_is_opened_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module's own contract, and the first test to execute it."""
    client = _FakeClient()
    warehouse = _warehouse(monkeypatch, client)
    first = asyncio.run(warehouse._connect())
    second = asyncio.run(warehouse._connect())
    assert first is second


def test_the_timeouts_and_paramstyle_reach_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the module sets on every connection, asserted where a real deployment would feel it.

    `paramstyle` is set per connection rather than through the client's process-wide module global
    on purpose — the module says so, and nothing checked it. Positional binding is what makes every
    value in this engine a bound parameter, which is the invariant the SQL header claims.
    """
    client = _FakeClient()
    warehouse = _warehouse(monkeypatch, client)
    asyncio.run(warehouse._connect())

    assert client.connected_with is not None
    assert client.connected_with["paramstyle"] == "qmark"
    assert client.connected_with["login_timeout"] == 30
    assert client.connected_with["network_timeout"] == 30
    assert client.connected_with["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == 30
    assert warehouse.placeholder == "?"


def test_a_missing_client_is_a_directive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The path a repository without the client actually takes — including this one.

    Not transient, so it is a `BindingError`: a binding named a driver this image does not carry,
    and retrying will not install it.
    """
    from chemclaw.ingest.eln.warehouse import snowflake as module

    monkeypatch.setitem(__import__("sys").modules, "snowflake", None)
    with pytest.raises(BindingError, match="not installed"):
        module._client()


def test_the_knowledge_path_join_is_still_correct(tmp_path: Path) -> None:
    """The check must not break the thing it guards: a legitimate key still finds its note."""
    key = "e-1041"
    note_id = note_id_for_reaction(key)
    assert is_note_slug(note_id)
    target = tmp_path / note_relative_path("reaction", note_id)
    target.parent.mkdir(parents=True)
    target.write_text("---\ntype: reaction\n---\n", encoding="utf-8")
    assert target.is_file()
