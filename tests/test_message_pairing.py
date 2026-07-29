"""The stored-history invariant: no tool call without its result (offline).

An unmatched `tool_use` is not one bad turn — the model rejects the whole thread, so a durable
history that acquires one replays it forever and the session is unusable until somebody edits the
database. These pin the pure rule; `test_session_store.py` pins the storage layer that applies it.
"""

from agent_framework import Content, Message

from chemclaw.agent.message_pairing import strip_unmatched_calls, unmatched_call_ids


def _call(call_id: str, name: str = "screen_hazards") -> Content:
    return Content.from_function_call(call_id=call_id, name=name, arguments={})


def _result(call_id: str, result: str = "ok") -> Content:
    return Content.from_function_result(call_id=call_id, result=result)


def test_a_matched_call_is_not_reported() -> None:
    """The ordinary case: a call answered by its result is not an orphan."""
    messages = [
        Message(role="assistant", contents=[_call("c1")]),
        Message(role="tool", contents=[_result("c1")]),
    ]
    assert unmatched_call_ids(messages) == set()
    assert strip_unmatched_calls(messages) == messages


def test_an_unanswered_call_is_reported_and_stripped() -> None:
    """A call whose result never arrived — the disconnect/kill signature — is removed."""
    messages = [
        Message(role="user", contents=[Content.from_text("screen these")]),
        Message(role="assistant", contents=[_call("c1")]),
    ]
    assert unmatched_call_ids(messages) == {"c1"}
    stripped = strip_unmatched_calls(messages)
    assert len(stripped) == 1  # the bare tool-call message is gone entirely
    assert stripped[0].contents[0].text == "screen these"  # the user's turn is untouched


def test_prose_alongside_an_orphan_call_survives() -> None:
    """Only the offending content is dropped — discarding the assistant's words would rewrite it.

    An assistant turn routinely carries text *and* a tool call; dropping the whole message to get
    rid of the call would silently delete something the chemist was told.
    """
    messages = [
        Message(role="assistant", contents=[Content.from_text("Let me check that."), _call("c1")]),
    ]
    (kept,) = strip_unmatched_calls(messages)
    assert [c.type for c in kept.contents] == ["text"]
    assert kept.contents[0].text == "Let me check that."


def test_only_the_unanswered_call_is_stripped_from_a_parallel_batch() -> None:
    """One missing result does not invalidate its siblings — parallel calls are judged per id."""
    messages = [
        Message(role="assistant", contents=[_call("c1"), _call("c2")]),
        Message(role="tool", contents=[_result("c1")]),
    ]
    assert unmatched_call_ids(messages) == {"c2"}
    stripped = strip_unmatched_calls(messages)
    assert [c.call_id for c in stripped[0].contents] == ["c1"]
    assert [c.call_id for c in stripped[1].contents] == ["c1"]


def test_a_result_matches_its_call_wherever_it_sits() -> None:
    """Matching is by id, not adjacency, so a valid pair is never reported over mere ordering."""
    messages = [
        Message(role="assistant", contents=[_call("c1")]),
        Message(role="assistant", contents=[Content.from_text("thinking")]),
        Message(role="tool", contents=[_result("c1")]),
    ]
    assert unmatched_call_ids(messages) == set()


def test_message_metadata_rides_along_when_contents_are_trimmed() -> None:
    """Trimming preserves the rest of the message; rebuilding it from scratch would drop these."""
    messages = [
        Message(
            role="assistant",
            contents=[Content.from_text("hi"), _call("c1")],
            author_name="chemclaw",
            message_id="m-1",
            additional_properties={"turn": 3},
        )
    ]
    (kept,) = strip_unmatched_calls(messages)
    assert kept.author_name == "chemclaw"
    assert kept.message_id == "m-1"
    assert kept.additional_properties == {"turn": 3}


def test_an_untouched_history_is_returned_unchanged() -> None:
    """The clean path copies nothing — this runs on every single history read."""
    messages = [Message(role="user", contents=[Content.from_text("hi")])]
    assert strip_unmatched_calls(messages)[0] is messages[0]


def test_empty_history_is_handled() -> None:
    """A brand-new session reads back nothing at all."""
    assert unmatched_call_ids([]) == set()
    assert strip_unmatched_calls([]) == []
