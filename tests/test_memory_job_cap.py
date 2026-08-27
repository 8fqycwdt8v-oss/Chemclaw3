"""What one synthesis run may propose is bounded, and the bound does not lose the tail (D-161).

The three memory jobs rescan the whole corpus daily with no cursor and had no ceiling on what a
single run could propose. In practice they stay quiet, on three accidents rather than any rule: an
id anchored on a cluster's smallest member reuses its branch, a byte-identical note produces no
diff and no push, and force-push-with-lease updates in place. Nothing *bounded* them, so a large
corpus import would have opened a PR per cluster on the first night.

The interesting half is what a plain cap would have done instead. The builders are deterministic
over the corpus, so `notes[:cap]` proposes the same first N every night and the rest are proposed
never — trading a visible flood for silently lost knowledge. These tests pin the rotation that
makes the cap a delay rather than a deletion.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.durable.memory_jobs import SynthesisUnit, _slice_for_this_run
from chemclaw.kg.note import Note


class _FakeWorkflowClock:
    """Stand in for `temporalio.workflow`'s `now()` and `logger` outside a workflow context."""

    def __init__(self, day: datetime) -> None:
        self._day = day
        self.warnings: list[tuple[Any, ...]] = []

    def now(self) -> datetime:
        return self._day

    @property
    def logger(self) -> "_FakeWorkflowClock":
        return self

    def warning(self, *args: Any) -> None:
        self.warnings.append(args)


def _corpus(n: int) -> list[Note]:
    """`n` distinct proposable notes, ids zero-padded so id order is numeric order."""
    return [
        SynthesisUnit(
            note=Note(id=f"campaign-{i:03d}", type="campaign", created_by="agent", body="x")
        )
        for i in range(n)
    ]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeWorkflowClock:
    """Pin the workflow clock to a fixed day and capture what the run logged."""
    fake = _FakeWorkflowClock(datetime(2026, 7, 31, tzinfo=UTC))
    monkeypatch.setattr("chemclaw.durable.memory_jobs.workflow", fake)
    return fake


def test_a_corpus_within_the_cap_is_untouched(
    clock: _FakeWorkflowClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steady state must be byte-identical, and silent: no cap, no warning."""
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 25)
    notes = _corpus(10)
    assert _slice_for_this_run(notes, settings.memory_max_notes_per_run, "campaign") == notes
    assert clock.warnings == []


def test_a_cap_of_zero_means_unbounded(
    clock: _FakeWorkflowClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that wants the old behaviour must be able to say so."""
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 0)
    assert (
        len(_slice_for_this_run(_corpus(500), settings.memory_max_notes_per_run, "campaign")) == 500
    )


def test_a_large_corpus_is_capped_and_the_drop_is_logged(
    clock: _FakeWorkflowClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flood this exists to stop — and it is never silent about stopping it."""
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 20)
    assert (
        len(_slice_for_this_run(_corpus(500), settings.memory_max_notes_per_run, "playbook")) == 20
    )
    assert len(clock.warnings) == 1
    message = clock.warnings[0][0] % clock.warnings[0][1:]
    assert "capped at 20 of 500" in message


def test_consecutive_runs_cover_different_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that makes this a delay and not a deletion.

    A plain `notes[:cap]` would return the same twenty every night forever, and the other 480
    would be proposed never — the failure a cap is *supposed* to be preventing, in a quieter form.
    """
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 20)
    notes = _corpus(500)

    def _day(n: int) -> set[str]:
        fake = _FakeWorkflowClock(datetime(2026, 7, n, tzinfo=UTC))
        monkeypatch.setattr("chemclaw.durable.memory_jobs.workflow", fake)
        return {
            unit.note.id
            for unit in _slice_for_this_run(notes, settings.memory_max_notes_per_run, "campaign")
        }

    first, second, third = _day(1), _day(2), _day(3)
    assert first != second != third
    assert not (first & second) and not (second & third)


def test_the_whole_corpus_is_reached_within_one_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every note is proposed eventually, which is the whole justification for capping at all."""
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 20)
    notes = _corpus(200)
    seen: set[str] = set()
    for day in range(1, 11):  # 200 / 20 = 10 runs
        fake = _FakeWorkflowClock(datetime(2026, 7, day, tzinfo=UTC))
        monkeypatch.setattr("chemclaw.durable.memory_jobs.workflow", fake)
        seen |= {
            unit.note.id
            for unit in _slice_for_this_run(notes, settings.memory_max_notes_per_run, "campaign")
        }
    assert seen == {unit.note.id for unit in notes}


def test_the_window_wraps_rather_than_running_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window starting near the end still returns a full page, taken from the front.

    Slicing without the wrap would make the last run of each cycle quietly smaller than every
    other — a second, subtler place for notes to go missing.
    """
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 7)
    notes = _corpus(10)
    for day in range(1, 15):
        fake = _FakeWorkflowClock(datetime(2026, 7, day, tzinfo=UTC))
        monkeypatch.setattr("chemclaw.durable.memory_jobs.workflow", fake)
        window = _slice_for_this_run(notes, settings.memory_max_notes_per_run, "campaign")
        assert len(window) == 7
        assert len({unit.note.id for unit in window}) == 7  # no note twice in one run


def test_the_slice_is_stable_within_a_run(
    clock: _FakeWorkflowClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow replays, so the same run must select the same notes every time it does."""
    monkeypatch.setattr(settings, "memory_max_notes_per_run", 20)
    notes = _corpus(500)
    assert _slice_for_this_run(
        notes, settings.memory_max_notes_per_run, "campaign"
    ) == _slice_for_this_run(notes, settings.memory_max_notes_per_run, "campaign")
    # And it does not depend on the order the builder happened to emit them in.
    assert _slice_for_this_run(
        notes, settings.memory_max_notes_per_run, "campaign"
    ) == _slice_for_this_run(list(reversed(notes)), settings.memory_max_notes_per_run, "campaign")
