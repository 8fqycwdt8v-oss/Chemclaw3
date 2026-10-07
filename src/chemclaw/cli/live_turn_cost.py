"""`python -m chemclaw.cli.live_turn_cost` — score `turn_cost_ratio` over turns this system ran.

The committed `autonomy-turn-cost` case is a fixed arithmetic fixture, so it cannot see a cost
regression such as prefix growth. This command drives a fixed scripted `WORKLOAD` against the live
lane on one session and scores the turns that session recorded — cost is a property of (system,
workload), so the workload must be the same to compare runs.

`WORKLOAD` carries `h-size-billed`'s marker, the mock behaviour that bills per character of the
serialized request, so instructions, skills listing and tool schemas reach the number; against a
real gateway the marker is inert and the gateway bills for itself.

The expectation is an ordinary eval case, `CASE_ID`, written by `--emit` and compared within
`eval_drift_epsilon`; refreshing it is as deliberate as `make eval-baseline`. The arithmetic
fixture stays, as the only case exercising cache weighting and unanswered turns.

Exit codes: 0 within the band, 1 on a worsening drift, 3 when the lane could not be reached (never
counted as a pass).
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

#: Billed token-equivalents the ratio is taken against — a constant of the case, so the recorded
#: and fresh values are the same quantity.
BASELINE_TOKENS = 1_000_000

#: The fields the emitted case carries: what the turn cost and the correlation id joining it to the
#: trail. `model_dump()` would also write unmeasured defaults, and `actor`/`session_id` identify a
#: person and a conversation, which are no part of a cost.
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
        # Not read by the metric; carried because they explain a cost difference between runs.
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

    `turn_id` is not selected: it is minted per record and would make the emitted case differ on
    every run for a reason that is not a cost.
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

    The event stream is drained and discarded — this grades nothing — but `open_session` is reused
    so
    session opening cannot diverge from the probes.
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
    # `live_probe_base_url`, not `service_host`/`service_port`: those are a bind address and the
    # chart's
    # port, while the live lane serves on its own.
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
        # `httpx.HTTPError` is the front door; `OSError` is the database (`core/db` raises
        # `ConnectionError`). Other driver errors are deliberately not caught, so a malformed query
        # is not
        # reported as an outage; `chemclaw.cli` may not import `psycopg`.
        print(f"could not reach the live lane ({exc}); nothing was measured")
        return 3
    if not turns:
        print(f"the lane ran {len(WORKLOAD)} turn(s) and the ledger holds none of them")
        return 3

    result = get_metric("turn_cost_ratio")(_case(turns))
    print(f"live turn_cost_ratio = {result.value:.6g} — {result.provenance}")
    # Printed beside the value so a reader can tell a regression from a different context-policy
    # regime without opening the database.
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
