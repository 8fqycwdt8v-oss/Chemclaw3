"""The keyset watermark that turns a daily corpus re-walk into a daily delta.

Two halves, and the second is the one worth having. The first is that `corpus_cursors` round-trips
a position — which is a table. The second is that the *drain activity* consults it only where the
binding claims the source is append-only, because that is the decision
`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop` actually takes: a release keeps the behaviour
it had, and only a source whose author asserted monotonic keys resumes.
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


def test_a_release_binding_is_not_append_only_and_a_feed_says_so() -> None:
    """The default is the release, so an existing manifest keeps draining from the top.

    Asserted rather than assumed because the whole change is additive *in behaviour* only as long
    as this default holds: a binding that silently became append-only would start skipping rows a
    vendor re-issued below the watermark.
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

    `drain_corpus` returns `cursor=after` for an empty page, and the activity stores it every page
    rather than only the last — so without this guard the first quiet day of a live feed would
    re-walk the whole corpus on the next fire, which is the failure the table exists to prevent.
    """
    await migrated_db_or_skip()

    await store_corpus_cursor("feed-quiet", "A500")
    await store_corpus_cursor("feed-quiet", "")

    assert await load_corpus_cursor("feed-quiet") == "A500"


def test_the_drain_activity_resumes_a_feed_and_re_walks_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `append_only` flag is what decides whether the stored position is consulted at all.

    Driven through `drain_reaction_corpus` rather than through `drain_corpus`, because the branch
    under test is in the activity: the workflow spells "start of this source" as an empty `after`
    both on the first page and after it pops a finished source, and the activity is the only place
    that may turn that into a database read.
    """
    from chemclaw.durable import corpus_sync

    seen: list[str] = []

    async def _fake_drain(*_args: object, after: str = "", **_kwargs: object) -> object:
        seen.append(after)
        from chemclaw.ingest.labels.corpus import CorpusReport

        return CorpusReport(read=0, cursor=after)

    async def _fake_load(source: str, dsn: str | None = None) -> str:
        return "A400"

    stored: list[tuple[str, str]] = []

    async def _fake_store(source: str, after: str, dsn: str | None = None) -> None:
        stored.append((source, after))

    monkeypatch.setattr(corpus_sync, "drain_corpus", _fake_drain)
    monkeypatch.setattr(corpus_sync, "load_corpus_cursor", _fake_load)
    monkeypatch.setattr(corpus_sync, "store_corpus_cursor", _fake_store)
    monkeypatch.setattr(corpus_sync, "_warehouse_for", lambda _source: object())
    monkeypatch.setattr(corpus_sync, "_label_index", lambda: object())
    monkeypatch.setattr(corpus_sync, "_corpus_molecules", lambda: None)
    monkeypatch.setattr(corpus_sync, "_corpus_reactions", lambda: None)

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
    # And only the feed wrote one back.
    assert stored == [("feed", "A400")]
