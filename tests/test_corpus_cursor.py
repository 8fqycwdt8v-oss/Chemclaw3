"""The keyset watermark that turns a daily corpus re-walk into a daily delta.

Two halves: `corpus_cursors` round-trips a position, and the drain activity consults it only
where the binding declares the source append-only — a release keeps re-walking, and only a
source whose author asserted monotonic keys resumes
(`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop`).
"""

import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from chemclaw.ingest.eln.warehouse.binding import CorpusBinding
from chemclaw.ingest.labels.cursor import load_corpus_cursor, store_corpus_cursor
from tests.pg import migrated_db_or_skip

_BINDING: dict[str, object] = {
    "relation": "V_REACTION",
    "key": "REACTION_ID",
    "order_by": "LOAD_SEQ",
    "smiles": {"path": "root.REACTION_SMILES"},
    "citation": {"path": "root.REACTION_ID"},
}


async def _resumes_at_a400(source: str, dsn: str | None = None) -> str:
    """A stored position, for the two tests that drive the activity's resume branch."""
    return "A400"


def test_a_release_binding_is_not_append_only_and_a_feed_says_so() -> None:
    """The default is the release, so an existing manifest keeps draining from the top.

    A binding that silently became append-only would skip rows a vendor re-issued below the
    watermark.
    """
    assert CorpusBinding.model_validate(_BINDING).append_only is False
    assert CorpusBinding.model_validate({**_BINDING, "append_only": True}).append_only is True


@pytest.mark.anyio
async def test_the_cursor_round_trips_and_an_unknown_source_starts_at_the_beginning() -> None:
    """A stored position comes back verbatim; an absent one is the empty start `drain_corpus` takes.

    Verbatim matters: the value is a key in the *source's* domain, so anything this side did to it
    — trimming, casing, coercing to a number — would resume the walk somewhere else.
    """
    await migrated_db_or_skip()

    assert await load_corpus_cursor("never-drained") == ""

    await store_corpus_cursor("feed-a", "A100")
    assert await load_corpus_cursor("feed-a") == "A100"

    await store_corpus_cursor("feed-a", "A250")
    assert await load_corpus_cursor("feed-a") == "A250"

    # Sources do not share a watermark.
    assert await load_corpus_cursor("feed-b") == ""


@pytest.mark.anyio
async def test_an_empty_position_never_overwrites_a_real_one() -> None:
    """A pass that advanced past nothing must not reset the source to the top.

    `drain_corpus` returns `cursor=after` for an empty page and the activity stores every page, so
    without this guard a quiet day would re-walk the whole corpus on the next fire.
    """
    await migrated_db_or_skip()

    await store_corpus_cursor("feed-quiet", "A500")
    await store_corpus_cursor("feed-quiet", "")

    assert await load_corpus_cursor("feed-quiet") == "A500"


def test_the_drain_activity_resumes_a_feed_and_re_walks_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `append_only` flag decides whether the stored position is consulted at all.

    Driven through the activity, because that is the only place allowed to turn the workflow's empty
    `after` ("start of this source") into a database read.
    """
    from chemclaw.durable import corpus_sync

    seen: list[str] = []
    stores: list[object] = []

    async def _fake_drain(
        *_args: object, after: str = "", reactions: object = None, **_kwargs: object
    ) -> object:
        seen.append(after)
        stores.append(reactions)
        from chemclaw.ingest.labels.corpus import CorpusReport

        # `advanced=True`, because a page that moved the position is the case that stores one —
        # the stalled page is its own test below.
        return CorpusReport(read=1, cursor=f"{after}+1", advanced=True)

    stored: list[tuple[str, str]] = []

    async def _fake_store(source: str, after: str, dsn: str | None = None) -> None:
        stored.append((source, after))

    monkeypatch.setattr(corpus_sync, "drain_corpus", _fake_drain)
    monkeypatch.setattr(corpus_sync, "load_corpus_cursor", _resumes_at_a400)
    monkeypatch.setattr(corpus_sync, "store_corpus_cursor", _fake_store)
    monkeypatch.setattr(corpus_sync, "_warehouse_for", lambda _source: object())
    monkeypatch.setattr(corpus_sync, "_label_index", lambda: object())
    monkeypatch.setattr(corpus_sync, "_corpus_molecules", lambda: None)
    # A sentinel rather than `None`, so the assertion below proves the activity still passes
    # `reactions=_corpus_reactions()`; this is the only test that drives the production call site.
    reaction_store = object()
    monkeypatch.setattr(corpus_sync, "_corpus_reactions", lambda: reaction_store)

    feed = CorpusBinding.model_validate({**_BINDING, "append_only": True})
    release = CorpusBinding.model_validate(_BINDING)

    # `activity.heartbeat()` needs an activity context; the environment is what supplies one.
    env = ActivityEnvironment()

    monkeypatch.setattr(corpus_sync, "corpus_sources", lambda: {"feed": feed})
    asyncio.run(env.run(corpus_sync.drain_reaction_corpus, "feed", ""))

    monkeypatch.setattr(corpus_sync, "corpus_sources", lambda: {"rel": release})
    asyncio.run(env.run(corpus_sync.drain_reaction_corpus, "rel", ""))

    # The feed resumed at its stored position; the release began at the top, as it always has.
    assert seen == ["A400", ""]
    # And only the feed wrote one back — at the position the page advanced to, not the one it
    # resumed from.
    assert stored == [("feed", "A400+1")]
    # Both modes fingerprint their reactions: `append_only` decides the cursor and nothing else, so
    # an existing release corpus becomes searchable by transformation on its next drain.
    assert stores == [reaction_store, reaction_store]


def test_a_page_that_did_not_advance_writes_no_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled feed must not refresh `updated_at`, or the column means nothing.

    A stalled feed returns the same non-empty key it resumed from, so an unconditional write would
    re-stamp `updated_at` every fire and a dead source would read as freshly synced. The gate is
    `report.advanced`, computed in `drain_corpus` where both positions are in scope.
    """
    from chemclaw.durable import corpus_sync

    async def _stalled(*_args: object, after: str = "", **_kwargs: object) -> object:
        from chemclaw.ingest.labels.corpus import CorpusReport

        # What a drain returns when the page it read moved nothing: the position it was given,
        # non-empty, with `advanced` false.
        return CorpusReport(read=1, cursor=after, advanced=False)

    stored: list[tuple[str, str]] = []

    async def _fake_store(source: str, after: str, dsn: str | None = None) -> None:
        stored.append((source, after))

    monkeypatch.setattr(corpus_sync, "drain_corpus", _stalled)
    monkeypatch.setattr(corpus_sync, "load_corpus_cursor", _resumes_at_a400)
    monkeypatch.setattr(corpus_sync, "store_corpus_cursor", _fake_store)
    monkeypatch.setattr(corpus_sync, "_warehouse_for", lambda _source: object())
    monkeypatch.setattr(corpus_sync, "_label_index", lambda: object())
    monkeypatch.setattr(corpus_sync, "_corpus_molecules", lambda: None)
    monkeypatch.setattr(corpus_sync, "_corpus_reactions", lambda: None)
    feed = CorpusBinding.model_validate({**_BINDING, "append_only": True})
    monkeypatch.setattr(corpus_sync, "corpus_sources", lambda: {"feed": feed})

    asyncio.run(ActivityEnvironment().run(corpus_sync.drain_reaction_corpus, "feed", ""))

    assert stored == []
