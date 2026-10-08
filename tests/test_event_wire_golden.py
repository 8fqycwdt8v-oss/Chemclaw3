"""The bytes of every turn event on the wire cannot change unnoticed.

`tests/fixtures/turn_event_frames.json` holds the SSE frame (`event:` name and `data:` body) of one
instance of every event kind, captured from the serialiser as it stood when the contract was first
published. A live browser parses those exact bytes, so a model refactor that reorders, renames or
retypes a field fails here even when every pydantic-level test stays green.

There is deliberately no regeneration switch: a wire change is a contract change (major or minor
under `schema/api/README.md`), made by editing the fixture by hand in the same commit as the bump.
"""

import json
import typing
from pathlib import Path

from chemclaw.api.events import Event, sse_frame
from tests.event_samples import SAMPLES

_FIXTURE = Path(__file__).parent / "fixtures" / "turn_event_frames.json"


def _recorded() -> dict[str, dict[str, str]]:
    return typing.cast(dict[str, dict[str, str]], json.loads(_FIXTURE.read_text(encoding="utf-8")))


def test_every_event_kind_serialises_to_the_recorded_bytes() -> None:
    """Each sample's frame equals the recorded frame, character for character."""
    recorded = _recorded()
    drifted = {
        key: {"recorded": recorded[key], "now": sse_frame(event)}
        for key, event in SAMPLES.items()
        if sse_frame(event) != recorded[key]
    }
    assert not drifted, (
        "the SSE wire format changed for "
        f"{sorted(drifted)}:\n{json.dumps(drifted, indent=2)}\n"
        "The UI is live and parses these bytes. If the change is intended, bump "
        "API_CONTRACT_VERSION per schema/api/README.md and edit the fixture in the same commit."
    )


def test_the_fixture_and_the_samples_cover_the_same_frames() -> None:
    """No sample is unrecorded and no recorded frame is unsampled."""
    assert sorted(_recorded()) == sorted(SAMPLES)


def test_every_member_of_the_union_has_a_full_sample() -> None:
    """A new event kind arrives with a sample, so its bytes are pinned from the first commit."""
    kinds = {
        typing.get_args(model.model_fields["type"].annotation)[0]
        for model in typing.get_args(Event)
    }
    sampled = {key.split("/")[0] for key in SAMPLES if key.endswith("/full")}
    assert kinds == sampled, (
        f"events without a sample: {sorted(kinds - sampled)}; stale: {sorted(sampled - kinds)}"
    )
