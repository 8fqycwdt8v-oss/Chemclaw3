"""The run half of the delegation experiment: drive each arm, observe what it did, record it.

Kept separate from the comparator (`evals/delegation.py`), which runs no model; the two meet at
`ArmRun`.

Each arm is a profile plus a server posture (`model_routes["helper"]`, `agent_peer_roster`) that the
process building the agent reads, so a client cannot switch it. `ARMS` records what each arm needs
and the report prints it; an arm whose posture is absent yields undelegated repeats, which
intention-to-treat reports rather than drops.

`delegated` is read off `audit_events`, the authoritative per-call record: one row per tool call,
drained before the answer event, carrying `agent` and `outcome`. A refused call never ran and is not
a delegation; a call that ran and failed is. `billed_tokens` is the whole turn's bill from
`turn_costs`, which covers both the helper's reading and the caller's.

Against `cli.mock_llm`, arms differ only if a `[[name]]` marker is injected, so the runner injects
one when `settings.llm_base_url` is `MOCK_BASE_URL`. A number produced against the mock is evidence
about this runner only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.markdown import render_table
from chemclaw.core.turn_cost import TurnCost
from chemclaw.evals.autonomy import billed_tokens
from chemclaw.evals.delegation import (
    BASELINE_ARM,
    ArmRun,
    DelegationReport,
    NoComparableTask,
    aggregate_runs,
    compare_arms,
)
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe, ProbeSet
from chemclaw.evals.tool_utility import VERDICT_SCORES

logger = logging.getLogger(__name__)

#: The corpus this suite asks, inside `settings.live_probe_dir`, so a missing file fails at a
#: searchable name.
DELEGATION_PROBE_FILE = "delegation.yaml"

#: Which act counts as this arm having taken its treatment.
#:
#: `helper` is a `task` call and `handoff` a `transfer_to_…` call — different acts. `any` is the
#: baseline's: with a peer arm in the run every arm has a peer roster bound, and a baseline that
#: handed off is no more a baseline than one that spawned a helper.
TREATMENTS = ("helper", "handoff", "any")


class ArmSpec(BaseModel):
    """One arm: the name the comparator knows it by, the profile it asks for, and what it needs.

    `profile` is not unique across arms and must not be: `helper` and `helper-routed` are the same
    agent asked of two deployments, which is exactly what `AgentProfile.model_route` is for — the
    route key carries no authority and a model id in a profile file would be a site's model name
    checked into this repository.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str = Field(min_length=1)
    #: The front-door profile this arm opens its sessions with (`POST /sessions` `profile`).
    profile: str = Field(min_length=1)
    #: Which tool call counts as this arm's treatment — one of `TREATMENTS`.
    treatment: str
    #: What the *front door* must have been started with for this arm to be able to comply. Prose,
    #: printed in the report, because nothing this client can do makes it true.
    posture: str = ""
    #: The `cli.mock_llm` behaviour this arm selects when the gateway is that mock. Inert against a
    #: real gateway, where nothing is injected at all.
    mock_behaviour: str = Field(min_length=1)


#: The four arms, in report order, baseline first.
ARMS: tuple[ArmSpec, ...] = (
    ArmSpec(
        arm=BASELINE_ARM,
        profile="no-helper",
        treatment="any",
        posture="nothing — this arm is an ask, and whether it complied is the observation",
        mock_behaviour="d-no-helper",
    ),
    ArmSpec(
        arm="helper",
        profile="with-helper",
        treatment="helper",
        posture="nothing — the helper runs on the caller's own model",
        mock_behaviour="d-delegates",
    ),
    ArmSpec(
        arm="helper-routed",
        profile="with-helper",
        treatment="helper",
        posture='CHEMCLAW_MODEL_ROUTES=\'{"helper": "<a smaller model>"}\' on the front door',
        mock_behaviour="d-delegates",
    ),
    ArmSpec(
        arm="peer",
        profile="with-peer",
        treatment="handoff",
        posture="CHEMCLAW_AGENT_PEER_ROSTER naming at least one other profile, on the front door",
        mock_behaviour="d-hands-off",
    ),
)


def arm_by_name(name: str) -> ArmSpec:
    """The arm called `name`.

    Raises:
        KeyError: No arm is called that; a typo in `--arms` must not silently shrink the report.
    """
    for spec in ARMS:
        if spec.arm == name:
            return spec
    raise KeyError(f"no delegation arm called {name!r}; known: {[s.arm for s in ARMS]}")


def load_delegation_probes(probe_dir: str | None = None) -> list[Probe]:
    """The delegation corpus, from its own file rather than the whole probe directory.

    The directory holds every suite's corpora; this protocol must ask only its own.

    Raises:
        FileNotFoundError: The corpus is absent, rather than a run of zero probes reporting zero
            failures.
    """
    directory = Path(probe_dir if probe_dir is not None else settings.live_probe_dir)
    path = directory / DELEGATION_PROBE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"the delegation suite needs {path}, which does not exist")
    return ProbeSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).probes


def treatment_tools(treatment: str, tools: Collection[str]) -> frozenset[str]:
    """Which of `tools` are the act `treatment` names.

    Derived from the producers rather than spelled here: `task` from building `SubAgentMiddleware`
    (`agent/chemclaw_agent.subagent_tool_names`, since upstream exports no constant) and handoffs
    via `agent/handoff.is_handoff_tool_name`. A stale literal would make an arm that delegated read
    as one that declined.

    Raises:
        ValueError: `treatment` is not one of `TREATMENTS`.
    """
    if treatment not in TREATMENTS:
        raise ValueError(f"unknown treatment {treatment!r}; known: {list(TREATMENTS)}")
    # Lazily, on the edge `tests/test_layering.py` already allows: importing this module must not
    # cost a compiled middleware or the connector registry.
    from chemclaw.agent.chemclaw_agent import subagent_tool_names
    from chemclaw.agent.handoff import is_handoff_tool_name

    spawns = subagent_tool_names()
    hands_off = {name for name in tools if is_handoff_tool_name(name)}
    if treatment == "helper":
        return frozenset(name for name in tools if name in spawns)
    if treatment == "handoff":
        return frozenset(hands_off)
    return frozenset(hands_off | {name for name in tools if name in spawns})


_RAN_TOOLS = """
    SELECT session_id, tool
    FROM audit_events
    WHERE session_id = ANY(%s) AND outcome <> %s
    GROUP BY session_id, tool
"""


async def tools_that_ran(session_ids: Sequence[str]) -> dict[str, frozenset[str]]:
    """Every tool each session actually ran, from the audit trail, excluding refusals.

    `agent/audit.REFUSED` is the one outcome meaning the tool body never ran. Every other outcome
    (`ok`, `error`, `returned_failure`, `cancelled`) is a call that was made; excluding failures
    would select on the treatment's success.

    Returns:
        Session id → the tool names it ran. A session with no rows is absent rather than present
        and empty, so a caller can tell "ran nothing" from "was never asked".
    """
    from chemclaw.agent.audit import REFUSED

    if not session_ids:
        return {}
    ran: dict[str, set[str]] = {}
    # `audit_events` lives on `postgres_dsn` (where `PostgresAuditSink` writes), not the session
    # store's DSN; the two may be split.
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RAN_TOOLS, (list(session_ids), REFUSED))
            for session_id, tool in await cur.fetchall():
                ran.setdefault(str(session_id), set()).add(str(tool))
    return {session_id: frozenset(tools) for session_id, tools in ran.items()}


_TURN_COST = """
    SELECT session_id, correlation_id, input_tokens, output_tokens,
           cache_read_tokens, cache_write_tokens
    FROM turn_costs
    WHERE session_id = ANY(%s)
"""


async def billed_by_session(session_ids: Sequence[str]) -> dict[str, int]:
    """What the ledger says each session's turns cost, in billed token-equivalents.

    Summed over the session's rows, since a retried repeat books two. Weighted by
    `evals/autonomy.billed_tokens`, the one definition of this arithmetic.

    Returns:
        Session id → its bill. A session with no row is absent, never zero: an unwritten row is a
        hole in the data, not a free turn.
    """
    if not session_ids:
        return {}
    totals: dict[str, float] = {}
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TURN_COST, (list(session_ids),))
            rows = await cur.fetchall()
    for session_id, correlation_id, inp, out, cache_read, cache_write in rows:
        cost = TurnCost(
            correlation_id=str(correlation_id),
            input_tokens=int(inp),
            output_tokens=int(out),
            cache_read_tokens=int(cache_read),
            cache_write_tokens=int(cache_write),
        )
        totals[str(session_id)] = totals.get(str(session_id), 0.0) + billed_tokens(cost)
    return {session_id: round(total) for session_id, total in totals.items()}


async def billed_by_session_when_booked(session_ids: Sequence[str]) -> dict[str, int]:
    """`billed_by_session`, waited for — the ledger write is booked off the turn's hot path.

    `record_turn_cost` writes in its own task, so the row is eventually consistent with the stream
    closing. Polled ten times within `eval_delegation_ledger_wait_seconds`; a row still missing
    after that is a hole.
    """
    wanted = set(session_ids)
    attempts = 10
    interval = settings.eval_delegation_ledger_wait_seconds / attempts
    booked: dict[str, int] = {}
    for attempt in range(attempts):
        booked = await billed_by_session(session_ids)
        if wanted <= set(booked):
            return booked
        if interval > 0 and attempt < attempts - 1:
            await asyncio.sleep(interval)
    missing = sorted(wanted - set(booked))
    logger.warning(
        "%d of %d session(s) have no `turn_costs` row after %.1fs: %s. Each is a hole in the data "
        "rather than a cost of zero.",
        len(missing),
        len(wanted),
        settings.eval_delegation_ledger_wait_seconds,
        missing,
    )
    return booked


class ArmRepeat(BaseModel):
    """One (task, arm, repeat) as it was driven, before the two ledger reads land on it.

    Its own record rather than a tuple because the hole cases need naming: a repeat whose verdict
    could not be obtained and a repeat whose cost was never booked are different failures, and only
    a named record can carry which one dropped it.
    """

    model_config = ConfigDict(extra="forbid")

    arm: str
    repeat: int
    outcome: ProbeOutcome
    judgement: Judgement


class ArmRunSet(BaseModel):
    """The runs one campaign recorded, plus every repeat that could not become one."""

    model_config = ConfigDict(extra="forbid")

    runs: list[ArmRun] = Field(default_factory=list)
    #: `<arm>/<task>#<repeat>` for each repeat the judge could not grade. `ungraded` is the absence
    #: of a grade, never a bad one, so such a repeat cannot become an `ArmRun`.
    ungraded: list[str] = Field(default_factory=list)
    #: `<arm>/<task>#<repeat>` for each repeat with no `turn_costs` row — see `billed_by_session`.
    unbilled: list[str] = Field(default_factory=list)


def assemble_runs(
    repeats: Iterable[ArmRepeat],
    ran: Mapping[str, frozenset[str]],
    billed: Mapping[str, int],
    treatments: Mapping[str, str],
) -> ArmRunSet:
    """Fold each driven repeat plus the two ledger reads into an `ArmRun`, or into a named hole.

    Args:
        repeats: What was driven, in any order.
        ran: Session id → the tools that session ran (`tools_that_ran`).
        billed: Session id → its bill (`billed_by_session`).
        treatments: Arm → the treatment name whose tools count as that arm having delegated.

    Returns:
        The runs, and the repeats that could not become one. Nothing is dropped for an arm's
        behaviour: `delegated=False` is recorded and reported as compliance.
    """
    result = ArmRunSet()
    for repeat in repeats:
        label = f"{repeat.arm}/{repeat.outcome.probe_id}#{repeat.repeat}"
        if repeat.judgement.verdict not in VERDICT_SCORES:
            result.ungraded.append(label)
            continue
        session_id = repeat.outcome.session_id
        if session_id not in billed:
            result.unbilled.append(label)
            continue
        took = treatment_tools(treatments[repeat.arm], ran.get(session_id, frozenset()))
        result.runs.append(
            ArmRun(
                task_id=repeat.outcome.probe_id,
                arm=repeat.arm,
                quality=VERDICT_SCORES[repeat.judgement.verdict],
                billed_tokens=billed[session_id],
                wall_clock_seconds=repeat.outcome.latency_seconds,
                delegated=bool(took),
            )
        )
    return result


def load_recorded_runs(paths: Sequence[Path]) -> list[ArmRun]:
    """Every `ArmRun` in one or more recorded `runs.json` files, concatenated.

    Lets arms recorded in separate processes or on different days aggregate into one report.

    Raises:
        ValueError: A file holds no runs.
    """
    runs: list[ArmRun] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        loaded = [ArmRun.model_validate(item) for item in payload]
        if not loaded:
            raise ValueError(f"{path} holds no recorded runs")
        runs.extend(loaded)
    return runs


def _bucket_rows(report: DelegationReport) -> list[list[str]]:
    """The four compliance buckets plus `incomplete`, as rows — every one present even when empty.

    An empty bucket is a real result and must be distinguishable from one the report forgot.
    """
    return [
        [
            "compared (carried the comparison)",
            str(report.compared),
            ", ".join(comparison.task_id for comparison in report.comparisons),
        ],
        [
            "undelegated (the arm never delegated)",
            str(len(report.undelegated)),
            ", ".join(report.undelegated),
        ],
        [
            "partially_delegated (some repeats, not others)",
            str(len(report.partially_delegated)),
            ", ".join(report.partially_delegated),
        ],
        [
            "contaminated (the **baseline** delegated)",
            str(len(report.contaminated)),
            ", ".join(report.contaminated),
        ],
        [
            "incomplete (a missing arm, or too few repeats)",
            str(len(report.incomplete)),
            ", ".join(report.incomplete),
        ],
    ]


def render_report(
    reports: Mapping[str, DelegationReport],
    runs: Sequence[ArmRun],
    run_set: ArmRunSet,
    provenance: Sequence[str],
) -> str:
    """One report per arm under test, with the compliance buckets and both cost axes beside quality.

    Quality, tokens and wall clock are never folded into one figure: "cheaper but worse" and "better
    but slower" are different answers.
    """
    lines = ["# The delegation experiment — one report per arm", ""]
    lines.extend(f"- {line}" for line in provenance)
    lines.append("")
    lines.append("## What was recorded")
    lines.append("")
    aggregates = aggregate_runs(runs)
    lines.append(
        render_table(
            ["task", "arm", "repeats", "delegated in", "quality", "billed tokens", "wall clock s"],
            [
                [
                    aggregate.task_id,
                    aggregate.arm,
                    str(aggregate.repeats),
                    f"{aggregate.delegated_in}/{aggregate.repeats}",
                    f"{aggregate.quality:+.1f}",
                    f"{aggregate.billed_tokens:,.0f}",
                    f"{aggregate.wall_clock_seconds:.1f}",
                ]
                for aggregate in aggregates
            ],
            align="llrrrrr",
        )
    )
    if run_set.ungraded or run_set.unbilled:
        lines += [
            "",
            "Repeats that could not become a run — **holes in the data, not results**: "
            f"{len(run_set.ungraded)} ungraded ({', '.join(run_set.ungraded) or '-'}), "
            f"{len(run_set.unbilled)} unbilled ({', '.join(run_set.unbilled) or '-'}).",
        ]
    for arm, report in reports.items():
        lines += [
            "",
            f"## `{arm}` against `{report.baseline_arm}`",
            "",
            render_table(
                ["task", "quality Δ", "token Δ", "wall-clock Δ", "verdict", "delegated"],
                [
                    [
                        comparison.task_id,
                        f"{comparison.quality_delta:+.1f}",
                        f"{comparison.token_delta:+,.0f}",
                        f"{comparison.wall_clock_delta:+.1f}",
                        comparison.verdict,
                        f"{comparison.delegated_in}/{comparison.repeats}",
                    ]
                    for comparison in report.comparisons
                ],
                align="lrrrlr",
            ),
            "",
            render_table(["outcome", "tasks", "which"], _bucket_rows(report)),
            "",
            # `None` is not 1.0 and must not be rendered as one — `DelegationReport`'s own field
            # comment says so: it means every compared task had a zero baseline on that axis.
            "- median token ratio (arm / baseline, below 1.0 is cheaper): "
            + (
                "**no usable ratio**"
                if report.median_token_ratio is None
                else f"**{report.median_token_ratio:.3f}**"
            ),
            "- median wall-clock ratio: "
            + (
                "**no usable ratio**"
                if report.median_wall_clock_ratio is None
                else f"**{report.median_wall_clock_ratio:.3f}**"
            ),
            f"- quality: {len(report.quality.helped)} helped, {len(report.quality.hurt)} hurt, "
            f"{len(report.quality.no_effect)} no effect, net {report.quality.net_delta:+.4g}",
        ]
    return "\n".join(lines) + "\n"


def compare_every_arm(
    runs: Sequence[ArmRun],
    arms: Sequence[str],
    minimum_repeats: int,
) -> tuple[dict[str, DelegationReport], dict[str, str]]:
    """One `DelegationReport` per non-baseline arm, plus the arms that could not be reported on.

    Each arm is compared separately so per-arm differences stay visible.

    Returns:
        `(reports, refused)` — the reports by arm, and arm → why no report exists for it. A
        `NoComparableTask` is carried rather than raised, so one empty arm does not cost the
        others' reports.
    """
    reports: dict[str, DelegationReport] = {}
    refused: dict[str, str] = {}
    for arm in arms:
        if arm == BASELINE_ARM:
            continue
        try:
            reports[arm] = compare_arms(
                runs, arm, baseline_arm=BASELINE_ARM, minimum_repeats=minimum_repeats
            )
        except (NoComparableTask, ValueError) as exc:
            refused[arm] = str(exc)
    return reports, refused
