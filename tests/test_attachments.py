"""What a session's attachment store drops, and whether anything says so.

`AttachmentStore.add` evicts a session's oldest uploads past `attachment_max_per_session` and past
the byte budget, and for the whole life of that bound the eviction was *silent*: no log record, no
metric, no field on either model-facing tool. Measured at the shipped cap of 10 — thirteen uploads,
ten held, `plate-00/01/02.csv` gone, and `read_attachment("plate-00.csv")` answering `no attachment
named 'plate-00.csv' in this conversation` about a file the chemist had just uploaded *to this
conversation*. The model relays that as fact.

These tests drive the store and the two tools directly, because the defect is what a reader is told
rather than what is retained: `tests/test_remaining_gaps.py` already pins the retention half.
"""

import asyncio

from chemclaw.agent.attachments import (
    Attachment,
    AttachmentStore,
    list_attachments,
    read_attachment,
)
from chemclaw.core.config import settings
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id


def _file(name: str, text: str = "a,b\n1,2\n") -> Attachment:
    """One parsed upload, small enough that only the count bound can bite."""
    return Attachment(name=name, content_type="text/csv", text=text, rows=1)


def test_a_session_remembers_which_uploads_it_dropped() -> None:
    """The count bound evicts, and the store used to keep no record that it had.

    Thirteen uploads at the shipped cap of ten: the three oldest are gone from `items`, and the
    only honest thing left to say about them is that they *were* here.
    """
    store = AttachmentStore()
    for index in range(settings.attachment_max_per_session + 3):
        store.add("s1", _file(f"plate-{index:02d}.csv"))

    held = store.snapshot("s1")
    assert len(held.items) == settings.attachment_max_per_session
    assert held.evicted == ["plate-00.csv", "plate-01.csv", "plate-02.csv"]
    assert held.evicted_total == 3


def test_the_byte_bound_records_its_evictions_too() -> None:
    """Both per-session bounds drop the same way, so both must leave the same record.

    The byte half is the one that bites on real spreadsheets: `attachment_max_bytes` bounds the
    *upload* and the parsed expansion is bounded by `document_max_expanded_bytes`, which is larger
    than the whole store's budget.
    """
    store = AttachmentStore()
    per_file = settings.attachment_store_max_bytes // 3
    for index in range(3):
        store.add("hog", _file(f"big{index}.csv", "x" * per_file))

    held = store.snapshot("hog")
    assert [item.name for item in held.items] == ["big1.csv", "big2.csv"]
    assert held.evicted == ["big0.csv"]


def test_the_listing_says_what_it_is_not_showing() -> None:
    """`list_attachments` claimed "everything attached to a session" over a truncated list.

    The verdict is a `computed_field` rather than a property for the reason
    `FingerprintSearch.verdict` is: a bare property is not serialized, so the one sentence that
    tells the model its list is short would never leave this process.
    """
    from chemclaw.agent import attachments

    store = AttachmentStore()
    token = set_current_session_id("listing-session")
    original = attachments.STORE
    attachments.STORE = store
    try:
        for index in range(settings.attachment_max_per_session + 3):
            store.add("listing-session", _file(f"plate-{index:02d}.csv"))
        listing = asyncio.run(list_attachments())
    finally:
        attachments.STORE = original
        reset_current_session_id(token)

    assert len(listing.attachments) == settings.attachment_max_per_session
    assert listing.evicted == ["plate-00.csv", "plate-01.csv", "plate-02.csv"]
    assert listing.evicted_total == 3
    payload = listing.model_dump()
    assert "verdict" in payload  # serialized, not a bare property
    assert "3" in payload["verdict"]
    assert "plate-00.csv" in payload["verdict"]


def test_the_listing_verdict_says_nothing_was_dropped_when_nothing_was() -> None:
    """The ordinary case must read as complete, or the marker means nothing."""
    from chemclaw.agent import attachments

    store = AttachmentStore()
    token = set_current_session_id("clean-session")
    original = attachments.STORE
    attachments.STORE = store
    try:
        store.add("clean-session", _file("only.csv"))
        listing = asyncio.run(list_attachments())
    finally:
        attachments.STORE = original
        reset_current_session_id(token)

    assert listing.evicted == []
    assert listing.evicted_total == 0
    assert "COMPLETE" in listing.model_dump()["verdict"]


def test_reading_a_dropped_upload_says_it_was_dropped_not_that_it_never_arrived() -> None:
    """The worst half: silence became a false statement about the chemist.

    `no attachment named 'plate-00.csv' in this conversation` is a claim, and it is wrong — the
    file was uploaded to this conversation and this store dropped it. A model told that tells the
    chemist they never sent it.
    """
    from chemclaw.agent import attachments

    store = AttachmentStore()
    token = set_current_session_id("read-session")
    original = attachments.STORE
    attachments.STORE = store
    try:
        for index in range(settings.attachment_max_per_session + 1):
            store.add("read-session", _file(f"plate-{index:02d}.csv"))
        try:
            asyncio.run(read_attachment("plate-00.csv"))
        except ValueError as exc:
            message = str(exc)
        else:  # pragma: no cover - the read must not succeed
            raise AssertionError("a dropped attachment must not read as present")
        try:
            asyncio.run(read_attachment("never-sent.csv"))
        except ValueError as exc:
            absent = str(exc)
        else:  # pragma: no cover - the read must not succeed
            raise AssertionError("an unknown attachment must not read as present")
    finally:
        attachments.STORE = original
        reset_current_session_id(token)

    assert "dropped" in message
    assert "was uploaded" in message
    # ...and the two cases must not render alike, which is the whole point.
    assert "was uploaded" not in absent
    assert message != absent
