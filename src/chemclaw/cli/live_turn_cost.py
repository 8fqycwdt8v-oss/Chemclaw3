"""`python -m chemclaw.cli.live_turn_cost` — score `turn_cost_ratio` over turns this system ran.

**The gap this closes.** `turn_cost_ratio` is a good metric attached to a case that cannot exercise
it. `data/evals/cases/autonomy-turn-cost.md` commits four literal `TurnCost` records, so the metric
returns 0.9845458333333333 to every decimal whatever changes in the agent — the case file says so
itself, the metric's docstring says so, and `evals.baseline.render_comparison` labels the row
`pinned`. A 32% growth of the static prefix, the regression `tests/test_context_floor.py` exists to
catch, leaves that row untouched. A cost metric that cannot see a cost regression is the shape
`D-2026-09-14-a-gate-nothing-has-failed-is-a-gate-that-cannot-fail` names one layer up.

**What makes it measurable is that the bill has to follow the request.** The obvious wiring — read
whatever `turn_costs` rows the lane happens to hold — produces a second gate that cannot fire, and
that is measured rather than supposed: every behaviour in `cli/storm_behaviours.py` but two names a
*constant* `input_tokens` (900), so a turn against the default behaviour bills 900 whether the
prefix is 40,000 tokens or 400,000. `WORKLOAD` therefore carries `h-size-billed`'s marker, the one
behaviour whose bill is `input_tokens_per_char` over the serialized request — which is what puts
the instructions, the skills listing and every bound tool schema into the number. Against a real
gateway the marker is inert prose and the gateway bills for itself, which is the same measurement
by a better instrument.

**And the workload is driven here rather than read.** Cost is a property of (system, workload), so
comparing this run against a recorded one requires the workload to be the same one — a reader over
whatever rows a lane left behind would compare two different questions and call the difference a
regression. The turns are scripted, fixed in this file, and the session they open is the only one
this command reads back.

The recorded expectation is an ordinary eval case, `CASE_ID`, emitted by `--emit`. That keeps one
format, one loader and one metric: `make eval` prints it beside the arithmetic fixture with its own
provenance, and this command re-measures and compares within `eval_drift_epsilon`. Refreshing it is
the same deliberate act as `make eval-baseline`.

The arithmetic fixture stays, deliberately. It is the only case that exercises the cache-write and
cache-read weighting and the counting of a turn that never answered — a mock lane produces none of
those — and it is honest about being a fixture.

Exit codes: 0 within the band, 1 on a worsening drift, 3 when the lane could not be reached (never
counted as a pass, the posture `live_probes` and `validate_template_args_live` already take).
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.turn_cost import TurnCost
from chemclaw.evals.baseline import drift_band
from chemclaw.evals.harness import load_eval_cases
from chemclaw.evals.live import open_session
from chemclaw.evals.metric import EvalCase, get_metric

#: The case this command records into and compares against.
CASE_ID = "autonomy-turn-cost-measured"

#: The marker that selects the one mock behaviour whose bill follows the request. Inert against a
#: real gateway — see the module docstring.
BEHAVIOUR_MARKER = "[[h-size-billed]]"

#: The scripted workload. Fixed here rather than read from a corpus because the comparison is only
#: meaningful between two runs of the *same* questions, and a corpus is a thing other waves edit.
WORKLOAD: tuple[str, ...] = (
    f"{BEHAVIOUR_MARKER} What do we know about Suzuki couplings in this corpus?",
    f"{BEHAVIOUR_MARKER} Which ligand won those screens, and on what evidence?",
    f"{BEHAVIOUR_MARKER} Summarise that for a process chemist in two sentences.",
)

#: Billed token-equivalents the ratio is taken against. A constant of the *case*, carried through
#: every re-measurement so the recorded and the fresh value are the same quantity — a denominator
#: that moved with the numerator would make every ratio read 1.0 and measure nothing.
BASELINE_TOKENS = 1_000_000

#: The fields the emitted case carries: what the turn cost, and the correlation id that joins it
#: back to the trail. Nothing else. `TurnCost` has 28 fields and `model_dump()` writes them
#: all, so the first emitted case published `model: ""` and `outcome: "unknown"` beside real
#: numbers — defaults wearing the appearance of measurements, in a file whose whole claim is that
#: every number in it was measured. `actor` and `session_id` are dropped for a second reason: they
#: identify a person and a conversation, and neither is part of what a turn cost.
_EMITTED = frozenset(
    {
        "correlation_id",
        "profile",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "estimated_tokens",
        "duration_seconds",
        "completed",
        # Not read by the metric, and carried anyway: these three are what turn an unexplained
        # number into a diagnosis. Measured across two boots of the same commit, the same three
        # questions cost 900,198 and 429,076 billed token-equivalents, and the ledger says how they
        # differed — `context_unreducible` true on every turn of the expensive run and false on
        # every turn of the cheap one, with the model calling a tool on 3 of 3 turns against 1 of 3.
        "tool_calls",
        "compacted",
        "context_unreducible",
    }
)

_READ = """
    SELECT correlation_id, session_id, actor, profile, input_tokens, output_tokens,
           cache_read_tokens, cache_write_tokens, estimated_tokens, duration_seconds, completed,
           tool_calls, compacted, context_unreducible
    FROM turn_costs
    WHERE session_id = %s
    ORDER BY recorded_at
"""


async def _recorded(session_id: str) -> list[TurnCost]:
    """The ledger rows this run's own session produced, oldest first.

    `turn_id` is deliberately not selected: it is minted per record and would make the emitted case
    differ on every run for a reason that is not a cost. `TurnCost` mints a fresh one, which is
    correct — the case is a record of what a turn cost, not of which row said so.
    """
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_READ, (session_id,))
            rows = await cur.fetchall()
    return [
        TurnCost(
            correlation_id=str(correlation_id),
            session_id=str(session_id_),
            actor=str(actor),
            profile=str(profile),
            input_tokens=int(inp),
            output_tokens=int(out),
            cache_read_tokens=int(cache_read),
            cache_write_tokens=int(cache_write),
            estimated_tokens=int(estimated),
            duration_seconds=float(duration),
            completed=bool(completed),
            tool_calls=None if tool_calls is None else int(tool_calls),
            compacted=bool(compacted),
            context_unreducible=bool(unreducible),
        )
        for (
            correlation_id,
            session_id_,
            actor,
            profile,
            inp,
            out,
            cache_read,
            cache_write,
            estimated,
            duration,
            completed,
            tool_calls,
            compacted,
            unreducible,
        ) in rows
    ]


async def _drive(base_url: str) -> str:
    """Ask every question in `WORKLOAD` on one session and return its id.

    The event stream is drained and discarded: this command grades nothing, and folding the stream
    through `evals.live.run_turn` would tie a cost measurement to citation scoring, tool
    expectations and a `Probe`'s graded shape — none of which has anything to say about what a turn
    cost. `open_session` *is* reused, because how a session is opened is exactly the part that must
    not diverge.
    """
    timeout = httpx.Timeout(settings.live_probe_timeout_seconds)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
        session_id = await open_session(client)
        for question in WORKLOAD:
            async with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": question}
            ) as response:
                response.raise_for_status()
                async for _ in response.aiter_lines():
                    pass
        return session_id


def _case(turns: list[TurnCost]) -> EvalCase:
    """The turns as the case the metric reads — the one shape, built once."""
    return EvalCase(
        id=CASE_ID,
        metrics=["turn_cost_ratio"],
        output={"turns": [turn.model_dump(include=set(_EMITTED)) for turn in turns]},
        reference={"baseline_tokens": BASELINE_TOKENS},
    )


def _recorded_value() -> float | None:
    """The committed case's score, or None when it has not been emitted yet."""
    cases = {case.id: case for case in load_eval_cases(settings.eval_case_dir)}
    case = cases.get(CASE_ID)
    return None if case is None else get_metric("turn_cost_ratio")(case).value


def _emit(turns: list[TurnCost], path: Path) -> None:
    """Write the measured turns out as the recorded case, front matter and prose together."""
    body = {
        "id": CASE_ID,
        "metrics": ["turn_cost_ratio"],
        "output": {"turns": [turn.model_dump(include=set(_EMITTED)) for turn in turns]},
        "reference": {"baseline_tokens": BASELINE_TOKENS},
    }
    path.write_text(
        "---\n"
        + json.dumps(body, indent=2)
        + "\n---\n"
        + f"""**Measured, not written.** Every number above came out of `turn_costs` after
`python -m chemclaw.cli.live_turn_cost --emit` drove {len(WORKLOAD)} scripted turns through a
running front door; none of it was chosen. That is the whole difference between this case and
`autonomy-turn-cost`, which commits invented records and therefore scores a constant.

It is still a committed literal, and so is still `pinned` in `make eval-baseline-check` — a file
cannot be anything else. What makes the metric score the *system* is the command that produced it:
`make live-turn-cost` re-drives the same three questions, scores the fresh ledger rows with the same
metric, and fails on a worsening drift past `eval_drift_epsilon` against the value here. The static
prefix is inside that number, because the workload asks the mock behaviour whose bill follows the
serialized request.

Refresh it the way a baseline is refreshed: deliberately, in a reviewed commit, when the cost
genuinely changed and the change is the intended one.
""",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """Drive the workload, score what it cost, and compare against the recorded case."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # `live_probe_base_url` rather than `service_host`/`service_port`: the first is a *bind*
    # address (0.0.0.0 is not a destination — the egress guard refused it) and the second is the
    # chart's port, while the live lane serves on its own. One setting already names where the lane
    # answers, and `live_probes` reads the same one.
    parser.add_argument(
        "--base-url",
        default=settings.live_probe_base_url,
        help="the running front door (default: `live_probe_base_url`, the lane's own address)",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="record this run as the case the next run is compared against",
    )
    args = parser.parse_args(argv)

    try:
        session_id = asyncio.run(_drive(args.base_url))
        turns = asyncio.run(_recorded(session_id))
    except (httpx.HTTPError, OSError) as exc:
        # `httpx.HTTPError` is the front door and `OSError` is the database — `core/db` maps an
        # unreachable or saturated one onto `ConnectionError`, which is an `OSError`. A driver
        # error that is *not* one of those is deliberately not caught: `chemclaw.cli` may not
        # import `psycopg` (`tests/test_third_party_layering.py`, and `cli/explain.py`'s
        # `_is_database_refusal` is the workaround where one is genuinely needed), and a
        # malformed query reported as "could not reach the live lane" would be a measurement
        # failure wearing an outage's message.
        print(f"could not reach the live lane ({exc}); nothing was measured")
        return 3
    if not turns:
        print(f"the lane ran {len(WORKLOAD)} turn(s) and the ledger holds none of them")
        return 3

    result = get_metric("turn_cost_ratio")(_case(turns))
    print(f"live turn_cost_ratio = {result.value:.6g} — {result.provenance}")
    # Beside the value rather than only in the emitted file: a drift this command reports is read
    # by somebody deciding whether a commit made the system more expensive, and the honest answer
    # is sometimes "the context policy was in a different regime". Stating it is what separates
    # those two readings without anybody having to open the database.
    print(
        f"regime: {sum(1 for t in turns if t.context_unreducible)}/{len(turns)} turn(s) "
        f"unreducible, {sum(1 for t in turns if t.compacted)} compacted, "
        f"{sum(t.tool_calls or 0 for t in turns)} tool call(s)"
    )

    if args.emit:
        path = Path(settings.eval_case_dir) / f"{CASE_ID}.md"
        _emit(turns, path)
        print(f"recorded {len(turns)} measured turn(s) to {path}")
        return 0

    recorded = _recorded_value()
    if recorded is None:
        print(f"no recorded case {CASE_ID!r} to compare against; run with --emit first")
        return 3
    band = drift_band(recorded, settings.eval_drift_epsilon)
    delta = result.value - recorded
    print(f"recorded {recorded:.6g}, delta {delta:+.4g}, band {band:.4g}")
    # Lower is better, so only an increase past the band is a failure — a command that failed on a
    # cost *improvement* is one everybody learns to re-run (`baseline.is_worsening`, same rule).
    if delta > band:
        print("**WORSE**: this workload costs materially more than the recorded run")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
