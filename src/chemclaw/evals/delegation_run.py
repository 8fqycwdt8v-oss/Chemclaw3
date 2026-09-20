"""The run half of the delegation experiment: drive each arm, observe what it did, record it.

`evals/delegation.py` is the comparator and says in its own closing paragraph that nothing
constructed an `ArmRun`, recorded `delegated`, or built the `no-helper` arm at all. This module is
that half. It deliberately stays out of the comparator, whose whole claim to being testable without
a gateway is that it runs no model — so the two halves meet at `ArmRun` and nowhere else.

**Every arm is a profile plus a server posture, and the second half is not something a client can
supply.** A helper's model is `settings.model_routes["helper"]` and a peer roster is
`settings.agent_peer_roster`; both are read by the process that *builds the agent*, so this runner
cannot switch them by asking the front door differently. `ARMS` therefore records what each arm
needs of the deployment, the report prints it, and an arm whose posture is absent produces exactly
what intention-to-treat says it should: repeats that did not take the treatment, reported rather
than dropped.

**`delegated` is read off `audit_events`, which is the authoritative per-turn record of a tool
call.** Three other things know about a `task` call and none of them is the record:

* the SSE stream's `tool_call` event is what the *browser* was told, assembled by
  `api/graph_stream.py` — a view, and one whose contents have been wrong about a turn before
  (`STREAM-1`, tool-call events carrying no arguments);
* `turn_costs.tool_calls` is a **count**, so it cannot say *which* tool;
* `ChemclawState` is in the checkpoint, keyed by thread, and holds no per-tool record at all.

`agent/audit.py` wraps every registered tool from one place, writes one row per call with the tool's
own name, and `api/runner.py` drains the sink before the turn's answer event — so the row is
queryable the moment the stream ends. It also carries `agent`, so a helper's own calls are
distinguishable from its caller's, and `outcome`, which is how a *refused* call is told from one
that ran. That last distinction is the one this module makes: a call the plan gate or the
authorization gate stopped never entered the helper's graph, so it is not a delegation; a call that
entered and then failed **is** one that happened, and under ITT a failed delegation dilutes the
effect toward zero, which is the conservative direction.

**`billed_tokens` is read off `turn_costs`, and the quantity is the whole turn's bill.** That is the
right quantity rather than a compromise: the cost claim for delegation is that the helper's reading
is billed in the helper's own context while only its report is billed in the caller's, and both
halves land on the same turn — so the turn total is exactly what "did delegation pay" asks about. An
estimator would not do, for the reason `ArmRun.billed_tokens`' own comment gives.

**The marker injection is for the scripted double and is derived, never configured.** Against
`cli.mock_llm` a turn's behaviour is selected by a `[[name]]` marker in the message, so a run
against the mock has to inject one or every arm gets the catalogue's default and the arms are the
same arm. Whether to inject is asked of `settings.llm_base_url` against `MOCK_BASE_URL` — the same
question `cli/live_probes._gateway_line` asks, and for the same reason: a transcribed address agrees
with the default on the day it is written and cannot follow it. Against a real gateway nothing is
injected and the arms differ by their profile alone. **A number this produces against the mock is
evidence about this runner and about nothing else.**
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

#: The corpus this suite asks, inside `settings.live_probe_dir`. A filename beside a configured
#: directory rather than a setting of its own, which is the shape `cli/live_probes._M12_SUITES`
#: already uses and argues for: a suite whose file is missing then fails at a name a reader can
#: search for instead of raising `FileNotFoundError` on a path nobody wrote down.
DELEGATION_PROBE_FILE = "delegation.yaml"

#: Which act counts as this arm having taken its treatment.
#:
#: `helper` is a `task` call and `handoff` is a `transfer_to_…` call, because a helper reads and
#: reports while a peer keeps the conversation — different acts, not two flavours of one
#: (`D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`). `any` is the
#: baseline's, and it is the union rather than `helper` for a reason that only bites once a peer arm
#: exists: the arms share one front door, so a run that includes the peer arm has a peer roster
#: bound for *every* arm, and a baseline that handed the conversation away is no more a baseline
#: than one that spawned a helper.
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
#:
#: Four rather than three because the BACKLOG row added the peer arm after
#: `D-2026-09-19-a-handoff-redistributes-the-turns-authority-it-cannot-extend-it`, and it belongs on
#: this row rather than on one of its own: a row per arm is what that row was four of.
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
        KeyError: No arm is called that. Named rather than silently skipped, because a typo in
            `--arms` would otherwise produce a report over fewer arms than were asked for — the
            coverage lie every exit code in this lane exists to prevent.
    """
    for spec in ARMS:
        if spec.arm == name:
            return spec
    raise KeyError(f"no delegation arm called {name!r}; known: {[s.arm for s in ARMS]}")


def load_delegation_probes(probe_dir: str | None = None) -> list[Probe]:
    """The delegation corpus, from its own file rather than the whole probe directory.

    One file, for `_m12_probes`' reason: `data/evals/probes/` holds fifteen corpora and a
    directory-wide read would put every other suite's questions through this protocol. The
    directory as a whole is still id-checked by the corpus suite's own `load_probes`.

    Raises:
        FileNotFoundError: The corpus is absent. Named, because a suite that runs zero probes and
            reports zero failures is the coverage lie this lane's exit codes exist to prevent.
    """
    directory = Path(probe_dir if probe_dir is not None else settings.live_probe_dir)
    path = directory / DELEGATION_PROBE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"the delegation suite needs {path}, which does not exist")
    return ProbeSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))).probes


def treatment_tools(treatment: str, tools: Collection[str]) -> frozenset[str]:
    """Which of `tools` are the act `treatment` names.

    Derived from the two producers rather than spelled here. `task` is read by building
    `SubAgentMiddleware` (`agent/chemclaw_agent.subagent_tool_names`), because upstream writes the
    name as a literal inside `_build_task_tool` and exports no constant; a handoff is recognised by
    `agent/handoff.is_handoff_tool_name`, which asks the shape `handoff_tool_name` mints. A string
    written here would be a third copy that an upstream rename or a profile name containing a
    hyphen would leave silently stale, and silently stale is the whole failure mode of this
    measurement: an arm whose treatment cannot be recognised reads as an arm that declined.

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

    A gate's refusal is excluded because it is the one outcome that means the tool body never ran —
    `agent/audit.REFUSED` is the classification the producer makes, so this reads it rather than
    guessing at a message. Every other outcome (`ok`, `error`, `returned_failure`, `cancelled`)
    means the call was made: a `task` that entered the helper's graph and then failed is a
    delegation that happened, and treating it as a non-delegation would be a selection on the
    treatment's *success*, which is the shape `evals/delegation.py` took three tries to get out of.

    Returns:
        Session id → the tool names it ran. A session with no rows is absent rather than present
        and empty, so a caller can tell "ran nothing" from "was never asked".
    """
    from chemclaw.agent.audit import REFUSED

    if not session_ids:
        return {}
    ran: dict[str, set[str]] = {}
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
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

    Summed over the session's rows rather than read from one, because the quantity is what the
    session *spent* and a repeat that was retried books two rows. The four counters are weighted by
    `evals/autonomy.billed_tokens`, the one definition of this arithmetic in the tree — the two
    cache weights are configured (`eval_cache_read_weight`, `eval_cache_write_weight`) because a
    cached read is charged at a fraction of an input token and a cache write at a premium.

    Returns:
        Session id → its bill. A session with no row is absent, never zero: a turn that failed
        before billing legitimately records zero, and a row that has not been written is a hole in
        the data. Reporting the second as the first would put a fabricated cost into a comparison
        whose whole subject is cost.
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

    `agent/turn_cost.record_turn_cost` runs the write as its own task so a teardown cannot lose a
    pending cancellation, which means a row is *eventually* consistent with the stream having
    closed. A single read therefore races the flush and would record a hole for a turn that was
    booked a moment later.

    Bounded by `eval_delegation_ledger_wait_seconds` and polled ten times inside it: enough
    attempts that a slow flush is caught, few enough that a genuinely unwritten row is a hole
    within the bound rather than a run that hangs. The interval is derived from the bound rather
    than set beside it, so there is one number and it is configured.
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
    #: `<arm>/<task>#<repeat>` for each repeat the judge could not grade. An `ungraded` verdict is
    #: the absence of a grade, never a bad one (`evals/live_judge.Verdict`), and `VERDICT_SCORES`
    #: has no entry for it on purpose — so such a repeat cannot become an `ArmRun` and is a hole.
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
        *behaviour*: `delegated=False` is recorded and the comparator reports it as compliance,
        which is what makes this an intention-to-treat comparison rather than a selection on the
        treatment.
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

    The same discipline `cli/live_probes --regrade` takes one axis over: a campaign's arms need not
    have been driven in one process. Two passes of one arm, or a baseline recorded last week beside
    a treatment recorded today, aggregate into one report here — and that is also the only way a
    `(task, arm)` pair whose repeats *differ* in whether they delegated can be assembled at all,
    which is the `partially_delegated` bucket.

    Raises:
        ValueError: A file holds no runs. An empty aggregation reported as a comparison is what
            `NoComparableTask` exists to refuse; a file that contributed nothing has to say so
            before the comparator is asked.
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

    Present-and-empty rather than omitted, which is this lane's standing rule
    (`cli/live_probes._findings_report`): a bucket nothing landed in is a real result, and a reader
    who cannot see the row cannot tell it from a bucket the report forgot to compute.
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

    Quality, tokens and wall clock are printed side by side and never folded into one figure, for
    the reason `evals/delegation.py` opens with: "cheaper but worse" and "better but slower" are
    different answers that a single number hides.
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

    Each arm is compared separately rather than folded together, because "delegation helped here and
    hurt there" is the finding a single aggregate destroys — and across arms it is the finding
    selective routing would need.

    Returns:
        `(reports, refused)` — the reports by arm, and arm → why no report exists for it. A
        `NoComparableTask` is carried rather than raised: with four arms, one arm that nothing
        compared must not cost the report on the other three.
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
