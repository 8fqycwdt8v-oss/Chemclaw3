"""What the in-process leak hunt measures, and what it refuses to conclude — `cli/leak_probe.py`.

The probe's own claim is that it drives the *real* front door, so most of it cannot run here: it
needs `make live-up` (the mock LLM, Postgres, every connector), and faking that stack would produce
exactly the measurement the module says is worthless — an earlier repro that faked the agent and
the connectors found zero retention across 900 turns, and the leak was in the three things it had
replaced. So `main` is deliberately not exercised below.

What is exercised is everything `main` composes, each of which is a real measurement in its own
right and none of which needs a live lane:

- the two counters — resident set and live objects by type — checked against an independent
  reading of the same quantity, because a probe whose instruments are wrong reports confidently;
- `_drive`, against a real HTTP client and a stand-in front door, for the one number a leak hunt
  cannot afford to have inflated: how many turns were actually *answered*;
- `_sample`, including what `tracemalloc` is allowed to say and what it must leave out;
- `report` and `leaks`, whose verdict is read off `soak_report.fit` rather than off two endpoints
  (`tests/test_soak_report.py` holds the fit itself; what is held here is that this module defers
  to it instead of growing a second opinion).
"""

import gc
import logging
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response

import chemclaw.cli.leak_probe as cli

_STATUS = Path("/proc/self/status")


class _Retained:
    """An object of a type nothing else in the process allocates, so a delta names only these."""


# --- the instruments ----------------------------------------------------------------------------


@pytest.mark.skipif(not _STATUS.exists(), reason="no /proc; the probe reads Linux counters")
def test_the_resident_set_agrees_with_the_kernel_s_own_kilobytes() -> None:
    """`statm` reports pages of an unstated size, and the whole leak verdict is built on this.

    Read against `VmRSS`, which the kernel prints in kilobytes — an independent expression of the
    same quantity. A wrong page size or the neighbouring `statm` field (total program size, several
    times larger) would both pass an "is it a plausible number" check and put every RSS slope in
    the report off by a constant factor.
    """
    before = _vm_rss_kb()
    measured = cli._rss_kb()
    after = _vm_rss_kb()

    assert measured == pytest.approx((before + after) / 2, rel=0.02)
    assert measured > 10_000, "a process with FastAPI imported holds more than 10 MB"


def test_the_type_histogram_counts_the_live_objects_of_each_type() -> None:
    """The decisive series: RSS can rise from fragmentation, a live object count cannot.

    So the count has to be exact rather than indicative — it is what names the leaked type when
    the two batches are diffed.
    """
    before = cli._type_histogram()
    held = [_Retained() for _ in range(500)]
    after = cli._type_histogram()

    grown = after.get("_Retained", 0) - before.get("_Retained", 0)
    assert grown == len(held)

    del held
    gc.collect()
    assert cli._type_histogram().get("_Retained", 0) == before.get("_Retained", 0)


def test_a_sample_reads_every_counter_at_one_moment() -> None:
    """One `Sample` is one instant, and the untraced run must not pay for tracemalloc."""
    sample = cli._sample(120, trace=False, baseline=None)

    assert sample.turns == 120
    assert sample.rss_kb > 10_000
    assert sample.gc_objects > 1_000
    # Both readings come from the same collection, moments apart, so they describe one heap.
    assert sum(sample.types.values()) == pytest.approx(sample.gc_objects, rel=0.01)
    assert sample.tracked_kb == 0.0
    assert sample.top_allocations == []


def test_a_traced_sample_names_what_grew_and_leaves_out_what_shrank() -> None:
    """Growth since the baseline is the question; a freed allocation is not an answer to it.

    `compare_to` ranks by the *magnitude* of the change, so the 2 MB released below is the largest
    entry in the comparison and would head the "largest growth" list — reported to a reader hunting
    a leak as the biggest thing that grew, with a minus sign in front of it.
    """
    tracemalloc.start(5)
    try:
        transient = ["x" * 10_000 for _ in range(200)]
        baseline = tracemalloc.take_snapshot()
        del transient
        kept_a = ["a" * 1_000 for _ in range(100)]
        kept_b = {index: f"b{index}" * 200 for index in range(100)}
        kept_c = ["c" * 1_000 for _ in range(100)]
        sample = cli._sample(50, trace=True, baseline=baseline)
    finally:
        tracemalloc.stop()

    # Held until after the sample, so they are growth rather than garbage.
    assert len(kept_a) == len(kept_b) == len(kept_c) == 100
    assert sample.tracked_kb > 0.0
    assert sample.top_allocations, "something grew between the baseline and the sample"
    assert len(sample.top_allocations) <= 8
    assert all(line.startswith("+") for line in sample.top_allocations), sample.top_allocations


# --- driving the turns --------------------------------------------------------------------------


@dataclass
class _Journal:
    """What the stand-in front door was asked to do, in the order it was asked."""

    creations: int = 0
    sessions: list[str] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)


def _front_door(
    journal: _Journal,
    *,
    refuse_creations: frozenset[int] = frozenset(),
    refuse_answers: bool = False,
) -> FastAPI:
    """A front door with the two routes the probe drives and nothing behind them.

    Real routing over a real client, because what is under test is the driving loop — that a turn
    is a fresh session plus a message into *that* session, and which of them counts as answered.
    The app the probe drives in anger is `chemclaw.api.app`; standing that up needs the live lane.
    """
    app = FastAPI()

    @app.post("/sessions")
    def open_session() -> Response:
        """Open a session, or refuse this one if the test asked for a refusal here."""
        journal.creations += 1
        if journal.creations in refuse_creations:
            return JSONResponse({"detail": "no capacity"}, status_code=503)
        session_id = f"sess-{journal.creations}"
        journal.sessions.append(session_id)
        return JSONResponse({"session_id": session_id})

    @app.post("/sessions/{session_id}/messages")
    def answer(session_id: str, body: dict[str, str]) -> Response:
        """Record the turn, and answer it unless the test asked for a failing front door."""
        journal.messages.append((session_id, body["message"]))
        if refuse_answers:
            return JSONResponse({"detail": "the turn failed"}, status_code=500)
        return JSONResponse({"reply": "ok"})

    return app


def test_a_turn_is_a_fresh_session_and_one_message_into_it() -> None:
    """What the probe repeats has to be a whole turn, or the retention it measures is not a turn's.

    A session reused across turns would measure the cost of one conversation growing, which is a
    different phenomenon and the one the soak already knows about.
    """
    journal = _Journal()
    with TestClient(_front_door(journal)) as client:
        answered = cli._drive(client, 3)

    assert answered == 3
    assert journal.sessions == ["sess-1", "sess-2", "sess-3"]
    assert journal.messages == [(session, cli._MESSAGE) for session in journal.sessions]


def test_a_session_that_could_not_be_opened_costs_that_turn_and_not_the_run() -> None:
    """A refused session must not take the batch down with it, nor be counted as a turn.

    It also must not send its message somewhere else: the id comes out of the response body, so a
    driver that pressed on regardless would either raise or post into the previous session.
    """
    journal = _Journal()
    with TestClient(_front_door(journal, refuse_creations=frozenset({2}))) as client:
        answered = cli._drive(client, 3)

    assert answered == 2
    assert journal.creations == 3
    assert journal.sessions == ["sess-1", "sess-3"]
    assert [session for session, _ in journal.messages] == ["sess-1", "sess-3"]


def test_a_turn_the_front_door_refused_is_driven_but_not_counted_as_answered() -> None:
    """The number that must not be inflated, because it is the only evidence work happened.

    A probe that counted attempts would print `answered=25/25` against a front door answering
    nothing, and the flat RSS series under it would read as "no leak" rather than as "no turns".
    """
    journal = _Journal()
    with TestClient(_front_door(journal, refuse_answers=True)) as client:
        answered = cli._drive(client, 2)

    assert answered == 0
    assert len(journal.messages) == 2, "every turn was still driven"


# --- the report ---------------------------------------------------------------------------------


def _samples(
    rss: list[float],
    *,
    step: int = 25,
    tracked: list[float] | None = None,
    types: list[dict[str, int]] | None = None,
    allocations: list[list[str]] | None = None,
) -> list[cli.Sample]:
    """A batch series with `step` turns between samples — the shape `main` accumulates."""
    return [
        cli.Sample(
            turns=step * (index + 1),
            rss_kb=value,
            gc_objects=0.0,
            tracked_kb=tracked[index] if tracked else 0.0,
            types=types[index] if types else {},
            top_allocations=allocations[index] if allocations else [],
        )
        for index, value in enumerate(rss)
    ]


def test_a_single_batch_is_refused_rather_than_fitted() -> None:
    """One point is not a trend, and neither is none."""
    assert cli.report(_samples([1000.0])) == "not enough batches to fit anything"
    assert cli.report([]) == "not enough batches to fit anything"


def test_the_per_turn_column_is_per_turn_and_the_verdict_is_per_batch() -> None:
    """The two numbers a reader would otherwise conflate, and only one of them is trustworthy.

    `describe` fits against the batch index, so its slope is per batch; the column beside it
    divides by the turns those batches actually drove. On this series they differ by 25×, which is
    the batch size — printing the fit's slope in the per-turn column would say a turn retains
    100 KB when it retains 4.
    """
    text = cli.report(_samples([1000.0, 1100.0, 1200.0, 1300.0, 1400.0]))

    assert "# Leak probe: 125 turns in 5 batches" in text
    assert "| RSS | 1000 | 1400 | +4.00 KB |" in text
    assert "grows +100.0 KB/batch" in text


def test_a_series_that_was_never_sampled_is_left_out_of_the_table() -> None:
    """Without `--trace` there is no tracemalloc series, and zeroes are not a measurement."""
    rss = [1000.0, 1100.0, 1200.0, 1300.0]

    assert "tracemalloc" not in cli.report(_samples(rss))
    assert "tracemalloc" in cli.report(_samples(rss, tracked=[10.0, 20.0, 30.0, 40.0]))


def test_the_type_table_names_what_grew_and_says_how_fast() -> None:
    """The diff that names the leaked type — per turn, so it is comparable with the RSS column."""
    text = cli.report(
        _samples(
            [1000.0, 1400.0],
            step=100,
            types=[{"Session": 10, "Connector": 40}, {"Session": 30, "Connector": 12}],
        )
    )

    assert "| `Session` | +0.20 | +20 |" in text
    assert "Connector" not in text, "a type that shrank did not grow"


def test_a_type_the_first_batch_had_never_seen_counts_from_zero() -> None:
    """A class that only appears once the leak starts must not be missing from the diff."""
    text = cli.report(_samples([1000.0, 1400.0], step=100, types=[{}, {"TurnState": 50}]))

    assert "| `TurnState` | +0.50 | +50 |" in text


def test_two_batches_at_the_same_turn_count_report_no_rate_rather_than_raising() -> None:
    """A per-turn rate over zero turns is not a rate, and it is not a crash either.

    `report` is public and is the one durable artefact a leak hunt produces, so anything holding
    `Sample`s can call it — a run whose last batch drove nothing, a caller replaying a truncated
    JSONL. The series table has always guarded this divisor; the type table one block below it did
    not, so such a series printed its header, its RSS row and half a type table and then raised
    `ZeroDivisionError` out of the middle of the deliverable.
    """
    samples = _samples([1000.0, 1400.0], step=100, types=[{"Session": 10}, {"Session": 30}])
    samples[-1].turns = samples[0].turns

    text = cli.report(samples)

    assert "| RSS | 1000 | 1400 | +0.00 KB |" in text
    assert "| `Session` | +0.00 | +20 |" in text


def test_the_report_carries_the_last_allocations_and_drops_the_older_ones() -> None:
    """Eight lines of allocation sites, from the end of the run — where a leak is clearest."""
    batches = [[f"+{index}00 KB site-{index}-{n}" for n in range(4)] for index in range(3)]
    text = cli.report(_samples([1000.0, 1100.0, 1200.0], allocations=batches))

    assert "## Largest growth since the first batch" in text
    assert "site-0-0" not in text, "the twelve entries are cut to the last eight"
    assert "site-2-3" in text
    assert text.count("KB site-") == 8


# --- the verdict --------------------------------------------------------------------------------


def test_the_verdict_is_the_fit_and_not_the_two_endpoints() -> None:
    """The rule the soak's own record had to be rewritten twice to learn.

    This series ends 29 KB above where it started, which is what an endpoint comparison reports as
    a leak, and it is alternating noise around a flat line. `fit` says so; anything reading the
    first and last samples does not.
    """
    noisy = [100.0, 130.0, 95.0, 135.0, 98.0, 132.0, 101.0, 129.0]
    assert noisy[-1] > noisy[0]
    assert not cli.leaks(_samples(noisy))


def test_a_resolvable_ramp_is_reported_as_a_leak() -> None:
    """And the opposite must be reachable, or the probe would exit zero on anything."""
    assert cli.leaks(_samples([100.0 + 20.0 * index for index in range(12)]))


# --- the run's own arguments ---------------------------------------------------------------------


class _Started(Exception):
    """Raised in place of `main`'s first post-parse statement, so parsing can be observed alone."""


@pytest.fixture
def parse_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `logging.basicConfig` — `main`'s first statement after `parse_args` — with a marker.

    That one line is the boundary every test below is about: reaching it means argparse accepted
    the arguments, and not reaching it means argparse refused them. Standing the real run up
    instead would need the whole live lane, and a `--batch 0` run would never return at all.
    """
    monkeypatch.setattr(logging, "basicConfig", _raise_started)


def _raise_started(**_: object) -> None:
    """Stand in for the first thing `main` does once its arguments are accepted."""
    raise _Started


@pytest.mark.parametrize("value", ["0", "-5"])
@pytest.mark.parametrize("flag", ["--batch", "--turns"])
@pytest.mark.usefixtures("parse_only")
def test_a_run_that_could_never_end_is_refused_before_anything_starts(
    flag: str,
    value: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--batch 0` was an unbounded loop inside the tool built to account for unbounded growth.

    `batch = min(args.batch, args.turns - done)` is zero, so `done` never advances, `while done <
    args.turns` never ends, and each iteration appends another full type histogram — every live
    object in the process, by name, forever. A negative batch walks `done` backwards into the same
    loop. `--turns` is the bound on that loop, so a zero or negative one drives nothing and leaves
    a single sample, which `report` can only refuse to fit.

    These two flags only. `--warmup` counts turns as well and is *not* one of them — see below.
    """
    with pytest.raises(SystemExit) as exit_code:
        cli.main([flag, value])

    assert exit_code.value.code == 2
    assert "must be at least 1" in capsys.readouterr().err
    assert cli._positive("25") == 25


@pytest.mark.usefixtures("parse_only")
def test_a_cold_start_is_a_run_the_probe_accepts() -> None:
    """`--warmup 0` measures from a cold process, which is a question worth asking.

    It is what puts the one-time costs the warm-up exists to exclude — the agent pool, each
    connector's first session, the caches, the allocator's arenas — *inside* the fitted series
    instead of ahead of it. Nothing about it is unbounded: `_drive(client, 0)` returns immediately,
    the first sample is taken at turn 0, and `--turns` bounds the loop exactly as always.
    """
    assert cli._non_negative("0") == 0

    with pytest.raises(_Started):
        cli.main(["--warmup", "0"])


@pytest.mark.usefixtures("parse_only")
def test_a_warm_up_below_zero_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """The only warm-up that is nonsense: it puts every turn count in the report below zero.

    It also inflates `span`, so every per-turn rate — the column a leak hunt is read off — comes
    out smaller than it is.
    """
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["--warmup", "-5"])

    assert exit_code.value.code == 2
    assert "cannot be negative" in capsys.readouterr().err


def _vm_rss_kb() -> float:
    """The kernel's own resident-set figure for this process, in kilobytes."""
    for line in _STATUS.read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return float(line.split()[1])
    raise AssertionError("/proc/self/status carries no VmRSS line")
